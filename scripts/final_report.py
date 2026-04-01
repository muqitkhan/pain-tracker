"""
final_report.py — Daily consolidation report
Reads morning/evening JSON reports, finds common patterns,
sends a clean HTML email summary at ~8:30 PM Central Time

New in this version
───────────────────
• Community coverage section — lists communities that returned no results
• Video signals section — YouTube and TikTok videos found for top problems
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import google.generativeai as genai


SESSIONS = ["morning", "evening"]

LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "America/Chicago"))

SEVERITY_COLOR = {
    "High":   ("#7f1d1d", "#fef2f2", "#dc2626"),
    "Medium": ("#78350f", "#fffbeb", "#d97706"),
    "Low":    ("#14532d", "#f0fdf4", "#16a34a"),
}

CATEGORY_COLOR = {
    "Finance":      "#1d4ed8",
    "Productivity": "#7c3aed",
    "Business":     "#0f766e",
    "Education":    "#b45309",
    "Health":       "#be185d",
    "Consumer":     "#c2410c",
    "Career":       "#1e40af",
    "Other":        "#4b5563",
}


# ──────────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────────

def load_reports(date):
    """Load all session JSON reports for the given date."""
    reports = {}
    for session in SESSIONS:
        path = Path(f"reports/{date}-{session}.json")
        if path.exists():
            with open(path) as f:
                reports[session] = json.load(f)
            n = len(reports[session].get("top_10_problems", []))
            print(f"  Loaded {session} report ({n} problems)")
        else:
            print(f"  WARNING: {session} report not found at {path}")
    return reports


def aggregate_coverage(reports):
    """
    Merge per-subreddit coverage status across all sessions.
    Returns:
        always_empty  — subreddits empty in EVERY session that ran
        any_error     — subreddits that errored in at least one session
        error_details — {sub: [error strings across sessions]}
    """
    all_subs_seen  = set()
    empty_in       = {}   # sub → count of sessions it was empty in
    sessions_count = len(reports)
    error_details  = {}

    for session, report in reports.items():
        coverage = report.get("coverage", {})
        for sub, status in coverage.items():
            all_subs_seen.add(sub)
            if status == "empty":
                empty_in[sub] = empty_in.get(sub, 0) + 1
            elif status.startswith("error"):
                error_details.setdefault(sub, []).append(f"{session}: {status[6:]}")

    always_empty = sorted(
        s for s, count in empty_in.items()
        if count == sessions_count and s not in error_details
    )
    any_error = sorted(error_details.keys())

    return always_empty, any_error, error_details


def aggregate_video_signals(reports):
    """
    Collect all video_signals entries across sessions.
    Deduplicate YouTube/TikTok URLs.
    Returns a list of signal dicts, keeping the richest entry per problem.
    """
    seen_urls = set()
    merged    = {}   # search_keywords → merged signal dict

    for session, report in reports.items():
        for sig in report.get("video_signals", []):
            key = sig.get("search_keywords", sig.get("problem_summary", ""))

            # Deduplicate individual video URLs
            yt_deduped = []
            for v in sig.get("youtube", []):
                if v.get("url") not in seen_urls:
                    seen_urls.add(v["url"])
                    yt_deduped.append(v)

            tt_deduped = []
            for v in sig.get("tiktok", []):
                if v.get("url") not in seen_urls:
                    seen_urls.add(v["url"])
                    tt_deduped.append(v)

            if key not in merged:
                merged[key] = dict(sig)
                merged[key]["youtube"] = yt_deduped
                merged[key]["tiktok"]  = tt_deduped
            else:
                merged[key]["youtube"].extend(yt_deduped)
                merged[key]["tiktok"].extend(tt_deduped)
                merged[key]["youtube_found"] = bool(merged[key]["youtube"])
                merged[key]["tiktok_found"]  = bool(merged[key]["tiktok"])

    return list(merged.values())


# ──────────────────────────────────────────────────────────────────────────────
# Gemini pattern analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_patterns(reports):
    """Use Gemini to find common themes and validate problems across sessions."""
    all_problems = []
    for session, report in reports.items():
        for p in report.get("top_10_problems", []):
            p["_session"] = session
            all_problems.append(p)

    if not all_problems:
        return None

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model_name = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash-latest")
    model = genai.GenerativeModel(model_name)

    data = json.dumps(all_problems, indent=2)[:22000]

    prompt = f"""You are a product researcher reviewing a full day of Reddit pain point data.

Below are problems collected from {len(reports)} scraping sessions today ({', '.join(reports.keys())}).
Each problem has a "_session" field showing which session found it.

Your tasks:
1. Find RECURRING THEMES — problems that appeared in multiple sessions or are highly similar
2. CLUSTER similar problems together under one theme
3. Identify TOP 5 VALIDATED PROBLEMS (highest confidence, most demand evidence)
4. Note any UNIQUE one-off problems worth watching

Return ONLY a valid JSON object (no markdown, no preamble) with this exact structure:
{{
  "top_validated_problems": [
    {{
      "rank": 1,
      "theme": "Short theme name (3-5 words)",
      "summary": "2-3 sentence description of the validated problem and why it matters",
      "frequency": "Found in X of {len(reports)} sessions",
      "severity": "High | Medium | Low",
      "category": "Finance | Productivity | Business | Education | Health | Consumer | Career | Other",
      "solution_opportunity": "What specific product/feature could solve this (1-2 sentences)",
      "sources": [
        {{
          "session": "morning",
          "url": "https://reddit.com/...",
          "subreddit": "subreddit_name",
          "quote": "brief quote (max 100 chars)"
        }}
      ],
      "merged_from": ["brief list of similar problems grouped into this theme"]
    }}
  ],
  "notable_one_offs": [
    {{
      "problem": "brief description",
      "source_url": "https://...",
      "subreddit": "subreddit_name",
      "why_notable": "why a product builder should pay attention to this"
    }}
  ],
  "key_insight": "One powerful takeaway sentence for a product builder from today's full dataset"
}}

Today's full problem dataset:
{data}"""

    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception as exc:
            print(f"  Gemini attempt {attempt + 1} failed: {exc}")
            time.sleep(6)

    return None


# ──────────────────────────────────────────────────────────────────────────────
# HTML badge helpers
# ──────────────────────────────────────────────────────────────────────────────

def sev_badge(severity):
    dark, light, _ = SEVERITY_COLOR.get(severity, ("#374151", "#f9fafb", "#6b7280"))
    return (
        f'<span style="background:{light};color:{dark};padding:2px 8px;'
        f'border-radius:4px;font-size:11px;font-weight:600;">{severity}</span>'
    )


def cat_badge(category):
    color = CATEGORY_COLOR.get(category, "#4b5563")
    return (
        f'<span style="background:{color};color:#ffffff;padding:2px 8px;'
        f'border-radius:4px;font-size:11px;font-weight:600;">{category}</span>'
    )


def session_dot(session):
    colors = {"morning": "#f59e0b", "evening": "#8b5cf6"}
    c = colors.get(session, "#6b7280")
    return f'<span style="color:{c};font-weight:600;">● {session.capitalize()}</span>'


# ──────────────────────────────────────────────────────────────────────────────
# HTML email builder
# ──────────────────────────────────────────────────────────────────────────────

def build_html_email(date, reports, analysis, video_signals, always_empty, any_error, error_details):
    sessions_used = list(reports.keys())
    total_scraped = sum(r.get("stats", {}).get("total_posts_collected", 0) for r in reports.values())
    total_subs = max((r.get("stats", {}).get("subreddits_scraped", 0) for r in reports.values()), default=0)
    validated = analysis.get("top_validated_problems", []) if analysis else []
    total_errors = sum(r.get("stats", {}).get("subreddits_errored", 0) for r in reports.values())

    def _li(items):
        return "".join(f"<li>{i}</li>" for i in items)

    # Top validated problems (max 5)
    validated_items = []
    for p in validated[:5]:
        theme = p.get("theme", "Unnamed theme")
        summary = p.get("summary", "")
        severity = p.get("severity", "Medium")
        validated_items.append(f"<strong>{theme}</strong> ({severity}) — {summary}")

    # Session highlights (top 3 each)
    session_items = []
    for session, report in reports.items():
        rows = []
        for item in report.get("top_10_problems", [])[:3]:
            rows.append(f"{item.get('problem_summary','')}")
        if rows:
            session_items.append(f"<strong>{session.title()}</strong>: " + "; ".join(rows))

    # Video summary (keep compact)
    yt_total = sum(len(v.get("youtube", [])) for v in video_signals)
    tt_total = sum(len(v.get("tiktok", [])) for v in video_signals)
    video_items = []
    if yt_total or tt_total:
        for sig in video_signals[:5]:
            name = sig.get("problem_summary", "")[:90]
            yt = sig.get("youtube", [])[:2]
            tt = sig.get("tiktok", [])[:2]
            links = []
            links += [f"YT: <a href='{v.get('url','')}'>{v.get('title','Video')}</a>" for v in yt]
            links += [f"TT: <a href='{v.get('url','')}'>{v.get('title','Video')}</a>" for v in tt]
            if links:
                video_items.append(f"<strong>{name}</strong><br/>" + " | ".join(links))

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial, sans-serif; background:#f8fafc; color:#0f172a; padding:20px;">
  <div style="max-width:720px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;">
    <h2 style="margin:0 0 8px;">Daily Pain Point Report</h2>
    <p style="margin:0 0 16px;color:#475569;">{date} · Sessions: {', '.join(sessions_used)}</p>

    <p style="margin:0 0 16px;"><strong>Stats:</strong> {total_scraped:,} posts · {total_subs} subreddits · Errors: {total_errors}</p>

    <h3 style="margin:0 0 8px;">Top Validated Problems</h3>
    <ul style="margin:0 0 16px;">{_li(validated_items) if validated_items else '<li>No validated problems found.</li>'}</ul>

    <h3 style="margin:0 0 8px;">Session Highlights</h3>
    <ul style="margin:0 0 16px;">{_li(session_items) if session_items else '<li>No session highlights available.</li>'}</ul>

    <h3 style="margin:0 0 8px;">Video Signals</h3>
    <p style="margin:0 0 8px;">YouTube: {yt_total} · TikTok: {tt_total}</p>
    <ul style="margin:0;">{_li(video_items) if video_items else '<li>No video results found.</li>'}</ul>
  </div>
</body>
</html>
"""
    return html


def send_gmail(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = os.environ["GMAIL_ADDRESS"]
    msg["To"]      = os.environ["RECIPIENT_EMAIL"]
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        server.sendmail(os.environ["GMAIL_ADDRESS"], os.environ["RECIPIENT_EMAIL"], msg.as_string())
    print("  Email sent successfully!")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    # Reports are named by Central Time date (America/Chicago)
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    print(f"\n{'='*50}")
    print(f"  FINAL REPORT | {today}")
    print(f"{'='*50}\n")

    # ── Step 1: Load session reports ──────────────────────────────────────────
    print("Loading session reports...")
    reports = load_reports(today)

    if not reports:
        print("No reports found for today. Sending health check email.")
        html = build_html_email(
            today, {}, {}, [],
            [], [], {},
        )
        subject = f"Pain Point Health Check — {today} | No Data"
        print(f"Sending email to {os.environ['RECIPIENT_EMAIL']}...")
        send_gmail(subject, html)
        print("\nFinal report complete.\n")
        return

    # ── Step 2: Aggregate coverage & video signals ─────────────────────────────
    print("\nAggregating coverage and video signals...")
    always_empty, any_error, error_details = aggregate_coverage(reports)
    video_signals = aggregate_video_signals(reports)

    yt_total = sum(len(v["youtube"]) for v in video_signals)
    tt_total = sum(len(v["tiktok"])  for v in video_signals)
    print(f"  Video signals: {yt_total} YouTube · {tt_total} TikTok")
    print(f"  Community coverage: {len(always_empty)} always-empty · {len(any_error)} errored")

    # ── Step 3: AI pattern analysis ───────────────────────────────────────────
    print("\nRunning Gemini pattern analysis...")
    analysis = analyze_patterns(reports)
    if not analysis:
        print("WARNING: Pattern analysis failed. Sending raw session data only.")

    # ── Step 4: Build HTML email ──────────────────────────────────────────────
    print("Building email...")
    html = build_html_email(
        today, reports, analysis or {},
        video_signals, always_empty, any_error, error_details,
    )

    # ── Step 5: Send email ────────────────────────────────────────────────────
    validated_n = len((analysis or {}).get("top_validated_problems", []))
    total_scraped = sum(r.get("stats", {}).get("total_posts_collected", 0) for r in reports.values())
    total_with_results = sum(r.get("stats", {}).get("subreddits_with_results", 0) for r in reports.values())
    if total_scraped == 0 or total_with_results == 0:
        subject = f"Pain Point Health Check — {today} | No Data"
    else:
        subject = f"Pain Point Report — {today} | {validated_n} Validated Problems"
    print(f"Sending email to {os.environ['RECIPIENT_EMAIL']}...")
    send_gmail(subject, html)

    print("\nFinal report complete.\n")


if __name__ == "__main__":
    main()
