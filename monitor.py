import os, json, urllib.parse, requests, feedparser

LABEL = "[CLGN]"
KEYWORDS = ['"CLGN"', '"$CLGN"', '"CollPlant"', '"LightSolver"']
SEEN_FILE = "seen.json"
HEADERS = {"User-Agent": "mention-monitor/1.0 (contact: you@example.com)"}


def feed_urls():
    for k in KEYWORDS:
        q = urllib.parse.quote(k)
        yield f"https://news.google.com/rss/search?q={q}+when:1d&hl=en-US&gl=US&ceid=US:en"
        yield f"https://www.reddit.com/search.rss?q={q}&sort=new"


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TG_CHAT"], "text": text},
        timeout=20,
    )


first_run = not os.path.exists(SEEN_FILE)
try:
    seen = set(json.load(open(SEEN_FILE))) if not first_run else set()
except Exception:
    seen = set()
new_items = []

for url in feed_urls():
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        for e in feedparser.parse(r.content).entries:
            link = e.get("link")
            if link and link not in seen:
                seen.add(link)
                new_items.append(f"{LABEL} {e.get('title', '')}\n{link}")
    except Exception as ex:
        print("feed failed:", url, ex)

if not first_run:
    for item in new_items[:20]:
        send(item)

json.dump(sorted(seen), open(SEEN_FILE, "w"))
