import os
import re
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import requests
import feedparser

# ------------------------------------------------------------------
# SECRETS
# ------------------------------------------------------------------
BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TG_TOKEN")
)
CHAT_ID = (
    os.environ.get("TELEGRAM_CHAT_ID")
    or os.environ.get("CHAT_ID")
    or os.environ.get("TG_CHAT_ID")
)
if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit(
        "Missing Telegram token or chat id. Add the repository secrets "
        "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and make sure the workflow "
        "env block passes them in."
    )

SEND_TEST = os.environ.get("SEND_TEST", "").lower() == "true"

# ------------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------------
MAX_AGE_HOURS = 72          # ignore items older than this
MAX_INSTANT_PER_RUN = 12    # cap on instant alerts per run, overflow goes to the digest
DIGEST_HOUR_UTC = 6         # daily digest goes out after this UTC hour
STATE_DIR = "state"

# Tier 1: always sent instantly. These are the LightSolver side of the story
# and the upcoming new name and ticker.
TIER1_PATTERNS = [
    r"lightsolver",
    r"light solver",
    r"optiphi",
    r"\$ophi\b",
]

# Phrases removed before matching so they can never trigger anything
NOISE_PHRASES = [
    "oxford poverty and human development",
    "oxford poverty & human development",
    "oxford poverty",
]

# Words that show a post is about the LightSolver / optical computing / name change side.
# CollPlant, CLGN and a bare OPHI only count when one of these is also present.
LS_CONTEXT = [
    "lpu", "laser", "optical", "photonic", "photonics", "gpu", "optimization",
    "optimisation", "quantum", "ai chip", "ai computing", "computing",
    "merger", "business combination", "acquisition", "acquire", "spin off",
    "spin-off", "name change", "rename", "renamed", "rebrand", "new name",
    "new ticker", "ticker change", "reverse split", "pivot", "nasdaq",
    "optiphi", "ophi",
]

COLLPLANT_RE = re.compile(r"collplant")
CLGN_RE = re.compile(r"\bclgn\b")
OPHI_RE = re.compile(r"\bophi\b")

# Extra context that makes a bare "ophi" count (it is a short word, so be careful)
OPHI_CONTEXT = [
    "stock", "shares", "share price", "ticker", "nasdaq", "lightsolver",
    "lpu", "collplant", "clgn", "optical", "photonic", "price target",
]

# Sources. No Stocktwits on purpose.
REDDIT_QUERIES = [
    "LightSolver",
    "LightSolver CollPlant",
    "CollPlant LPU",
    "Optiphi",
    "$OPHI",
    "OPHI stock",
]
GOOGLE_NEWS_QUERIES = [
    "LightSolver",
    "LightSolver CollPlant",
    "CollPlant merger LightSolver",
    "Optiphi",
]

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


# ------------------------------------------------------------------
# CLASSIFICATION
# Returns:
#   1    send instantly (clear LightSolver / Optiphi / $OPHI match)
#   2    send instantly (CollPlant, CLGN or OPHI plus LightSolver-side context)
#   3    daily digest (bare OPHI with no context)
#   0    CollPlant or CLGN with no LightSolver context, treated as bio products and dropped
#   None no match at all
# ------------------------------------------------------------------
def classify(text):
    t = text.lower()
    for n in NOISE_PHRASES:
        t = t.replace(n, " ")

    for p in TIER1_PATTERNS:
        if re.search(p, t):
            return 1

    has_ls_context = any(c in t for c in LS_CONTEXT)

    if COLLPLANT_RE.search(t) or CLGN_RE.search(t):
        return 2 if has_ls_context else 0

    if OPHI_RE.search(t):
        if any(c in t for c in OPHI_CONTEXT):
            return 2
        return 3

    return None


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def fresh_stats():
    return {"checked": 0, "instant": 0, "digest": 0, "filtered": 0, "errors": {}}


def main():
    if SEND_TEST:
        send_telegram("CLGN monitor test: the bot is connected and working.")

    seen = load_json("seen.json", {})
    digest = load_json("digest.json", {
        "items": [],
        "last_digest_date": "",
        "stats": fresh_stats(),
    })
    stats = digest.setdefault("stats", fresh_stats())
    for k, v in fresh_stats().items():
        stats.setdefault(k, v)

    now = now_utc()
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    # prune seen ids older than 14 days
    keep_after = now - timedelta(days=14)
    seen = {
        k: v for k, v in seen.items()
        if datetime.fromisoformat(v) > keep_after
    }

    # collect from all sources, one failure never stops the others
    jobs = []
    for q in REDDIT_QUERIES:
        jobs.append((f"Reddit [{q}]", lambda q=q: fetch_reddit(q)))
    for q in GOOGLE_NEWS_QUERIES:
        jobs.append((f"News [{q}]", lambda q=q: fetch_google_news(q)))

    collected = {}
    for name, fn in jobs:
        try:
            for item in fn():
                collected[item["id"]] = item
        except Exception as e:
            print(f"{name} failed: {e}")
            stats["errors"][name] = stats["errors"].get(name, 0) + 1
        time.sleep(2)

    instant, to_digest = [], []
    for item in collected.values():
        if item["id"] in seen:
            continue
        pub = item["published"]
        if pub and pub < cutoff:
            continue
        stats["checked"] += 1
        tier = classify(item["text"])
        seen[item["id"]] = now.isoformat()
        if tier in (1, 2):
            instant.append(item)
        elif tier == 3:
            to_digest.append(item)
        elif tier == 0:
            stats["filtered"] += 1

    # newest first, cap instant alerts, overflow goes to the digest
    instant.sort(key=lambda i: i["published"] or now, reverse=True)
    overflow = instant[MAX_INSTANT_PER_RUN:]
    instant = instant[:MAX_INSTANT_PER_RUN]
    to_digest.extend(overflow)

    for item in instant:
        msg = f"[LightSolver] {item['source']}\n{item['title']}\n{item['url']}"
        send_telegram(msg)
        stats["instant"] += 1

    for item in to_digest:
        digest["items"].append({
            "source": item["source"],
            "title": item["title"][:200],
            "url": item["url"],
        })
        stats["digest"] += 1

    # daily digest plus heartbeat
    today = now.strftime("%Y-%m-%d")
    if now.hour >= DIGEST_HOUR_UTC and digest.get("last_digest_date") != today:
        lines = ["LightSolver monitor daily digest"]
        lines.append(
            f"Checked {stats['checked']} new items. "
            f"Sent {stats['instant']} instantly, "
            f"{stats['filtered']} CollPlant bio items filtered out, "
            f"{stats['digest']} loose matches below."
        )
        if stats["errors"]:
            errs = ", ".join(f"{k} x{v}" for k, v in stats["errors"].items())
            lines.append(f"Source errors: {errs}")
        else:
            lines.append("No source errors.")
        if digest["items"]:
            lines.append("")
            lines.append("Loose matches (bare 'ophi' with no stock context):")
            for it in digest["items"][:40]:
                lines.append(f"- {it['source']}: {it['title']}\n  {it['url']}")
        send_telegram("\n".join(lines))
        digest["items"] = []
        digest["last_digest_date"] = today
        digest["stats"] = fresh_stats()

    save_json("seen.json", seen)
    save_json("digest.json", digest)
    print(f"Done. instant={len(instant)} digest_added={len(to_digest)}")


if __name__ == "__main__":
    main()
