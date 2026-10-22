import os, json, re, time, urllib.parse, requests, feedparser
from concurrent.futures import ThreadPoolExecutor

# ---------- SETTINGS (edit this block only) ----------
LABEL = "[CLGN]"
TICKER = "CLGN"
TERMS = ["clgn", "collplant", "lightsolver"]
KEYWORDS = ['"CLGN"', '"$CLGN"', '"CollPlant"', '"LightSolver"']
HEBREW_KEYWORDS = ["קולפלנט"]
HASHTAGS = ["CLGN", "CollPlant", "LightSolver"]
SITE_QUERY = '(CollPlant OR CLGN OR LightSolver)'
TRIALS_TERM = "CollPlant"
PAPER_TERMS = ['"CollPlant"', '"rhCollagen"']
SITES = [
    "globenewswire.com", "prnewswire.com", "businesswire.com", "accessnewswire.com",
    "seekingalpha.com", "benzinga.com", "investing.com", "stocktitan.net",
    "tipranks.com", "marketbeat.com", "fiercebiotech.com", "biospace.com",
    "medtechdive.com", "globes.co.il", "calcalistech.com", "timesofisrael.com", "jpost.com",
]
NITTER_HOSTS = []
MASTODON = ["mastodon.social", "mastodon.online"]
EXTRA_FEEDS = []
MAX_SEND = 15
SEEN_FILE = "seen.json"
# ---------- END SETTINGS ----------

HEADERS = {"User-Agent": "mention-monitor/1.0 (contact: yishai.saltsman@gmail.com)"}

EDITIONS_MAIN = [("en-US", "US", "US:en"), ("en-IL", "IL", "IL:en")]
EDITIONS_EXTRA = [("en-GB", "GB", "GB:en"), ("en-CA", "CA", "CA:en"), ("en-AU", "AU", "AU:en")]


def gnews(query, hl="en-US", gl="US", ceid="US:en"):
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}+when:2d&hl={hl}&gl={gl}&ceid={ceid}"


def sources():
    for i, k in enumerate(KEYWORDS):
        q = urllib.parse.quote(k)
        editions = EDITIONS_MAIN + (EDITIONS_EXTRA if i < 2 else [])
        for hl, gl, ceid in editions:
            yield gnews(k, hl, gl, ceid), "rss", False
        yield f"https://www.bing.com/news/search?q={q}&format=rss", "rss", True
        yield f"https://news.search.yahoo.com/rss?p={q}", "rss", True
        yield f"https://www.reddit.com/search.rss?q={q}&sort=new", "rss", True
        yield f"https://hnrss.org/newest?q={q}", "rss", True
        yield f"https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={q}&sort=latest&limit=25", "bluesky", True
        for h in NITTER_HOSTS:
            yield f"https://{h}/search/rss?f=tweets&q={q}", "rss", True
    for k in HEBREW_KEYWORDS:
        yield gnews(k, "he", "IL", "IL:he"), "rss", False
    for tag in HASHTAGS:
        for inst in MASTODON:
            yield f"https://{inst}/tags/{tag}.rss", "rss", True
    for s in SITES:
        yield gnews(f"{SITE_QUERY} site:{s}"), "rss", False
    yield f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={TICKER}&region=US&lang=en-US", "rss", False
    yield f"https://www.nasdaq.com/feed/rssoutbound?symbol={TICKER}", "rss", False
    yield f"https://seekingalpha.com/api/sa/combined/{TICKER}.xml", "rss", False
    yield f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={TICKER}&type=&dateb=&owner=include&count=20&output=atom", "rss", False
    yield f"https://clinicaltrials.gov/api/v2/studies?query.term={urllib.parse.quote(TRIALS_TERM)}&pageSize=20&sort=LastUpdatePostDate:desc", "trials", False
    for t in PAPER_TERMS:
        yield f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={urllib.parse.quote(t + ' sort_date:y')}&format=json&pageSize=20", "papers", False
    for u in EXTRA_FEEDS:
        yield u, "rss", False


def norm(title):
    t = re.sub(r"\s+-\s+[^-]+$", "", title.lower())
    t = re.sub(r"[^\w]+", " ", t).strip()
    return t[:90]


def send(text):
    ok_any = False
    for chat in os.environ["TG_CHAT"].split(","):
        chat = chat.strip()
        if not chat:
            continue
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
                data={"chat_id": chat, "text": text[:3500]},
                timeout=20,
            )
            if not r.ok:
                print("telegram error:", chat, r.status_code, r.text[:200])
            ok_any = ok_any or r.ok
        except Exception as ex:
            print("telegram failed:", ex)
    return ok_any


def parse_bluesky(r):
    out = []
    for p in r.json().get("posts", []):
        handle = p.get("author", {}).get("handle", "")
        text = (p.get("record", {}).get("text") or "").replace("\n", " ")
        rkey = p.get("uri", "").split("/")[-1]
        out.append((f"Bluesky {handle}: {text[:200]}", f"https://bsky.app/profile/{handle}/post/{rkey}", text))
    return out


def parse_trials(r):
    out = []
    for s in r.json().get("studies", []):
        idm = s.get("protocolSection", {}).get("identificationModule", {})
        nct = idm.get("nctId")
        if nct:
            out.append((f"ClinicalTrials.gov: {idm.get('briefTitle', '')}", f"https://clinicaltrials.gov/study/{nct}", ""))
    return out


def parse_papers(r):
    out = []
    for a in r.json().get("resultList", {}).get("result", []):
        if a.get("id") and a.get("source"):
            out.append((f"Research: {a.get('title', '')}", f"https://europepmc.org/article/{a['source']}/{a['id']}", ""))
    return out


PARSERS = {"bluesky": parse_bluesky, "trials": parse_trials, "papers": parse_papers}


def fetch(src):
    url, kind, need_match = src
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if kind in PARSERS:
            entries = PARSERS[kind](r)
        else:
            feed = feedparser.parse(r.content)
            entries = [(e.get("title", ""), e.get("link", ""), e.get("summary", "")) for e in feed.entries]
        print(r.status_code, len(entries), url[:110])
        return need_match, entries
    except Exception as ex:
        print("failed:", url[:110], str(ex)[:80])
        return need_match, []


first_run = not os.path.exists(SEEN_FILE)
try:
    seen = set(json.load(open(SEEN_FILE))) if not first_run else set()
except Exception:
    seen = set()

found = []
batch = set()

with ThreadPoolExecutor(max_workers=16) as pool:
    results = list(pool.map(fetch, list(sources())))

for need_match, entries in results:
    for title, link, summary in entries:
        if not link:
            continue
        link = re.sub(r"^https?://[^/]*nitter[^/]*/", "https://x.com/", link).replace("#m", "")
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
