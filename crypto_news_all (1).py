"""
crypto_news_all.py - All-in-one automatic crypto news email agent.

What it does:
  1. Fetches crypto news (RSS feeds, or any website you add)
  2. Removes duplicates and ranks stories confirmed by several outlets
  3. Skips stories it already emailed (sent_links.json)
  4. Emails the news as a formatted HTML email to one or more people
  5. Runs automatically at the times in SEND_TIMES (or use --once with a scheduler)

Install:
    pip install requests feedparser beautifulsoup4

Environment variables (Gmail needs an App Password, no spaces):
    EMAIL_SENDER    you@gmail.com
    EMAIL_PASSWORD  16-character app password
    EMAIL_RECEIVER  one address, or several separated by commas: a@x.com,b@y.com

Run:
    python crypto_news_all.py              # runs forever, emails at SEND_TIMES
    python crypto_news_all.py --once       # one email now, then exit (GitHub Actions / cron / Task Scheduler)
    python crypto_news_all.py --test       # print news only, no email
    python crypto_news_all.py --keyword bitcoin
    python crypto_news_all.py --site https://www.theblock.co
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

SEND_TIMES = ["08:00", "12:00", "18:00"]   # 24-hour, computer's local time (only for non-GitHub use)
MAX_STORIES = 15                            # stories per email
MAX_AGE_HOURS = 24                          # ignore stories older than this (None = no limit)
KEYWORD = None                              # e.g. "bitcoin", or None for everything
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")   # Outlook: smtp.office365.com
SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_links.json")

DEFAULT_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsBot/1.0)"}

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


def collect_news(sites=None, keyword=None):
    sources = [(s, None) for s in sites] if sites else list(DEFAULT_FEEDS.items())
    all_items = []
    for name, feed in sources:
        try:
            items = get_news_from_site(name) if feed is None else parse_feed(name, feed)
            all_items.extend(items)
            print(f"[ok] {name}: {len(items)} items")
        except Exception as ex:
            print(f"[fail] {name}: {ex}")

    if keyword:
        k = keyword.lower()
        all_items = [i for i in all_items if k in i["title"].lower() or k in i["summary"].lower()]

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


def build_email_bodies(items):
    text_lines, html_rows = [], []
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
    body = ("<html><body style='font-family:Arial,sans-serif;'><h2>Crypto News</h2>"
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


def run_job(sites=None, keyword=None, dry_run=False):
    """Fetch -> drop already-sent stories -> email -> remember them."""
    sent = load_sent()
    items = [i for i in collect_news(sites, keyword) if i["link"] not in sent][:MAX_STORIES]

    if not items:
        print("No new stories to send.")
        return

    text, body = build_email_bodies(items)
    if dry_run:
        print("\n" + text)
        return

    subject = f"Crypto News - {datetime.now().strftime('%d %b %Y, %H:%M')}"
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


def run_scheduler(sites, keyword):
    print(f"Scheduler started. Will send at: {', '.join(SEND_TIMES)} (local time). Ctrl+C to stop.")
    while True:
        run_at = next_run_time(datetime.now())
        print(f"Next email at {run_at.strftime('%Y-%m-%d %H:%M')}")
        while datetime.now() < run_at:
            time.sleep(30)
        try:
            run_job(sites, keyword)
        except Exception as ex:           # one failure must not stop the agent
            print(f"[error] {ex}")
        time.sleep(61)


def main():
    p = argparse.ArgumentParser(description="Automatic crypto news email agent")
    p.add_argument("--site", action="append", help="Website URL to include (repeatable)")
    p.add_argument("--keyword", default=KEYWORD, help="Only stories matching this word")
    p.add_argument("--once", action="store_true", help="Send one email now and exit")
    p.add_argument("--test", action="store_true", help="Print news only, do not send email")
    args = p.parse_args()

    if args.test:
        run_job(args.site, args.keyword, dry_run=True)
        return

    missing = [v for v in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER") if v not in os.environ]
    if missing:
        raise SystemExit(f"Set these environment variables first: {', '.join(missing)}")

    if args.once:
        run_job(args.site, args.keyword)
    else:
        run_scheduler(args.site, args.keyword)


if __name__ == "__main__":
    main()
