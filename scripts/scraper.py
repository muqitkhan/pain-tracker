"""
scraper.py — Reddit pain point scraper with Gemini AI analysis
Runs 3x daily (morning / afternoon / evening)

Key behaviours
──────────────
• Scrapes every community twice per run:
    hot  → engagement-ranked posts (always)
    new  → time-filtered posts since the previous session ended (gap coverage)
• Tracks coverage per community: ok / empty / error
• After AI analysis, searches YouTube (official API) and TikTok (web scrape)
  for the top 10 problems to surface video pain signals
"""

import json
import os
import re
import sys
import time
import requests
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

import praw
import google.generativeai as genai
try:
    from groq import Groq
except Exception:
    Groq = None


# ── Problem-signal keywords ────────────────────────────────────────────────────
PROBLEM_SIGNALS = [
    "can't", "cannot", "frustrated", "frustrating", "annoying", "problem",
    "issue", "struggle", "struggling", "please help", "anyone else",
    "tired of", "sick of", "why does", "how do i", "so hard",
    "hate", "stuck", "broken", "advice needed", "worried", "lost",
    "confused", "failed", "failing", "no solution", "still can't",
    "impossible", "nightmare", "disaster", "awful", "terrible",
    "doesn't work", "not working", "keeps happening", "help me",
]

# ── Time windows ───────────────────────────────────────────────────────────────
# Each session's "new" filter covers posts since the previous session ended.
#
# Times are expressed in Central Time (America/Chicago) and converted to UTC
# for comparison against Reddit post timestamps (which are UTC epochs).
LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "America/Chicago"))

# Morning covers posts since previous day 6 PM.
# Afternoon covers posts since today 8 AM.
# Evening covers posts since today 1 PM.
SINCE_LOCAL_HOURS = {
    "morning":   {"base_day_offset": -1, "hour": 18, "minute": 0},
    "afternoon": {"base_day_offset": 0,  "hour": 8,  "minute": 0},
    "evening":   {"base_day_offset": 0,  "hour": 13, "minute": 0},
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_since_utc(session, local_now=None):
    """Return a UTC epoch float for the start of this session's gap window."""
    cfg = SINCE_LOCAL_HOURS.get(session)
    if not cfg:
        return None

    if local_now is None:
        local_now = datetime.now(LOCAL_TZ)

    base = local_now + timedelta(days=cfg["base_day_offset"])
    since_local = base.replace(
        hour=cfg["hour"],
        minute=cfg["minute"],
        second=0,
        microsecond=0,
    )
    return since_local.astimezone(timezone.utc).timestamp()


def get_reddit_client():
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=f"PainPointTracker/1.0 by u/{os.environ['REDDIT_USERNAME']}",
    )


def is_problem_post(post):
    text = (post["title"] + " " + post["body"]).lower()
    return any(kw in text for kw in PROBLEM_SIGNALS)


def _clean_html_text(value):
    # RSS summaries contain HTML; convert to plain text for AI and keyword scans.
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _iso_to_utc_epoch(value):
    if not value:
        return None
    try:
        # RSS dates are usually RFC3339 with trailing Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _rss_entry_to_post(entry, subreddit_name):
    post_url = ""
    for link in entry.findall("{http://www.w3.org/2005/Atom}link"):
        if link.attrib.get("rel", "alternate") == "alternate":
            post_url = link.attrib.get("href", "")
            break
    if not post_url:
        post_url = entry.findtext("{http://www.w3.org/2005/Atom}id", default="")
    if post_url and "/comments/" in post_url and not post_url.endswith(".json"):
        post_url = post_url.rstrip("/") + ".json"

    title = entry.findtext("{http://www.w3.org/2005/Atom}title", default="").strip()
    summary_html = entry.findtext("{http://www.w3.org/2005/Atom}content", default="")
    body = _clean_html_text(summary_html)[:400]
    updated = entry.findtext("{http://www.w3.org/2005/Atom}updated", default="")
    created_utc = _iso_to_utc_epoch(updated)

    # RSS does not include votes/comments, so we keep conservative defaults.
    return {
        "title": title,
        "body": body,
        "score": 1,
        "num_comments": 0,
        "url": post_url,
        "subreddit": subreddit_name,
        "created_utc": created_utc or 0,
        "top_comments": [],
    }


def scrape_subreddit_rss(subreddit_name, since_utc=None, hot_limit=12, new_limit=30):
    """
    Fallback scraper via Reddit RSS (no auth required).
    Uses /hot/.rss and /new/.rss and deduplicates by URL.
    """
    headers = {
        "User-Agent": "PainPointTracker/1.0 (RSS fallback; contact: personal-use)",
    }
    seen = {}
    try:
        feeds = [
            (f"https://www.reddit.com/r/{subreddit_name}/hot/.rss", hot_limit, False),
            (f"https://www.reddit.com/r/{subreddit_name}/new/.rss", new_limit, True),
        ]
        for url, limit, time_filter in feeds:
            resp = _fetch_with_backoff(url, headers=headers, timeout=20)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.text)
            entries = root.findall("{http://www.w3.org/2005/Atom}entry")[:limit]
            for entry in entries:
                post = _rss_entry_to_post(entry, subreddit_name)
                if time_filter and since_utc and post["created_utc"] and post["created_utc"] < since_utc:
                    continue
                key = post["url"] or f"{subreddit_name}:{post['title']}"
                seen[key] = post
            _rate_sleep()
    except Exception as exc:
        return [], f"error:{type(exc).__name__}: {exc}"

    posts = list(seen.values())
    return posts, ("ok" if posts else "empty")


def _listing_to_post(item, subreddit_name):
    data = item.get("data", {})
    permalink = data.get("permalink", "")
    url = f"https://reddit.com{permalink}" if permalink else data.get("url", "")
    return {
        "title": data.get("title", "").strip(),
        "body": (data.get("selftext") or "")[:400].replace("\n", " "),
        "score": int(data.get("score", 0) or 0),
        "num_comments": int(data.get("num_comments", 0) or 0),
        "url": url,
        "subreddit": subreddit_name,
        "created_utc": float(data.get("created_utc", 0) or 0),
        "top_comments": [],
    }


def scrape_subreddit_public_json(subreddit_name, since_utc=None, hot_limit=12, new_limit=30):
    """
    Public JSON listing (no auth required).
    Uses /hot.json and /new.json, dedupes by URL.
    """
    headers = {
        "User-Agent": "PainPointTracker/1.0 (public json; contact: personal-use)",
        "Accept": "application/json",
    }
    seen = {}
    try:
        feeds = [
            (f"https://www.reddit.com/r/{subreddit_name}/hot.json?limit={hot_limit}", False),
            (f"https://www.reddit.com/r/{subreddit_name}/new.json?limit={new_limit}", True),
        ]
        for url, time_filter in feeds:
            resp = _fetch_with_backoff(url, headers=headers, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            items = payload.get("data", {}).get("children", [])
            for item in items:
                post = _listing_to_post(item, subreddit_name)
                if time_filter and since_utc and post["created_utc"] and post["created_utc"] < since_utc:
                    continue
                key = post["url"] or f"{subreddit_name}:{post['title']}"
                seen[key] = post
            _rate_sleep()
    except Exception as exc:
        return [], f"error:{type(exc).__name__}: {exc}"

    posts = list(seen.values())
    return posts, ("ok" if posts else "empty")


def _post_to_dict(post, subreddit_name):
    post.comments.replace_more(limit=0)
    top_comments = []
    for c in post.comments[:3]:
        if hasattr(c, "body") and c.score > 1:
            top_comments.append({
                "text": c.body[:250].replace("\n", " "),
                "score": c.score,
            })
    return {
        "title": post.title,
        "body": post.selftext[:400].replace("\n", " ") if post.selftext else "",
        "score": post.score,
        "num_comments": post.num_comments,
        "url": f"https://reddit.com{post.permalink}",
        "subreddit": subreddit_name,
        "created_utc": post.created_utc,
        "top_comments": top_comments,
    }


def _rate_sleep():
    base = float(os.environ.get("REDDIT_MIN_DELAY_SEC", "1.2"))
    jitter = float(os.environ.get("REDDIT_JITTER_SEC", "0.8"))
    time.sleep(base + random.random() * jitter)


def _fetch_with_backoff(url, headers=None, timeout=20, max_retries=3):
    delay = float(os.environ.get("REDDIT_BACKOFF_SEC", "8"))
    for attempt in range(max_retries + 1):
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 429:
            return resp
        if attempt < max_retries:
            time.sleep(delay + random.random() * 2.0)
            delay *= 2
            continue
        return resp


# ──────────────────────────────────────────────────────────────────────────────
# Reddit scraping
# ──────────────────────────────────────────────────────────────────────────────

def scrape_subreddit(reddit, subreddit_name, since_utc=None, hot_limit=12, new_limit=30):
    """
    Scrape a subreddit using hot (engagement) + new (time gap).

    Returns
    -------
    posts  : deduplicated list of post dicts
    status : "ok" | "empty" | "error:<detail>"
    """
    seen = {}
    try:
        sub = reddit.subreddit(subreddit_name)

        # — Hot posts (always) ─────────────────────────────────────────────────
        for post in sub.hot(limit=hot_limit):
            if post.distinguished or post.score < 3:
                continue
            seen[post.id] = _post_to_dict(post, subreddit_name)

        # — New posts filtered by time window (gap coverage) ───────────────────
        if since_utc:
            for post in sub.new(limit=new_limit):
                if post.created_utc < since_utc:
                    break   # new() is chronological desc — stop when older than window
                if post.distinguished or post.score < 1:
                    continue
                if post.id not in seen:
                    seen[post.id] = _post_to_dict(post, subreddit_name)

        time.sleep(1.2)  # Respect Reddit rate limits

    except Exception as exc:
        return [], f"error:{type(exc).__name__}: {exc}"

    posts = list(seen.values())
    return posts, ("ok" if posts else "empty")


def resolve_reddit_mode():
    """
    REDDIT_MODE:
      - api: require Reddit API credentials
      - public: use public JSON listing (no auth)
      - rss: force RSS fallback
      - auto (default): use API when creds exist, else public JSON
    """
    requested = os.environ.get("REDDIT_MODE", "auto").strip().lower()
    if requested not in {"api", "public", "rss", "auto"}:
        requested = "auto"

    has_api_creds = all(
        os.environ.get(name)
        for name in ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"]
    )

    if requested == "rss":
        return "rss"
    if requested == "public":
        return "public"
    if requested == "api":
        return "api"
    return "api" if has_api_creds else "public"


# ──────────────────────────────────────────────────────────────────────────────
# Gemini AI analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_with_gemini(posts, session):
    """Send top candidate posts to Gemini; returns list of top-10 problem dicts."""
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model_name = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash-latest")
    model = genai.GenerativeModel(model_name)

    posts_json = json.dumps(posts, indent=2)[:22000]

    prompt = f"""You are a senior product researcher. From these Reddit posts, \
identify the TOP 10 that describe a REAL, SPECIFIC problem a software product or app could solve.

Session: {session.upper()}

Scoring criteria (rank highest to lowest):
1. Specificity — concrete problem, not vague venting
2. Actionability — technology/software could realistically help
3. Demand signal — high upvotes or comment count
4. Diversity — prefer a variety of categories across the top 10

Return ONLY a valid JSON array of exactly 10 objects. No markdown, no preamble, just JSON.

Each object must have EXACTLY these fields:
{{
  "rank": 1,
  "problem_summary": "One clear sentence describing the exact problem",
  "category": "Finance | Productivity | Business | Education | Health | Consumer | Career | Other",
  "severity": "High | Medium | Low",
  "solution_hint": "What type of app or feature could fix this (1 sentence)",
  "evidence_quote": "Most relevant verbatim quote from post or comment (max 130 chars)",
  "source_url": "https://reddit.com/...",
  "subreddit": "subreddit_name",
  "post_title": "original post title",
  "upvotes": 0,
  "num_comments": 0,
  "search_keywords": "3-6 keywords a frustrated person would type into YouTube or TikTok search (prefer TikTok-style phrasing like 'rant', 'POV', 'storytime' when relevant)"
}}

Reddit posts to analyze:
{posts_json}"""

    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text.strip())
            return result if isinstance(result, list) else result.get("problems", [])
        except Exception as exc:
            print(f"  Gemini attempt {attempt + 1} failed: {exc}")
            time.sleep(6)

    print("  WARNING: All Gemini attempts failed. Returning empty list.")
    return []


def analyze_with_groq(posts, session):
    """Use Groq API for the same TOP-10 extraction as Gemini."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or Groq is None:
        print("  Groq not configured. Skipping Groq analysis.")
        return []

    model_name = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
    client = Groq(api_key=api_key)

    posts_json = json.dumps(posts, indent=2)[:22000]
    prompt = f"""You are a senior product researcher. From these Reddit posts, \
identify the TOP 10 that describe a REAL, SPECIFIC problem a software product or app could solve.

Session: {session.upper()}

Scoring criteria (rank highest to lowest):
1. Specificity — concrete problem, not vague venting
2. Actionability — technology/software could realistically help
3. Demand signal — high upvotes or comment count
4. Diversity — prefer a variety of categories across the top 10

Return ONLY a valid JSON array of exactly 10 objects. No markdown, no preamble, just JSON.

Each object must have EXACTLY these fields:
{{
  "rank": 1,
  "problem_summary": "One clear sentence describing the exact problem",
  "category": "Finance | Productivity | Business | Education | Health | Consumer | Career | Other",
  "severity": "High | Medium | Low",
  "solution_hint": "What type of app or feature could fix this (1 sentence)",
  "evidence_quote": "Most relevant verbatim quote from post or comment (max 130 chars)",
  "source_url": "https://reddit.com/...",
  "subreddit": "subreddit_name",
  "post_title": "original post title",
  "upvotes": 0,
  "num_comments": 0,
  "search_keywords": "3-6 keywords a frustrated person would type into YouTube or TikTok search (prefer TikTok-style phrasing like 'rant', 'POV', 'storytime' when relevant)"
}}

Reddit posts to analyze:
{posts_json}"""

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
            )
            text = (response.choices[0].message.content or "").strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text.strip())
            return result if isinstance(result, list) else result.get("problems", [])
        except Exception as exc:
            print(f"  Groq attempt {attempt + 1} failed: {exc}")
            time.sleep(6)

    print("  WARNING: All Groq attempts failed. Returning empty list.")
    return []


def analyze_with_grok(posts, session):
    """Use xAI Grok (OpenAI-compatible) for the same TOP-10 extraction."""
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("  Grok not configured. Skipping Grok analysis.")
        return []

    model_name = os.environ.get("XAI_MODEL", "grok-2-1212")
    url = "https://api.x.ai/v1/chat/completions"

    posts_json = json.dumps(posts, indent=2)[:22000]
    prompt = f"""You are a senior product researcher. From these Reddit posts, \
identify the TOP 10 that describe a REAL, SPECIFIC problem a software product or app could solve.

Session: {session.upper()}

Scoring criteria (rank highest to lowest):
1. Specificity — concrete problem, not vague venting
2. Actionability — technology/software could realistically help
3. Demand signal — high upvotes or comment count
4. Diversity — prefer a variety of categories across the top 10

Return ONLY a valid JSON array of exactly 10 objects. No markdown, no preamble, just JSON.

Each object must have EXACTLY these fields:
{{
  "rank": 1,
  "problem_summary": "One clear sentence describing the exact problem",
  "category": "Finance | Productivity | Business | Education | Health | Consumer | Career | Other",
  "severity": "High | Medium | Low",
  "solution_hint": "What type of app or feature could fix this (1 sentence)",
  "evidence_quote": "Most relevant verbatim quote from post or comment (max 130 chars)",
  "source_url": "https://reddit.com/...",
  "subreddit": "subreddit_name",
  "post_title": "original post title",
  "upvotes": 0,
  "num_comments": 0,
  "search_keywords": "3-6 keywords a frustrated person would type into YouTube or TikTok search (prefer TikTok-style phrasing like 'rant', 'POV', 'storytime' when relevant)"
}}

Reddit posts to analyze:
{posts_json}"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=40)
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text.strip())
            return result if isinstance(result, list) else result.get("problems", [])
        except Exception as exc:
            print(f"  Grok attempt {attempt + 1} failed: {exc}")
            time.sleep(6)

    print("  WARNING: All Grok attempts failed. Returning empty list.")
    return []


def build_fallback_top(posts, n=10):
    """
    Build a simple top-N list from raw posts when AI fails or returns nothing.
    """
    ranked = sorted(
        posts,
        key=lambda x: x.get("score", 0) + x.get("num_comments", 0) * 2,
        reverse=True,
    )
    out = []
    for i, p in enumerate(ranked[:n], start=1):
        body = (p.get("body") or "").strip()
        quote = (body or p.get("title", ""))[:130]
        title = (p.get("title") or "").strip()
        subreddit = p.get("subreddit", "")
        keywords = title[:80] if title else f"{subreddit} problem"
        out.append({
            "rank": i,
            "problem_summary": (title or "No title")[:160],
            "category": "Other",
            "severity": "Medium",
            "solution_hint": "Manual review recommended",
            "evidence_quote": quote,
            "source_url": p.get("url", ""),
            "subreddit": subreddit,
            "post_title": title,
            "upvotes": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "search_keywords": keywords,
        })
    return out


def send_scrape_confirmation(session, today, coverage, total_posts, problems_count, youtube_hits, tiktok_hits):
    """
    Send a lightweight confirmation email after a scrape run.
    """
    if os.environ.get("SCRAPE_CONFIRM_EMAIL", "").lower() not in ("1", "true", "yes"):
        return
    required = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "RECIPIENT_EMAIL")
    if not all(os.environ.get(k) for k in required):
        print("  Confirmation email skipped: Gmail secrets missing.")
        return

    ok_count    = sum(1 for s in coverage.values() if s == "ok")
    empty_count = sum(1 for s in coverage.values() if s == "empty")
    error_count = sum(1 for s in coverage.values() if s.startswith("error"))

    subject = f"Scrape Confirmation — {session.title()} ({today})"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;font-size:14px;color:#111;">
      <h3 style="margin:0 0 8px;">Scrape Confirmation</h3>
      <p style="margin:0 0 8px;"><strong>Session:</strong> {session.title()}<br/>
      <strong>Date:</strong> {today}</p>
      <ul style="margin:0 0 8px;padding-left:18px;">
        <li>Total posts collected: <strong>{total_posts}</strong></li>
        <li>Problem-signal posts analyzed: <strong>{problems_count}</strong></li>
        <li>Coverage: <strong>{ok_count}</strong> ok · <strong>{empty_count}</strong> empty · <strong>{error_count}</strong> errors</li>
        <li>Video hits: <strong>{youtube_hits}</strong> YouTube · <strong>{tiktok_hits}</strong> TikTok</li>
      </ul>
      <p style="margin:0;">This is an automated confirmation email.</p>
    </body></html>
    """.strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = os.environ["RECIPIENT_EMAIL"]
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
            server.sendmail(os.environ["GMAIL_ADDRESS"], os.environ["RECIPIENT_EMAIL"], msg.as_string())
        print("  Confirmation email sent.")
    except Exception as exc:
        print(f"  Confirmation email failed: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# YouTube search  (official Data API v3)
# ──────────────────────────────────────────────────────────────────────────────

YOUTUBE_COMPLAINT_SIGNALS = [
    "problem", "issue", "not working", "doesn't work", "cant", "can't",
    "frustrated", "annoying", "broken", "scam", "warning", "regret",
    "avoid", "rant", "hate", "worst", "failed", "failure", "complaint",
]


def _count_signal_hits(text):
    lowered = (text or "").lower()
    return sum(1 for kw in YOUTUBE_COMPLAINT_SIGNALS if kw in lowered)


def _youtube_search_queries(base_query):
    """Build complaint-focused query variants for better pain-signal recall."""
    q = base_query.strip()
    if not q:
        return []
    variants = [
        q,
        f"{q} problem",
        f"{q} not working",
        f"{q} complaint",
        f"{q} review",
        f"{q} scam",
        f"{q} rant",
        f"frustrated with {q}",
    ]
    # Preserve order while deduplicating.
    return list(dict.fromkeys(variants))


def _tiktok_search_queries(base_query):
    """Build TikTok-style complaint queries (rant/POV/storytime) for better recall."""
    q = base_query.strip()
    if not q:
        return []
    variants = [
        q,
        f"{q} rant",
        f"{q} POV",
        f"{q} storytime",
        f"{q} problem",
        f"{q} not working",
        f"{q} worst",
        f"{q} scam",
    ]
    return list(dict.fromkeys(variants))


def _youtube_api_get(path, api_key, params):
    resp = requests.get(
        f"https://www.googleapis.com/youtube/v3/{path}",
        params={**params, "key": api_key},
        timeout=12,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_youtube_video_details(video_ids, api_key):
    """Fetch statistics + richer snippet data for candidate video IDs."""
    if not video_ids:
        return {}
    try:
        data = _youtube_api_get(
            "videos",
            api_key,
            {
                "part": "snippet,statistics",
                "id": ",".join(video_ids),
                "maxResults": len(video_ids),
            },
        )
    except Exception as exc:
        print(f"  YouTube video details fetch failed: {exc}")
        return {}

    details = {}
    for item in data.get("items", []):
        vid = item.get("id")
        if not vid:
            continue
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        details[vid] = {
            "channel": snippet.get("channelTitle", ""),
            "published_at": snippet.get("publishedAt", "")[:10],
            "description": snippet.get("description", "")[:200],
            "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
            "view_count": int(stats.get("viewCount", 0) or 0),
            "comment_count": int(stats.get("commentCount", 0) or 0),
            "like_count": int(stats.get("likeCount", 0) or 0),
        }
    return details


def _fetch_youtube_comments(video_id, api_key, max_comments=12):
    """Fetch top comments and score complaint density."""
    try:
        data = _youtube_api_get(
            "commentThreads",
            api_key,
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": max_comments,
                "textFormat": "plainText",
                "order": "relevance",
            },
        )
    except Exception:
        return 0, []

    comments = []
    signal_hits = 0
    for item in data.get("items", []):
        top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
        text = top.get("textDisplay", "")
        likes = int(top.get("likeCount", 0) or 0)
        signal_hits += _count_signal_hits(text)
        if text:
            comments.append({"text": text[:180], "likes": likes})
    return signal_hits, comments[:3]


def search_youtube(query, max_results=3):
    """Search YouTube for complaint-heavy videos and return ranked results."""
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        return []

    try:
        candidates = {}
        for q in _youtube_search_queries(query):
            data = _youtube_api_get(
                "search",
                api_key,
                {
                    "part": "snippet",
                    "q": q,
                    "type": "video",
                    "maxResults": 6,
                    "order": "relevance",
                    "relevanceLanguage": "en",
                },
            )
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                vid_id = item.get("id", {}).get("videoId", "")
                if not vid_id:
                    continue
                if vid_id not in candidates:
                    candidates[vid_id] = {
                        "video_id": vid_id,
                        "title": snippet.get("title", ""),
                        "channel": snippet.get("channelTitle", ""),
                        "published_at": snippet.get("publishedAt", "")[:10],
                        "description": snippet.get("description", "")[:200],
                        "url": f"https://www.youtube.com/watch?v={vid_id}",
                        "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                    }
            time.sleep(0.2)

        video_ids = list(candidates.keys())[:18]
        details = _fetch_youtube_video_details(video_ids, api_key)

        ranked = []
        for vid in video_ids:
            v = candidates[vid]
            d = details.get(vid, {})
            title_desc = f"{v.get('title','')} {v.get('description','')}"
            title_signal = _count_signal_hits(title_desc)
            comment_signal, sample_comments = _fetch_youtube_comments(vid, api_key)
            engagement = ((d.get("view_count", 0) // 10000) + (d.get("comment_count", 0) // 50))
            complaint_score = (title_signal * 3) + (comment_signal * 2) + min(engagement, 20)

            ranked.append({
                **v,
                "channel": d.get("channel", v.get("channel", "")),
                "published_at": d.get("published_at", v.get("published_at", "")),
                "description": d.get("description", v.get("description", "")),
                "thumbnail": d.get("thumbnail", v.get("thumbnail", "")),
                "view_count": d.get("view_count", 0),
                "comment_count": d.get("comment_count", 0),
                "like_count": d.get("like_count", 0),
                "complaint_score": complaint_score,
                "comment_signal_hits": comment_signal,
                "sample_comments": sample_comments,
            })
            time.sleep(0.1)

        ranked.sort(key=lambda x: x.get("complaint_score", 0), reverse=True)
        return ranked[:max_results]
    except Exception as exc:
        print(f"  YouTube search failed for '{query}': {exc}")
        return []


# ──────────────────────────────────────────────────────────────────────────────
# TikTok search  (web scrape — fragile, fails gracefully)
# ──────────────────────────────────────────────────────────────────────────────

def search_tiktok(query, max_results=3):
    """
    Parse TikTok's embedded __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON blob.

    Fragile — TikTok may change their frontend at any time.
    Always returns [] on any failure so the report still generates cleanly.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }
    try:
        def _search_once(q):
            url = "https://www.tiktok.com/search?q=" + requests.utils.quote(q) + "&t=video"
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            html = resp.text

            marker = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
            if marker not in html:
                print(f"  TikTok: rehydration marker not found for '{q}'")
                return []

            start = html.index(marker) + len(marker)
            start = html.index("{", start)          # jump to opening brace
            end   = html.index("</script>", start)
            data  = json.loads(html[start:end].rstrip(";"))

            results = []

            def _walk(obj, depth=0):
                # TikTok's rehydration blob is deeply nested; allow a bit more depth
                # so we don't miss video nodes.
                if depth > 20 or len(results) >= max_results:
                    return
                if isinstance(obj, dict):
                    # Video items carry desc + author dict + id
                    if (
                        isinstance(obj.get("author"), dict)
                        and obj.get("id")
                        and obj.get("desc")
                    ):
                        author_id = obj["author"].get("uniqueId", "")
                        vid_id    = obj["id"]
                        if author_id and vid_id:
                            stats = obj.get("stats", {})
                            results.append({
                                "title":   obj.get("desc", "")[:150],
                                "channel": f"@{author_id}",
                                "url":     f"https://www.tiktok.com/@{author_id}/video/{vid_id}",
                                "views":   stats.get("playCount", 0),
                                "likes":   stats.get("diggCount", 0),
                            })
                    for v in obj.values():
                        _walk(v, depth + 1)
                elif isinstance(obj, list):
                    for item in obj:
                        _walk(item, depth + 1)

            _walk(data)
            return results

        combined = []
        seen = set()
        for q in _tiktok_search_queries(query):
            for item in _search_once(q):
                url = item.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                combined.append(item)
            time.sleep(0.4)

        combined.sort(
            key=lambda x: (x.get("views", 0), x.get("likes", 0)),
            reverse=True,
        )
        return combined[:max_results]

    except Exception as exc:
        print(f"  TikTok scrape failed for '{query}': {type(exc).__name__}: {exc}")
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Video search orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def run_video_searches(top_problems):
    """Search YouTube + TikTok for the top 10 problems using AI-generated keywords."""
    video_results = []
    for problem in top_problems[:10]:
        rank     = problem.get("rank", "?")
        keywords = problem.get("search_keywords", problem.get("problem_summary", ""))[:80]

        print(f"  🎬 #{rank}: {keywords[:60]}...")

        yt = search_youtube(keywords, max_results=10)
        time.sleep(1)
        tt = search_tiktok(keywords, max_results=10)
        time.sleep(2)

        video_results.append({
            "problem_rank":    rank,
            "problem_summary": problem.get("problem_summary", ""),
            "search_keywords": keywords,
            "youtube":         yt,
            "tiktok":          tt,
            "youtube_found":   len(yt) > 0,
            "tiktok_found":    len(tt) > 0,
        })

    return video_results


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    session   = sys.argv[1] if len(sys.argv) > 1 else "morning"
    local_now = datetime.now(LOCAL_TZ)
    today     = local_now.strftime("%Y-%m-%d")
    since_utc = get_since_utc(session, local_now=local_now)

    print(f"\n{'='*55}")
    print(f"  PAIN POINT SCRAPER — {session.upper()} | {today}")
    if since_utc:
        since_str = datetime.fromtimestamp(since_utc, tz=timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
        print(f"  Gap coverage: posts since {since_str}")
    print(f"{'='*55}\n")

    with open("subreddits.json") as f:
        subreddits = json.load(f)

    print(f"Communities to scrape: {len(subreddits)}\n")

    # ── Step 1: Scrape all communities ────────────────────────────────────────
    source_mode = resolve_reddit_mode()
    print(f"Collection mode: {source_mode.upper()}")
    reddit = None
    if source_mode == "api":
        try:
            reddit = get_reddit_client()
        except Exception as exc:
            print(f"API client init failed ({exc}); falling back to RSS.")
            source_mode = "rss"

    all_posts = []
    coverage  = {}                          # sub → "ok" | "empty" | "error:..."

    for i, sub in enumerate(subreddits):
        print(f"[{i+1:02d}/{len(subreddits)}] r/{sub}", end="  ")
        if source_mode == "api" and reddit is not None:
            posts, status = scrape_subreddit(reddit, sub, since_utc=since_utc)
        elif source_mode == "public":
            posts, status = scrape_subreddit_public_json(sub, since_utc=since_utc)
            if status.startswith("error"):
                posts, status = scrape_subreddit_rss(sub, since_utc=since_utc)
        else:
            posts, status = scrape_subreddit_rss(sub, since_utc=since_utc)
        coverage[sub] = status
        all_posts.extend(posts)
        icon = "✓" if status == "ok" else ("○" if status == "empty" else "✗")
        print(f"{icon}  {len(posts)} posts | {status}")

    ok_count    = sum(1 for s in coverage.values() if s == "ok")
    empty_count = sum(1 for s in coverage.values() if s == "empty")
    error_count = sum(1 for s in coverage.values() if s.startswith("error"))

    print(f"\nCoverage  : {ok_count} with results · {empty_count} empty · {error_count} errors")
    print(f"Total posts: {len(all_posts)}")

    # ── Step 2: Pre-filter by problem signals ─────────────────────────────────
    problem_posts = [p for p in all_posts if is_problem_post(p)]
    problem_posts.sort(key=lambda x: x["score"] + x["num_comments"] * 2, reverse=True)
    candidates = problem_posts[:70]
    print(f"Problem-signal posts: {len(problem_posts)}")
    print(f"Sending top {len(candidates)} to Gemini...\n")

    # ── Step 3: Gemini AI analysis ────────────────────────────────────────────
    top_10 = analyze_with_gemini(candidates, session)
    if not top_10 or len(top_10) < 10:
        print("WARNING: Gemini returned no usable problems. Trying Groq...")
        top_10 = analyze_with_groq(candidates, session)
    if not top_10 or len(top_10) < 10:
        print("WARNING: Groq returned no usable problems. Trying Grok...")
        top_10 = analyze_with_grok(candidates, session)
    if not top_10 or len(top_10) < 10:
        print("WARNING: AI returned no usable problems. Falling back to top posts.")
        fallback_source = candidates if candidates else all_posts
        top_10 = build_fallback_top(fallback_source, n=10)

    # ── Step 4: Video signals ─────────────────────────────────────────────────
    video_signals = []
    yt_hits = 0
    tt_hits = 0
    if top_10:
        print("\nSearching YouTube & TikTok for video signals...")
        video_signals = run_video_searches(top_10)
        yt_hits = sum(1 for v in video_signals if v["youtube_found"])
        tt_hits = sum(1 for v in video_signals if v["tiktok_found"])
        print(f"  YouTube: {yt_hits}/{len(video_signals)} · TikTok: {tt_hits}/{len(video_signals)}")

    # ── Step 5: Save report ───────────────────────────────────────────────────
    report = {
        "session":    session,
        "date":       today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "time_window": {
            "since_utc": since_utc,
            "since_readable": (
                datetime.fromtimestamp(since_utc, tz=timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
                if since_utc else None
            ),
        },
        "stats": {
            "subreddits_scraped":       len(subreddits),
            "subreddits_with_results":  ok_count,
            "subreddits_empty":         empty_count,
            "subreddits_errored":       error_count,
            "total_posts_collected":    len(all_posts),
            "problem_signal_posts":     len(problem_posts),
            "sent_to_ai":               len(candidates),
        },
        "coverage":       coverage,         # full per-sub status map
        "top_10_problems": top_10,
        "video_signals":  video_signals,
    }

    os.makedirs("reports", exist_ok=True)
    out_path = f"reports/{today}-{session}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    # Optional confirmation email per session.
    send_scrape_confirmation(
        session=session,
        today=today,
        coverage=coverage,
        total_posts=len(all_posts),
        problems_count=len(problem_posts),
        youtube_hits=yt_hits if top_10 else 0,
        tiktok_hits=tt_hits if top_10 else 0,
    )

    # ── Step 6: Print summary ─────────────────────────────────────────────────
    print(f"\nReport saved → {out_path}")
    print("\nTop 3 problems found:")
    for item in top_10[:3]:
        print(f"  #{item.get('rank','?')} [{item.get('category','?')}] "
              f"[{item.get('severity','?')}] {item.get('problem_summary','?')[:70]}")

    if empty_count:
        empty_subs = [s for s, st in coverage.items() if st == "empty"]
        print(f"\nEmpty ({empty_count}): {', '.join(f'r/{s}' for s in empty_subs[:10])}")
    if error_count:
        err_subs = [s for s, st in coverage.items() if st.startswith("error")]
        print(f"Errors ({error_count}): {', '.join(f'r/{s}' for s in err_subs[:10])}")

    print(f"\n{session.upper()} scrape complete.\n")


if __name__ == "__main__":
    main()
