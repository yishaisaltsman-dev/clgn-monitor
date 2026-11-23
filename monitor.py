import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen.json"
MAX_AGE_DAYS = 3
MAX_ALERTS_PER_RUN = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; clgn-monitor/2.1)"}

QUERIES = [
    "LightSolver",
    '"Light Solver"',
    "LightSolver LPU",
    "LightSolver CollPlant",
    "LightSolver optical computing",
    "LightSolver laser processing unit",
    "Optiphi",
    "Optiphi CollPlant",
    "OPHI Optiphi",
    "CLGN LightSolver",
]

KEEP_ICASE = re.compile(
    r"light\s?solver|optiphi|laser processing unit|\$ophi", re.I
)
KEEP_CASE = re.compile(r"\bOPHI\b")


def relevant(text):
    return bool(KEEP_ICASE.search(text) or KEEP_CASE.search(text))


def get(url, retries=0, **kw):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, **kw)
            if r.status_code == 429 and attempt < retries:
                time.sleep(12)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == retries:
                print(f"fetch failed: {url[:90]} -> {e}", file=sys.stderr)
    return None


def parse_rss(xml_text, source):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        pub = it.findtext("pubDate")
        try:
            when = parsedate_to_datetime(pub) if pub else None
            if when and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            when = None
        items.append(
            {"title": title, "link": link, "text": f"{title} {desc}",
             "when": when, "source": source}
        )
    return items


def google_news(q):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(f"{q} when:7d")
           + "&hl=en-US&gl=US&ceid=US:en")
    r = get(url)
    return parse_rss(r.text, "Google News") if r else []


def bing_news(q):
    url = ("https://www.bing.com/news/search?q="
           + urllib.parse.quote(q) + "&format=rss&qft=interval%3d%228%22")
    r = get(url)
    return parse_rss(r.text, "Bing News") if r else []


def hacker_news(q):
    url = ("https://hn.algolia.com/api/v1/search_by_date?tags=story&query="
           + urllib.parse.quote(q))
    r = get(url)
    out = []
    if not r:
        return out
    for h in r.json().get("hits", []):
        title = h.get("title") or ""
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        when = None
        if h.get("created_at_i"):
            when = datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc)
        out.append({"title": title, "link": link, "text": title,
                    "when": when, "source": "Hacker News"})
    return out


def gdelt():
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json"
           "&maxrecords=75&timespan=3d&query="
           + urllib.parse.quote("(LightSolver OR Optiphi)"))
    r = get(url, retries=2)
    out = []
    if not r:
        return out
    try:
        data = r.json()
    except Exception:
        return out
    for a in data.get("articles", []):
        title = a.get("title") or ""
        when = None
        try:
            when = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc)
        except Exception:
            pass
        out.append({"title": title, "link": a.get("url", ""), "text": title,
                    "when": when, "source": "GDELT"})
    return out


def yahoo_ticker(symbol):
    url = ("https://feeds.finance.yahoo.com/rss/2.0/headline?s="
           f"{symbol}&region=US&lang=en-US")
    r = get(url)
    return parse_rss(r.text, "Yahoo Finance") if r else []


def key_for(item):
    norm = re.sub(r"[^a-z0-9]", "", item["title"].lower())[:90]
    return hashlib.sha1(norm.encode()).hexdigest()


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    if isinstance(data, list):
        now = time.time()
        return {str(k): now for k in data}
    if isinstance(data, dict):
        return data
    return {}


def save_seen(seen):
    cutoff = time.time() - 30 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


def send(item):
    text = f"{item['title']}\n{item['source']}\n{item['link']}"
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=25,
    )
    if r.status_code == 429:
        time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)))
        return send(item)
    return r.ok


def main():
    first_run = not os.path.exists(SEEN_FILE)
    seen = load_seen()
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    found = []
    for q in QUERIES:
        found += google_news(q)
        found += bing_news(q)
        found += hacker_news(q)
    found += gdelt()
    for sym in ["CLGN", "OPHI"]:
        found += yahoo_ticker(sym)

    new = []
    batch_keys = set()
    for it in found:
        if not it["title"] or not it["link"]:
            continue
        if not relevant(it["text"]):
            continue
        if it["when"] and it["when"] < cutoff:
            continue
        k = key_for(it)
        if k in seen or k in batch_keys:
            continue
        batch_keys.add(k)
        new.append(it)

    new.sort(key=lambda i: i["when"] or datetime.now(timezone.utc))
    print(f"fetched {len(found)}, new relevant {len(new)}")

    if first_run:
        for it in new:
            seen[key_for(it)] = time.time()
        save_seen(seen)
        print("first run: seeded, nothing sent")
        return

    sent = 0
    for it in new:
        if sent >= MAX_ALERTS_PER_RUN:
            break
        if send(it):
            seen[key_for(it)] = time.time()
            sent += 1
            time.sleep(1.2)
    save_seen(seen)
    print(f"sent {sent}")


if __name__ == "__main__":
    main()
