# 🔍 Reddit Pain Point Tracker

Automated agentic workflow that scrapes 60 Reddit communities 2× per day,
uses Gemini AI to find the top 10 real problems per session, then sends a
consolidated email report at 8:30 PM Central Time (America/Chicago) every night.

---

## 📅 Daily Schedule (2× per day)

| Run | Time (Central) | What it does |
|-----|-----------------|--------------|
| Morning | 8:00 AM | Scrapes all 60 subs → Top 10 problems |
| Evening | 6:00 PM | Scrapes all 60 subs → Top 10 problems |
| **Final Report** | **8:30 PM** | Validates both reports → Email to you |

> Runs follow Central Time automatically (workflows guard local time via `America/Chicago`,
> so DST shifts don't require manual cron edits).

---

## ✅ Free Running Options (No Actions Minutes)

GitHub-hosted runners are **free for public repos**, but **limited for private repos**.
Pick one of these two free options:

### Option A — Make the Repo Public (Fastest)

1. Go to **Repo Settings → General → Change repository visibility**.
2. Set the repo to **Public**.

This gives **unlimited GitHub-hosted minutes** for Actions.
Secrets still **won’t be exposed to forks** (GitHub blocks secrets on forked PRs).

### Option B — Keep It Private + Use a Self‑Hosted Runner

1. Go to **Repo Settings → Actions → Runners → New self-hosted runner** and follow the setup steps on your machine.
2. Set this repo variable to use your runner:
   - **Settings → Secrets and variables → Actions → Variables → New repository variable**
   - Name: `RUNS_ON`
   - Value: `self-hosted`

Once set, all workflows will run on your machine for free.

---

## 🕐 Gap Coverage — How Time Windows Work

Each scrape session runs **two passes** on every subreddit:

| Pass | What it fetches | Why |
|------|----------------|-----|
| `hot` | Top trending posts (always) | Engagement signal — upvotes confirm real demand |
| `new` | Posts since the previous session ended | Gap coverage — catches overnight and between-session posts |

Time windows per session:

| Session | Covers posts since… |
|---------|---------------------|
| Morning (8 AM) | Previous day 6 PM Central — catches everything overnight |
| Evening (6 PM) | 8 AM today — no posts missed between morning and evening |

Posts fetched via both passes are **deduplicated by ID**, so nothing is counted twice.

---

## 🛠️ One-Time Setup

### Step 1 — Create GitHub Repository

1. Go to https://github.com/new
2. Name it: `reddit-pain-tracker`
3. Set to **Private** (your API keys will be stored as secrets)
4. Do NOT initialize with README
5. Click **Create repository**

---

### Step 2 — Enable GitHub Actions Write Permissions

1. Open your new repo → **Settings** → **Actions** → **General**
2. Scroll to **Workflow permissions**
3. Select ✅ **Read and write permissions**
4. Click **Save**

---

### Step 3 — Get Your Reddit API Credentials

1. Go to https://www.reddit.com/prefs/apps
2. Click **"Create App"** or **"Create Another App"**
3. Fill in:
   - **Name:** `PainPointTracker`
   - **Type:** Select **script**
   - **Redirect URI:** `http://localhost:8080`
4. Click **Create app**
5. Note your **Client ID** (under "personal use script") and **Client Secret**

---

### Step 4 — Get Your Google Gemini API Key (Free)

1. Go to https://aistudio.google.com/app/apikey
2. Click **Create API Key**
3. Copy the key — it looks like `AIzaSy...`

Free tier: **1,500 requests/day** — more than enough.

---

### Step 5 — Get Your YouTube Data API Key (Free)

This is used to search YouTube for videos about the top discovered problems.

1. Go to https://console.developers.google.com
2. Create a new project (or reuse an existing one)
3. Click **Enable APIs and Services** → search for **YouTube Data API v3** → Enable it
4. Go to **Credentials** → **Create Credentials** → **API Key**
5. Copy the key

Free quota: **10,000 units/day** — each search costs ~100 units.
Your usage: ~500 units/day (5 searches × 3 sessions). Well within free tier.

> **Note:** The YouTube key is optional. If you skip it, YouTube results will
> simply be absent from the email. TikTok scraping does not require a key.

---

### Step 6 — Get Your Gmail App Password

1. Go to https://myaccount.google.com/security
2. Make sure **2-Step Verification** is ON
3. Search for **"App passwords"** → Click it
4. App name: `Pain Point Tracker` → Click **Create**
5. Copy the 16-character password

---

### Step 7 — Add All Secrets to GitHub

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret Name | What to paste |
|-------------|--------------|
| `REDDIT_CLIENT_ID` | Your Reddit Client ID |
| `REDDIT_CLIENT_SECRET` | Your Reddit Client Secret |
| `REDDIT_USERNAME` | Your Reddit username (without u/) |
| `REDDIT_PASSWORD` | Your Reddit account password |
| `GEMINI_API_KEY` | Your Gemini API key |
| `YOUTUBE_API_KEY` | Your YouTube Data API v3 key *(optional)* |
| `GMAIL_ADDRESS` | Your full Gmail address |
| `GMAIL_APP_PASSWORD` | The 16-char App Password |
| `RECIPIENT_EMAIL` | Email where you want reports sent |

---

### Step 7.1 — Fast Secret Upload (Recommended)

Instead of manually creating each secret in GitHub UI:

1. Copy the template:
   ```bash
   cp .env.secrets.template .env.secrets.local
   ```
2. Fill values in `.env.secrets.local` (this file is git-ignored).
3. Install/login GitHub CLI:
   ```bash
   gh auth login
   ```
4. Upload all non-empty keys to your repo:
   ```bash
   chmod +x scripts/upload_github_secrets.sh
   ./scripts/upload_github_secrets.sh YOUR_USERNAME/reddit-pain-tracker
   ```

This uploads values to **GitHub > Settings > Secrets and variables > Actions**.

---

### Step 8 — Push This Project to GitHub

```bash
cd path/to/reddit-pain-tracker
git init
git add .
git commit -m "Initial setup"
git remote add origin https://github.com/YOUR_USERNAME/reddit-pain-tracker.git
git branch -M main
git push -u origin main
```

---

### Step 9 — Test It Manually

1. Go to your repo on GitHub → **Actions** tab
2. Click **"Morning Report (8:00 AM Central)"** in the left panel
3. Click **"Run workflow"** → **"Run workflow"**
4. Watch it run (~8–12 minutes for all 60 subreddits)
5. The final report will send automatically at **8:30 PM Central**

---

### Step 10 — Run Tonight End-to-End (GitHub Actions Only)

1. Go to **Actions** tab.
2. Select **"Run Tonight End-to-End (Manual)"**.
3. Click **Run workflow**.

This will run morning + evening scrapes, commit JSON reports, and send your final email tonight.

---

## 📁 Project Structure

```
reddit-pain-tracker/
├── .github/
│   └── workflows/
│       ├── scrape-morning.yml  ← 8:00 AM Central
│       ├── scrape-evening.yml  ← 6:00 PM Central
│       └── final-report.yml    ← 8:30 PM Central (email)
├── scripts/
│   ├── scraper.py             ← Reddit scraper + Gemini AI + YouTube/TikTok search
│   └── final_report.py        ← Consolidation + HTML email sender
├── reports/
│   └── YYYY-MM-DD-{session}.json   ← Auto-created by workflows
├── subreddits.json            ← All 60 subreddits to monitor
├── requirements.txt
└── README.md
```

---

## 💰 Cost Breakdown

| Service | Free Tier | Your Usage | Cost |
|---------|-----------|-----------|------|
| GitHub Actions | 2,000 min/month | ~480 min/month | **$0** |
| Reddit API (PRAW) | Free for personal use | ~180 calls/day | **$0** |
| Google Gemini 1.5 Flash | 1,500 req/day | ~50 req/day | **$0** |
| YouTube Data API v3 | 10,000 units/day | ~500 units/day | **$0** |
| TikTok | No API needed | Web scraping | **$0** |
| Gmail SMTP | Free | 1 email/day | **$0** |
| **Total** | | | **$0/month** |

---

## 📬 What Your Daily Email Includes

- **Stats bar:** Posts analyzed, subreddits covered, sessions run
- **Key Insight:** One AI-generated takeaway for a product builder
- **Top 5 Validated Problems:** Problems appearing across multiple sessions, with sources
- **Individual Session Tables:** All 30 problems (10 per session)
- **Notable One-Offs:** Unique signals worth watching
- **🎬 Video Signals:** YouTube and TikTok videos found for top problems
- **📊 Community Coverage:** Communities that returned no results, plus any scrape errors

---

## 🐛 Troubleshooting

**Workflow fails with "Authentication error"**
→ Double-check all secrets match exactly as shown in Step 7.

**"Bad credentials" from Reddit**
→ Try logging into Reddit in browser first. Some accounts need email verification.

**Email not arriving**
→ Check spam. Use App Password (not regular password) in `GMAIL_APP_PASSWORD`.

**No YouTube results in email**
→ Verify `YOUTUBE_API_KEY` secret is set. Check the Actions log for "YouTube search failed" lines.

**No TikTok results in email**
→ TikTok scraping is fragile and may be blocked by bot detection on GitHub Actions IPs.
   This is expected and will show as "No TikTok results found" in the email rather than failing.

**Workflow runs but no report committed**
→ Make sure Step 2 (Write Permissions) was done correctly.

---

*Built with Reddit PRAW API + Google Gemini 1.5 Flash + YouTube Data API v3 + GitHub Actions*
