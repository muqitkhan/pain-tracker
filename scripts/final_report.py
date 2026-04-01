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

    def _resolve_gemini_model():
        configured = os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash"
        configured = configured if configured.startswith("models/") else f"models/{configured}"
        candidates = [configured, "models/gemini-2.0-flash", "models/gemini-1.5-flash"]
        for name in candidates:
            try:
                genai.GenerativeModel(name)
                return name
            except Exception:
                continue
        try:
            for m in genai.list_models():
                methods = getattr(m, "supported_generation_methods", []) or []
                if "generateContent" in methods:
                    return m.name
        except Exception:
            pass
        return configured

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel(_resolve_gemini_model())

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
    sessions_used  = list(reports.keys())
    total_scraped  = sum(r.get("stats", {}).get("total_posts_collected", 0) for r in reports.values())
    total_subs     = max((r.get("stats", {}).get("subreddits_scraped", 0) for r in reports.values()), default=0)
    validated_n    = len(analysis.get("top_validated_problems", [])) if analysis else 0
    total_with_results = sum(r.get("stats", {}).get("subreddits_with_results", 0) for r in reports.values())
    total_errored = sum(r.get("stats", {}).get("subreddits_errored", 0) for r in reports.values())

    # ── Header ────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">

<!-- HEADER -->
<tr><td style="background:#0f172a;border-radius:12px 12px 0 0;padding:28px 32px;">
  <p style="margin:0;color:#94a3b8;font-size:12px;letter-spacing:.08em;text-transform:uppercase;">Daily Pain Point Research</p>
  <h1 style="margin:6px 0 0;color:#f8fafc;font-size:24px;font-weight:700;">Reddit Insights Report</h1>
  <p style="margin:4px 0 0;color:#64748b;font-size:14px;">{date} &nbsp;·&nbsp; Final Summary</p>
</td></tr>

<!-- STATS BAR -->
<tr><td style="background:#1e293b;padding:16px 32px;">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td align="center" style="color:#94a3b8;font-size:12px;">
      <span style="display:block;color:#f8fafc;font-size:20px;font-weight:700;">{total_scraped:,}</span>Posts Analyzed
    </td>
    <td align="center" style="color:#94a3b8;font-size:12px;">
      <span style="display:block;color:#f8fafc;font-size:20px;font-weight:700;">{total_subs}</span>Subreddits
    </td>
    <td align="center" style="color:#94a3b8;font-size:12px;">
      <span style="display:block;color:#f8fafc;font-size:20px;font-weight:700;">{len(sessions_used)}</span>Sessions
    </td>
    <td align="center" style="color:#94a3b8;font-size:12px;">
      <span style="display:block;color:#f8fafc;font-size:20px;font-weight:700;">{validated_n}</span>Validated Problems
    </td>
  </tr></table>
</td></tr>

<!-- SESSION BADGES -->
<tr><td style="background:#1e293b;padding:0 32px 16px;border-bottom:1px solid #334155;">
  <p style="margin:0;font-size:12px;color:#64748b;">Sessions included: &nbsp;
    {" &nbsp;·&nbsp; ".join(session_dot(s) for s in sessions_used)}
  </p>
</td></tr>

<!-- MAIN CONTENT -->
<tr><td style="background:#ffffff;padding:28px 32px;">
"""

    # ── Data Availability Notice ─────────────────────────────────────────────
    if total_scraped == 0 or total_with_results == 0:
        html += f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
<tr><td style="background:#fff7ed;border-left:4px solid #f97316;border-radius:0 8px 8px 0;padding:14px 18px;">
  <p style="margin:0;font-size:11px;font-weight:700;color:#c2410c;text-transform:uppercase;letter-spacing:.06em;">Data Availability Notice</p>
  <p style="margin:6px 0 0;font-size:13px;color:#7c2d12;line-height:1.6;">
    We couldn't find usable data for this run. This usually means Reddit sources were empty or blocked.
    The workflow completed and will try again on the next scheduled run.
  </p>
</td></tr></table>
"""

    if total_errored > 0:
        html += f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
<tr><td style="background:#fef2f2;border-left:4px solid #dc2626;border-radius:0 8px 8px 0;padding:14px 18px;">
  <p style="margin:0;font-size:11px;font-weight:700;color:#b91c1c;text-transform:uppercase;letter-spacing:.06em;">Coverage Errors</p>
  <p style="margin:6px 0 0;font-size:13px;color:#7f1d1d;line-height:1.6;">
    Some communities could not be reached in this run. We will keep trying in future runs.
  </p>
</td></tr></table>
"""

    # ── Key Insight ───────────────────────────────────────────────────────────
    if analysis and analysis.get("key_insight"):
        html += f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
<tr><td style="background:#eff6ff;border-left:4px solid #2563eb;border-radius:0 8px 8px 0;padding:14px 18px;">
  <p style="margin:0;font-size:11px;font-weight:700;color:#1d4ed8;text-transform:uppercase;letter-spacing:.06em;">Key Insight of the Day</p>
  <p style="margin:6px 0 0;font-size:14px;color:#1e3a5f;line-height:1.6;">{analysis["key_insight"]}</p>
</td></tr></table>
"""

    # ── Top Validated Problems ─────────────────────────────────────────────────
    top_problems = analysis.get("top_validated_problems", []) if analysis else []

    if top_problems:
        html += """<h2 style="margin:0 0 16px;font-size:17px;color:#0f172a;font-weight:700;">
  ✅ Top Validated Problems
  <span style="font-size:12px;font-weight:400;color:#64748b;margin-left:8px;">— appeared across multiple sessions</span>
</h2>"""

        for problem in top_problems:
            rank     = problem.get("rank", "?")
            theme    = problem.get("theme", "Unnamed theme")
            summary  = problem.get("summary", "")
            freq     = problem.get("frequency", "")
            severity = problem.get("severity", "Medium")
            category = problem.get("category", "Other")
            opp      = problem.get("solution_opportunity", "")
            sources  = problem.get("sources", [])
            merged   = problem.get("merged_from", [])

            _, light_bg, border_color = SEVERITY_COLOR.get(severity, ("#374151", "#f9fafb", "#6b7280"))

            sources_html = ""
            for src in sources:
                url   = src.get("url", "#")
                sub   = src.get("subreddit", "?")
                quote = src.get("quote", "")
                sess  = src.get("session", "")
                sources_html += f"""
<tr><td style="padding:6px 0;border-top:1px solid #e2e8f0;">
  <table width="100%"><tr>
    <td style="font-size:11px;color:#64748b;">{session_dot(sess)} &nbsp;
      <a href="{url}" style="color:#2563eb;text-decoration:none;">r/{sub} ↗</a>
    </td>
  </tr>
  {"<tr><td style='font-size:12px;color:#475569;font-style:italic;padding-top:3px;'>&ldquo;" + quote + "&rdquo;</td></tr>" if quote else ""}
  </table>
</td></tr>"""

            merged_html = ""
            if merged:
                items = "".join(f'<li style="margin:2px 0;color:#64748b;">{m}</li>' for m in merged)
                merged_html = f"""
<p style="margin:10px 0 4px;font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;">Similar problems grouped here</p>
<ul style="margin:0;padding-left:18px;font-size:12px;">{items}</ul>"""

            html += f"""
<table width="100%" cellpadding="0" cellspacing="0"
  style="margin-bottom:16px;border:1px solid {border_color};border-radius:10px;overflow:hidden;">
<tr><td style="background:{light_bg};padding:12px 16px;">
  <table width="100%"><tr><td>
    <span style="font-size:22px;font-weight:800;color:#0f172a;margin-right:8px;">#{rank}</span>
    {sev_badge(severity)} &nbsp; {cat_badge(category)} &nbsp;
    <span style="font-size:11px;color:#94a3b8;">{freq}</span>
  </td></tr></table>
  <h3 style="margin:8px 0 0;font-size:15px;color:#0f172a;font-weight:700;">{theme}</h3>
</td></tr>
<tr><td style="background:#ffffff;padding:14px 16px;">
  <p style="margin:0;font-size:13px;color:#334155;line-height:1.7;">{summary}</p>
  {"<p style='margin:10px 0 0;font-size:12px;color:#0f172a;'><strong>Solution Opportunity:</strong> " + opp + "</p>" if opp else ""}
  {merged_html}
  {"<p style='margin:12px 0 4px;font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;'>Sources</p><table width='100%'>" + sources_html + "</table>" if sources_html else ""}
</td></tr></table>
"""

    # ── Individual Session Reports ─────────────────────────────────────────────
    html += """<h2 style="margin:24px 0 16px;font-size:17px;color:#0f172a;font-weight:700;">
  📋 Individual Session Reports
</h2>"""

    session_colors = {
        "morning":   ("#fffbeb", "#92400e", "#f59e0b"),
        "evening":   ("#f5f3ff", "#3b0764", "#8b5cf6"),
    }

    for session, report in reports.items():
        problems = report.get("top_10_problems", [])
        stats    = report.get("stats", {})
        tw       = report.get("time_window", {})
        light, dark, accent = session_colors.get(session, ("#f9fafb", "#111827", "#6b7280"))

        # Time-window note
        since_note = ""
        if tw.get("since_readable"):
            since_note = f'<span style="font-size:11px;color:#94a3b8;margin-left:12px;">Gap coverage since {tw["since_readable"]}</span>'

        # No problems found notice
        if not problems:
            rows = f"""
<tr><td colspan="6" style="padding:16px 8px;text-align:center;font-size:13px;color:#94a3b8;">
  ⚠️ No problems returned by AI for this session
</td></tr>"""
        else:
            rows = ""
            for p in problems:
                r   = p.get("rank", "?")
                ps  = p.get("problem_summary", "?")[:90]
                cat = p.get("category", "?")
                sev = p.get("severity", "?")
                url = p.get("source_url", "#")
                sub = p.get("subreddit", "?")
                up  = p.get("upvotes", 0)
                rows += f"""
<tr>
  <td style="padding:8px 4px;font-size:12px;color:{accent};font-weight:700;width:24px;">#{r}</td>
  <td style="padding:8px 8px;font-size:13px;color:#1e293b;">{ps}</td>
  <td style="padding:8px 4px;white-space:nowrap;">{cat_badge(cat)}</td>
  <td style="padding:8px 4px;white-space:nowrap;">{sev_badge(sev)}</td>
  <td style="padding:8px 4px;font-size:11px;white-space:nowrap;">
    <a href="{url}" style="color:#2563eb;text-decoration:none;">r/{sub} ↗</a>
  </td>
  <td style="padding:8px 4px;font-size:11px;color:#94a3b8;white-space:nowrap;">▲ {up}</td>
</tr>"""

        html += f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
<tr><td style="background:{light};padding:12px 16px;">
  <span style="font-size:16px;font-weight:700;color:{dark};">{session.capitalize()} Report</span>
  <span style="font-size:12px;color:#94a3b8;margin-left:12px;">
    {stats.get("total_posts_collected", 0):,} posts · {stats.get("subreddits_scraped", 0)} subreddits
  </span>
  {since_note}
</td></tr>
<tr><td style="padding:0 16px 12px;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr style="border-bottom:2px solid #f1f5f9;">
      <th style="padding:8px 4px;font-size:11px;color:#94a3b8;text-align:left;width:24px;">#</th>
      <th style="padding:8px 8px;font-size:11px;color:#94a3b8;text-align:left;">Problem</th>
      <th style="padding:8px 4px;font-size:11px;color:#94a3b8;text-align:left;">Category</th>
      <th style="padding:8px 4px;font-size:11px;color:#94a3b8;text-align:left;">Severity</th>
      <th style="padding:8px 4px;font-size:11px;color:#94a3b8;text-align:left;">Source</th>
      <th style="padding:8px 4px;font-size:11px;color:#94a3b8;text-align:left;">Score</th>
    </tr>
    {rows}
  </table>
</td></tr></table>
"""

    # ── Notable One-Offs ──────────────────────────────────────────────────────
    one_offs = analysis.get("notable_one_offs", []) if analysis else []
    if one_offs:
        html += """<h2 style="margin:8px 0 14px;font-size:17px;color:#0f172a;font-weight:700;">
  👀 Notable One-Off Problems
  <span style="font-size:12px;font-weight:400;color:#64748b;margin-left:8px;">— unique signals worth watching</span>
</h2>"""
        for item in one_offs:
            prob = item.get("problem", "")
            url  = item.get("source_url", "#")
            sub  = item.get("subreddit", "?")
            why  = item.get("why_notable", "")
            html += f"""
<table width="100%" style="margin-bottom:10px;border:1px solid #e2e8f0;border-radius:8px;"><tr>
<td style="padding:12px 16px;">
  <p style="margin:0;font-size:13px;color:#1e293b;font-weight:600;">{prob}</p>
  <p style="margin:4px 0 0;font-size:12px;color:#64748b;">{why}</p>
  <p style="margin:6px 0 0;font-size:11px;">
    <a href="{url}" style="color:#2563eb;text-decoration:none;">r/{sub} — view post ↗</a>
  </p>
</td></tr></table>"""

    # ── Video Signals ─────────────────────────────────────────────────────────
    if video_signals:
        has_any_video = any(
            v["youtube_found"] or v["tiktok_found"] for v in video_signals
        )
        html += """<h2 style="margin:24px 0 14px;font-size:17px;color:#0f172a;font-weight:700;">
  🎬 Video Signals
  <span style="font-size:12px;font-weight:400;color:#64748b;margin-left:8px;">— YouTube &amp; TikTok content on top problems</span>
</h2>"""

        if not has_any_video:
            html += """<table width="100%" style="margin-bottom:20px;border:1px solid #e2e8f0;border-radius:8px;"><tr>
<td style="padding:16px;text-align:center;font-size:13px;color:#94a3b8;">
  No video results found. TikTok scraping may be blocked; YouTube key may not be set.
</td></tr></table>"""
        else:
            for sig in video_signals:
                if not sig["youtube_found"] and not sig["tiktok_found"]:
                    continue

                rank_label = f"Problem #{sig['problem_rank']}"
                keywords   = sig.get("search_keywords", "")
                summary    = sig.get("problem_summary", "")

                yt_rows = ""
                for v in sig.get("youtube", []):
                    yt_rows += f"""
<tr style="border-top:1px solid #f1f5f9;">
  <td style="padding:8px 4px;width:20px;">
    <img src="{v.get('thumbnail','')}" width="16" height="16" style="border-radius:2px;" onerror="this.style.display='none'">
  </td>
  <td style="padding:8px 4px;font-size:12px;">
    <a href="{v.get('url','#')}" style="color:#dc2626;text-decoration:none;font-weight:600;">{v.get('title','')[:70]}</a>
    <span style="display:block;color:#94a3b8;font-size:11px;">{v.get('channel','')} · {v.get('published_at','')}</span>
  </td>
</tr>"""

                tt_rows = ""
                for v in sig.get("tiktok", []):
                    tt_rows += f"""
<tr style="border-top:1px solid #f1f5f9;">
  <td style="padding:8px 4px;font-size:12px;">
    <a href="{v.get('url','#')}" style="color:#1d1d1d;text-decoration:none;font-weight:600;">{v.get('title','')[:70]}</a>
    <span style="display:block;color:#94a3b8;font-size:11px;">{v.get('channel','')} · 👁 {v.get('views',0):,} · ❤️ {v.get('likes',0):,}</span>
  </td>
</tr>"""

                platforms_html = ""
                if yt_rows:
                    platforms_html += f"""
<td style="width:50%;padding:0 8px 0 0;vertical-align:top;">
  <p style="margin:0 0 6px;font-size:11px;font-weight:700;color:#dc2626;">▶ YouTube</p>
  <table width="100%">{yt_rows}</table>
</td>"""
                if tt_rows:
                    platforms_html += f"""
<td style="width:50%;padding:0 0 0 8px;vertical-align:top;">
  <p style="margin:0 0 6px;font-size:11px;font-weight:700;color:#1d1d1d;">♪ TikTok</p>
  <table width="100%">{tt_rows}</table>
</td>"""

                html += f"""
<table width="100%" style="margin-bottom:16px;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
<tr><td style="background:#f8fafc;padding:10px 14px;border-bottom:1px solid #e2e8f0;">
  <span style="font-size:12px;font-weight:700;color:#0f172a;">{rank_label}</span>
  <span style="font-size:11px;color:#64748b;margin-left:8px;">"{keywords}"</span>
  <span style="display:block;font-size:12px;color:#475569;margin-top:2px;">{summary[:90]}</span>
</td></tr>
<tr><td style="padding:12px 14px;">
  <table width="100%"><tr>{platforms_html}</tr></table>
</td></tr></table>
"""

    # ── Community Coverage ─────────────────────────────────────────────────────
    if always_empty or any_error:
        html += """<h2 style="margin:24px 0 14px;font-size:17px;color:#0f172a;font-weight:700;">
  📊 Community Coverage
</h2>"""

        if always_empty:
            empty_pills = " ".join(
                f'<span style="display:inline-block;background:#f1f5f9;color:#475569;'
                f'padding:2px 8px;border-radius:12px;font-size:11px;margin:2px;">r/{s}</span>'
                for s in always_empty
            )
            html += f"""
<table width="100%" style="margin-bottom:12px;border:1px solid #e2e8f0;border-radius:8px;"><tr>
<td style="padding:14px 16px;">
  <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#374151;">
    ○ No Problems Found ({len(always_empty)} communities)
  </p>
  <p style="margin:0 0 8px;font-size:12px;color:#64748b;">
    These communities had posts but none matched problem-signal keywords across all sessions today.
  </p>
  <div>{empty_pills}</div>
</td></tr></table>"""

        if any_error:
            err_rows = ""
            for sub in any_error:
                details = "; ".join(error_details.get(sub, []))
                err_rows += f"""
<tr style="border-top:1px solid #f1f5f9;">
  <td style="padding:6px 4px;font-size:12px;font-weight:600;color:#0f172a;white-space:nowrap;">r/{sub}</td>
  <td style="padding:6px 8px;font-size:11px;color:#94a3b8;">{details[:120]}</td>
</tr>"""
            html += f"""
<table width="100%" style="margin-bottom:12px;border:1px solid #fee2e2;border-radius:8px;"><tr>
<td style="padding:14px 16px;">
  <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#991b1b;">
    ✗ Scrape Errors ({len(any_error)} communities)
  </p>
  <table width="100%">{err_rows}</table>
</td></tr></table>"""

    # ── Footer ────────────────────────────────────────────────────────────────
    generated_at = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    html += f"""
</td></tr>

<!-- FOOTER -->
<tr><td style="background:#f8fafc;border-top:1px solid #e2e8f0;border-radius:0 0 12px 12px;padding:16px 32px;">
  <p style="margin:0;font-size:11px;color:#94a3b8;text-align:center;">
    Generated {generated_at} &nbsp;·&nbsp; Pain Point Tracker &nbsp;·&nbsp;
    Powered by Reddit API + Gemini AI + YouTube API
  </p>
</td></tr>

</table>
</td></tr></table>
</body></html>"""

    return html


# ──────────────────────────────────────────────────────────────────────────────
# Email sender
# ──────────────────────────────────────────────────────────────────────────────

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
