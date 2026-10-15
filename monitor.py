import os, json, re, time, urllib.parse, requests, feedparser
from concurrent.futures import ThreadPoolExecutor

LABEL = "[CLGN]"
TICKER = "CLGN"
TERMS = ["clgn", "collplant", "lightsolver"]
KEYWORDS = ['"CLGN"', '"$CLGN"', '"CollPlant"', '"LightSolver"']
HEBREW_KEYWORDS = ["קולפלנט"]
SITES = [
    "globenewswire.com", "prnewswire.com", "businesswire.com", "accessnewswire.com",
    "seekingalpha.com", "benzinga.com", "investing.com", "fiercebiotech.com",
    "biospace.com", "globes.co.il", "calcalistech.com", "timesofisrael.com",
]
USE_STOCKTWITS = True
MAX_SEND = 15
SEEN_FILE = "seen.json"
HEADERS = {"User-Agent": "mention-monitor/1.0 (contact: yishai.saltsman@gmail.com)"}


def gnews(query, hl="en-US", gl="US", ceid="US:en"):
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}+when:2d&hl={hl}&gl={gl}&ceid={ceid}"


def sources():
    for k in KEYWORDS:
        q = urllib.parse.quote(k)
        yield gnews(k), "rss", False
        yield gnews(k, "en-IL", "IL", "IL:en"), "rss", False
        yield f"https://www.bing.com/news/search?q={q}&format=rss", "rss", True
        yield f"https://www.reddit.com/search.rss?q={q}&sort=new", "rss", True
        yield f"https://hnrss.org/newest?q={q}", "rss", True
    for k in HEBREW_KEYWORDS:
        yield gnews(k, "he", "IL", "IL:he"), "rss", False
    for s in SITES:
        yield gnews(f"(CollPlant OR CLGN) site:{s}"), "rss", False
    yield f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={TICKER}&region=US&lang=en-US", "rss", False
    yield f"https://www.nasdaq.com/feed/rssoutbound?symbol={TICKER}", "rss", False
    yield f"https://seekingalpha.com/api/sa/combined/{TICKER}.xml", "rss", False
    yield f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={TICKER}&type=&dateb=&owner=include&count=20&output=atom", "rss", False
    if USE_STOCKTWITS:
        yield f"https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json", "stocktwits", False


def norm(title):
    t = re.sub(r"\s+-\s+[^-]+$", "", title.lower())
    t = re.sub(r"[^\w]+", " ", t).strip()
    return t[:90]


def send(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
            data={"chat_id": os.environ["TG_CHAT"], "text": text[:3500]},
            timeout=20,
        )
        if not r.ok:
            print("telegram error:", r.status_code, r.text[:200])
        return r.ok
    except Exception as ex:
        print("telegram failed:", ex)
        return False


def parse_stocktwits(r):
    out = []
    for m in r.json().get("messages", []):
        user = m.get("user", {}).get("username", "")
        body = (m.get("body") or "").replace("\n", " ")
        out.append((f"Stocktwits {user}: {body[:200]}", f"https://stocktwits.com/message/{m['id']}", ""))
    return out


def fetch(src):
    url, kind, need_match = src
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if kind == "stocktwits":
            entries = parse_stocktwits(r)
        else:
            feed = feedparser.parse(r.content)
            entries = [(e.get("title", ""), e.get("link", ""), e.get("summary", "")) for e in feed.entries]
        print(r.status_code, len(entries), url)
        return need_match, entries
    except Exception as ex:
        print("failed:", url, ex)
        return need_match, []


first_run = not os.path.exists(SEEN_FILE)
try:
    seen = set(json.load(open(SEEN_FILE))) if not first_run else set()
except Exception:
    seen = set()

found = []
batch = set()

with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(fetch, list(sources())))

for need_match, entries in results:
    for title, link, summary in entries:
        if not link:
            continue
        if need_match and not any(t in (title + " " + summary).lower() for t in TERMS):
            continue
        n = norm(title)
        tkey = "t:" + n if n else link
        if link in seen or tkey in seen or link in batch or tkey in batch:
            continue
        batch.update([link, tkey])
        found.append((link, tkey, f"{LABEL} {title}\n{link}"))

if first_run:
    for link, tkey, _ in found:
        seen.update([link, tkey])
else:
    for link, tkey, text in found[:MAX_SEND]:
        if send(text):
            seen.update([link, tkey])
        time.sleep(1)

print("new items found:", len(found))
json.dump(sorted(seen), open(SEEN_FILE, "w"))
