"""
crypto_news_all.py - Automatic crypto news email agent with a DIFFERENT topic in each email.

Each of the 3 daily emails has its own "profile" (topic + keywords):
    morning  -> Bitcoin & market moves
    noon     -> Ethereum, altcoins & DeFi
    evening  -> Regulation, ETFs & policy

The profile is chosen from the scheduled slot (news.yml passes it in SCHEDULE_CRON),
so even if GitHub starts a run hours late, it still sends the right topic.
For manual runs it falls back to the current India time. Force one with --profile.

Install:
    pip install requests feedparser beautifulsoup4

Environment variables (Gmail needs an App Password, no spaces):
    EMAIL_SENDER    you@gmail.com
    EMAIL_PASSWORD  16-character app password
    EMAIL_RECEIVER  one address, or several separated by commas: a@x.com,b@y.com

Run:
    python crypto_news_all.py --once                  # send the profile for the current IST time
    python crypto_news_all.py --once --profile noon   # force a profile
    python crypto_news_all.py --test --profile evening  # print only, no email
    python crypto_news_all.py                         # runs forever on your own PC at SEND_TIMES
"""

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

# ----------------------------- SETTINGS (edit these) -----------------------------

IST = timezone(timedelta(hours=5, minutes=30))

# One profile per email. Edit the keywords to change what each email covers.
# A story is included if ANY keyword appears as a whole word/phrase in its title or summary.
PROFILES = {
    "morning": {
        "title": "Morning Briefing: Bitcoin & Market",
        "keywords": ["bitcoin", "btc", "market", "markets", "price", "prices", "rally",
                     "crash", "trading", "traders", "whale", "whales", "miners", "mining"],
    },
    "noon": {
        "title": "Midday Update: Ethereum, Altcoins & DeFi",
        "keywords": ["ethereum", "ether", "eth", "solana", "sol", "xrp", "ripple", "cardano",
                     "altcoin", "altcoins", "defi", "nft", "nfts", "layer 2", "stablecoin",
                     "stablecoins", "token", "tokens", "airdrop", "dex"],
    },
    "evening": {
        "title": "Evening Brief: Regulation, ETFs & Policy",
        "keywords": ["sec", "regulation", "regulations", "regulator", "regulators", "regulatory",
                     "etf", "etfs", "law", "lawsuit", "court", "ban", "government", "tax",
                     "policy", "cbdc", "fed", "congress", "senate", "legal", "compliance"],
    },
}

MIN_MATCHED = 3          # if fewer topic stories than this, top up with general top stories
FILL_TO = 5              # ...up to this many stories, so the email is never empty
MAX_STORIES = 12         # stories per email
MAX_AGE_HOURS = 24       # ignore stories older than this (None = no limit)
SEND_TIMES = ["09:00", "12:00", "18:00"]   # only used when running on your own PC without --once
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")   # Outlook: smtp.office365.com
SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_links.json")

DEFAULT_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/1.0)"}

# ----------------------------- PROFILE HELPERS -----------------------------


# Which cron line in news.yml belongs to which profile (GitHub tells us the slot
# in the SCHEDULE_CRON variable, so a late run still sends the right topic).
# These keys must match the cron lines in news.yml EXACTLY.
SCHEDULE_MAP = {
    "30 3 * * *": "morning",    # 9:00 AM IST
    "30 6 * * *": "noon",       # 12:00 PM IST
    "30 12 * * *": "evening",   # 6:00 PM IST
}


def profile_for_now(now_ist=None):
    """Fallback for manual runs: pick the profile from the current India time."""
    now_ist = now_ist or datetime.now(IST)
    h = now_ist.hour
    if 5 <= h < 10:
        return "morning"
    if 10 <= h < 15:
        return "noon"
    return "evening"


def profile_for_run():
    """Scheduled run -> profile of its slot (even if GitHub started it hours late).
    Manual run or own PC -> profile by current IST time."""
    slot = os.environ.get("SCHEDULE_CRON", "").strip()
    return SCHEDULE_MAP.get(slot) or profile_for_now()


def matches_profile(item, keywords):
    text = f"{item['title']} {item['summary']}".lower()
    return any(re.search(rf"\b{re.escape(k.lower())}\b", text) for k in keywords)


def select_for_profile(items, profile_name):
    """Keep topic stories; top up with general top stories if too few match."""
    keywords = PROFILES[profile_name]["keywords"]
    matched = [i for i in items if matches_profile(i, keywords)]
    if len(matched) < MIN_MATCHED:
        extras = [i for i in items if i not in matched]
        matched += extras[: max(0, FILL_TO - len(matched))]
    return matched

# ----------------------------- FETCHING NEWS -----------------------------


def fetch(url, timeout=15):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def find_feed_url(site_url):
    """Look for an RSS/Atom feed on any website."""
    try:
        soup = BeautifulSoup(fetch(site_url).text, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            if link.get("type") in ("application/rss+xml", "application/atom+xml"):
                return urljoin(site_url, link.get("href"))
    except Exception:
        pass
    for path in ("/feed", "/rss", "/feed.xml", "/rss.xml"):
        candidate = urljoin(site_url, path)
        try:
            if feedparser.parse(candidate, request_headers=HEADERS).entries:
                return candidate
        except Exception:
            pass
    return None


def parse_feed(source, feed_url):
    items = []
    feed = feedparser.parse(feed_url, request_headers=HEADERS)
    for e in feed.entries:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc)
        summary = BeautifulSoup(getattr(e, "summary", ""), "html.parser").get_text(" ", strip=True)
        items.append({
            "source": source,
            "title": e.get("title", "").strip(),
            "link": e.get("link", ""),
            "summary": summary[:300],
            "published": published,
        })
    return items


def scrape_headlines(site_url):
    """Fallback for sites with no RSS."""
    items = []
    soup = BeautifulSoup(fetch(site_url).text, "html.parser")
    for tag in soup.find_all(["h1", "h2", "h3"]):
        a = tag.find("a") or tag.find_parent("a")
        title = tag.get_text(" ", strip=True)
        if a and a.get("href") and len(title) > 20:
            items.append({
                "source": site_url,
                "title": title,
                "link": urljoin(site_url, a["href"]),
                "summary": "",
                "published": None,
            })
    return items


def get_news_from_site(site_url):
    feed_url = find_feed_url(site_url)
    return parse_feed(site_url, feed_url) if feed_url else scrape_headlines(site_url)


def normalize(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())


def dedupe(items):
    seen, out = set(), []
    for i in items:
        key = i["link"] or normalize(i["title"])
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


def add_confirmation_counts(items, threshold=0.6):
    """Count how many DIFFERENT sources report a similar headline (accuracy signal)."""
    for item in items:
        sources = {item["source"]}
        for other in items:
            if other is item or other["source"] in sources:
                continue
            ratio = SequenceMatcher(None, normalize(item["title"]), normalize(other["title"])).ratio()
            if ratio >= threshold:
                sources.add(other["source"])
        item["confirmed_by"] = len(sources)
    return items


def collect_news(sites=None):
    sources = [(s, None) for s in sites] if sites else list(DEFAULT_FEEDS.items())
    all_items = []
    for name, feed in sources:
        try:
            items = get_news_from_site(name) if feed is None else parse_feed(name, feed)
            all_items.extend(items)
            print(f"[ok] {name}: {len(items)} items")
        except Exception as ex:
            print(f"[fail] {name}: {ex}")

    if MAX_AGE_HOURS:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
        all_items = [i for i in all_items if i["published"] is None or i["published"] >= cutoff]

    all_items = add_confirmation_counts(dedupe(all_items))
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    all_items.sort(key=lambda i: (i["confirmed_by"], i["published"] or oldest), reverse=True)
    return all_items


# ----------------------------- REMEMBER SENT STORIES -----------------------------


def load_sent():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent(links):
    links = list(links)[-3000:]
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f)


# ----------------------------- EMAIL -----------------------------


def build_email_bodies(items, heading):
    text_lines, html_rows = [heading, ""], []
    for n in items:
        date = n["published"].strftime("%Y-%m-%d %H:%M UTC") if n["published"] else "date n/a"
        meta = f"{n['source']} | {date} | confirmed by {n['confirmed_by']} source(s)"
        text_lines.append(f"{n['title']}\n  {meta}\n  {n['link']}\n")
        html_rows.append(
            f"""<div style="margin-bottom:18px;">
  <a href="{html.escape(n['link'])}" style="font-size:16px;font-weight:bold;text-decoration:none;">
    {html.escape(n['title'])}</a><br>
  <span style="color:#666;font-size:12px;">{html.escape(meta)}</span><br>
  <span style="font-size:13px;">{html.escape(n['summary'])}</span>
</div>"""
        )
    body = (f"<html><body style='font-family:Arial,sans-serif;'><h2>{html.escape(heading)}</h2>"
            + "".join(html_rows) + "</body></html>")
    return "\n".join(text_lines), body


def send_email(subject, text_body, html_body):
    sender = os.environ["EMAIL_SENDER"].strip()
    password = os.environ["EMAIL_PASSWORD"].replace(" ", "").strip()
    receivers = [r.strip() for r in os.environ["EMAIL_RECEIVER"].split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, context=context, timeout=30) as server:
            server.login(sender, password)
            server.sendmail(sender, receivers, msg.as_string())
    except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError):
        # fallback: port 587 with STARTTLS
        with smtplib.SMTP(SMTP_HOST, 587, timeout=30) as server:
            server.starttls(context=context)
            server.login(sender, password)
            server.sendmail(sender, receivers, msg.as_string())


# ----------------------------- MAIN JOB + SCHEDULER -----------------------------


def run_job(profile=None, sites=None, dry_run=False):
    """Fetch -> pick this profile's topic -> drop already-sent -> email -> remember."""
    profile = profile or profile_for_run()
    title = PROFILES[profile]["title"]
    print(f"Profile: {profile} ({title})")

    sent = load_sent()
    items = [i for i in collect_news(sites) if i["link"] not in sent]
    items = select_for_profile(items, profile)[:MAX_STORIES]

    if not items:
        print("No new stories to send.")
        return

    text, body = build_email_bodies(items, title)
    if dry_run:
        print("\n" + text)
        return

    subject = f"{title} - {datetime.now(IST).strftime('%d %b %Y, %H:%M')} IST"
    send_email(subject, text, body)
    sent.update(i["link"] for i in items)
    save_sent(sent)
    print(f"Email sent with {len(items)} stories.")


def next_run_time(now):
    candidates = []
    for t in SEND_TIMES:
        hour, minute = map(int, t.split(":"))
        run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run <= now:
            run += timedelta(days=1)
        candidates.append(run)
    return min(candidates)


def run_scheduler(sites):
    """For running on your own PC. Uses your computer's local time."""
    print(f"Scheduler started. Will send at: {', '.join(SEND_TIMES)} (local time). Ctrl+C to stop.")
    while True:
        run_at = next_run_time(datetime.now())
        print(f"Next email at {run_at.strftime('%Y-%m-%d %H:%M')}")
        while datetime.now() < run_at:
            time.sleep(30)
        try:
            run_job(sites=sites)
        except Exception as ex:           # one failure must not stop the agent
            print(f"[error] {ex}")
        time.sleep(61)


def main():
    p = argparse.ArgumentParser(description="Automatic crypto news email agent")
    p.add_argument("--site", action="append", help="Website URL to include (repeatable)")
    p.add_argument("--profile", choices=list(PROFILES), help="Force a topic profile (default: by IST time)")
    p.add_argument("--once", action="store_true", help="Send one email now and exit")
    p.add_argument("--test", action="store_true", help="Print news only, do not send email")
    args = p.parse_args()

    if args.test:
        run_job(args.profile, args.site, dry_run=True)
        return

    missing = [v for v in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER") if v not in os.environ]
    if missing:
        raise SystemExit(f"Set these environment variables first: {', '.join(missing)}")

    if args.once:
        run_job(args.profile, args.site)
    else:
        run_scheduler(args.site)


if __name__ == "__main__":
    main()
