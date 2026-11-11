import os
import re
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import requests
import feedparser

# ------------------------------------------------------------------
# SETTINGS (edit these to tune how strict the screening is)
# ------------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MAX_AGE_HOURS = 72          # ignore items older than this
MAX_INSTANT_PER_RUN = 12    # cap on instant alerts per run, overflow goes to the digest
DIGEST_HOUR_UTC = 6         # daily digest goes out after this UTC hour (6 = 8am Israel in winter)
STATE_DIR = "state"

# Tier 1: unambiguous terms, always sent instantly
TIER1_PATTERNS = [
    r"lightsolver",
    r"light solver",
    r"collplant",
    r"\$clgn\b",
]

# Tier 2: bare "clgn" is sent instantly only if one of these words is also present
CLGN_BARE = re.compile(r"\bclgn\b")
CONTEXT_WORDS = [
    "stock", "shares", "share price", "biotech", "nasdaq", "bioink", "bio-ink",
    "lpu", "reverse split", "ticker", "pps", "price target", "warrant",
    "offering", "earnings", "rhcollagen", "collagen bioink", "breast implant",
    "optical", "gpu", "photonic",
]

# Anything that matches "clgn" but has no context word goes to the daily digest (tier 3)

REDDIT_QUERIES = ["LightSolver", "CollPlant", "CLGN stock", "$CLGN"]
GOOGLE_NEWS_QUERIES = ["CollPlant", "LightSolver", "CollPlant LightSolver"]
STOCKTWITS_SYMBOL = "CLGN"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; clgn-monitor/1.0)"}
TIMEOUT = 25


# ------------------------------------------------------------------
# STATE HELPERS
# ------------------------------------------------------------------
def load_json(name, default):
    path = os.path.join(STATE_DIR, name)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def now_utc():
    return datetime.now(timezone.utc)


def struct_to_dt(t):
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)


# ------------------------------------------------------------------
# TELEGRAM
# ------------------------------------------------------------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        try:
            r = requests.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                },
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                print("Telegram error:", r.status_code, r.text[:200])
        except Exception as e:
            print("Telegram exception:", e)
        time.sleep(1)


# ------------------------------------------------------------------
# SOURCES
# Each fetcher returns a list of dicts:
# {id, source, title, text, url, published}
# ------------------------------------------------------------------
def fetch_reddit(query):
    url = f"https://www.reddit.com/search.rss?q={quote_plus(query)}&sort=new&t=week"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Reddit HTTP {r.status_code}")
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        items.append({
            "id": "reddit:" + (e.get("id") or e.get("link", "")),
            "source": "Reddit",
            "title": e.get("title", ""),
            "text": (e.get("title", "") + " " + e.get("summary", "")),
            "url": e.get("link", ""),
            "published": struct_to_dt(e.get("updated_parsed") or e.get("published_parsed")),
        })
    return items


def fetch_google_news(query):
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Google News HTTP {r.status_code}")
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        items.append({
            "id": "gnews:" + (e.get("id") or e.get("link", "")),
            "source": "News",
            "title": e.get("title", ""),
            "text": (e.get("title", "") + " " + e.get("summary", "")),
            "url": e.get("link", ""),
            "published": struct_to_dt(e.get("published_parsed")),
        })
    return items


def fetch_stocktwits(symbol):
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Stocktwits HTTP {r.status_code}")
    data = r.json()
    items = []
    for m in data.get("messages", []):
        created = None
        try:
            created = datetime.strptime(
                m["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            pass
        user = m.get("user", {}).get("username", "user")
        body = m.get("body", "")
        items.append({
            "id": f"st:{m['id']}",
            "source": "Stocktwits",
            "title": f"{user}: {body[:200]}",
            # the stream is already about the symbol, so tag it as on-topic
            "text": body + f" ${symbol}",
            "url": f"https://stocktwits.com/{user}/message/{m['id']}",
            "published": created,
        })
    return items


#
