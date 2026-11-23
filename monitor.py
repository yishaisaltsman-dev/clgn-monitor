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


def relevant(text: str) -> bool:
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
