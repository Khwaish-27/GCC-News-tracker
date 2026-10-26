"""
GCC News Tracker — a daily public page of Global Capability Centre news from around the world.

Source: Google News search feeds only (public, free, no API key). The searches live in
searches.txt. Each run fetches the last few days in the India and US editions, merges them
into data/articles.json (kept for 90 days), and rebuilds:

    index.html   the page (GitHub Pages serves it)
    feed.xml     RSS feed of the last 30 days

Usage:
    python build.py                  # daily run: last 3 days
    python build.py --backfill 60    # first run: fill the last 60 days, week by week

Python 3.11+, standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from xml.sax.saxutils import escape

# ── Page settings: edit these ─────────────────────────────────────
TITLE      = "GCC News Tracker"
AUTHOR     = ""   # e.g. "Compiled by Your Name" — shown at the bottom of the page
AUTHOR_URL = ""   # e.g. your LinkedIn profile link
SITE_URL   = "https://khwaish-27.github.io/GCC-News-tracker/"   # used in the RSS feed
# ──────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).resolve().parent
SEARCHES   = ROOT / "searches.txt"
STORE      = ROOT / "data" / "articles.json"
PAGE       = ROOT / "index.html"
RSS        = ROOT / "feed.xml"
KEEP_DAYS  = 90
DAILY_DAYS = 3
RSS_DAYS   = 30
STORY_GAP  = 7       # days: same company + same type of news within this gap = one story

GN_URL   = "https://news.google.com/rss/search"
EDITIONS = [{"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}, {"hl": "en-US", "gl": "US", "ceid": "US:en"}]
UK       = {"hl": "en-GB", "gl": "GB", "ceid": "GB:en"}   # extra edition for "world" searches
GN_CAP   = 100       # Google News returns at most this many results per search
GN_PAUSE = 1.5       # seconds between requests


# ═════════════════════════════════════════════════════════════════
# 1. FETCH
# ═════════════════════════════════════════════════════════════════

def _clean(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(str(s or "")))).strip()


def load_searches() -> list[dict]:
    out = []
    for line in SEARCHES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) != 3 or not parts[2]:
            raise ValueError(f"searches.txt: expected 'type | name | query', got: {line}")
        out.append({"type": parts[0], "name": parts[1], "query": parts[2]})
    return out


def _request(query: str, edition: dict) -> list[dict] | None:
    """One Google News RSS search. None = the request failed."""
    url = GN_URL + "?" + urlencode({"q": query, **edition})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; gcc-news-tracker)"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                root = ET.fromstring(r.read())
            break
        except urllib.error.HTTPError as e:
            if attempt == 3:
                print(f"    gave up ({e.code}) on: {query[:60]}", flush=True)
                return None
            time.sleep(30 * (attempt + 1) if e.code in (429, 503) else 3)
        except Exception:
            if attempt == 3:
                return None
            time.sleep(3)
    items = []
    for it in root.iter("item"):
        src = it.find("source")
        publisher = (src.text or "").strip() if src is not None else ""
        title = _clean(it.findtext("title"))
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc).date()
        except (TypeError, ValueError):
            continue
        items.append({"title": title, "url": it.findtext("link") or "", "date": d.isoformat(),
                      "publisher": publisher, "site": src.get("url", "") if src is not None else ""})
    return items


def _window(query: str, edition: dict, a: date, b: date, stats: dict) -> list[dict]:
    """Results dated a ≤ d < b, halving the window while Google's 100-result cap is hit."""
    raw = _request(f"{query} after:{a - timedelta(days=1)} before:{b + timedelta(days=1)}", edition)
    stats["requests"] += 1
    time.sleep(GN_PAUSE)
    if raw is None:
        stats["failed"] += 1
        return []
    inside = [i for i in raw if a.isoformat() <= i["date"] < b.isoformat()]
    if len(raw) >= GN_CAP and (b - a).days > 1 and len(inside) > len(raw) / 2:
        mid = a + (b - a) // 2
        return _window(query, edition, a, mid, stats) + _window(query, edition, mid, b, stats)
    return inside


def fetch(days: int) -> tuple[list[dict], dict]:
    end   = date.today() + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    weeks, w = [], start
    while w < end:
        weeks.append((w, min(w + timedelta(days=7), end)))
        w += timedelta(days=7)
    searches = load_searches()
    stats = {"requests": 0, "failed": 0}
    found = []
    for s in searches:
        for ed in EDITIONS + ([UK] if s["type"] == "world" else []):
            for a, b in weeks:
                for it in _window(s["query"], ed, a, b, stats):
                    it["search"] = s["name"]
                    found.append(it)
        print(f"  {s['name']}: done ({stats['requests']} requests so far)", flush=True)
    return found, stats


# ═════════════════════════════════════════════════════════════════
# 2. STORE (rolling 90 days)
# ═════════════════════════════════════════════════════════════════

def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())[:90]


def merge(new: list[dict]) -> list[dict]:
    old = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else []
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    by_key = {}
    for a in old + new:                       # first copy of a headline wins (keeps its date)
        if a["date"] < cutoff or not a["title"]:
            continue
        by_key.setdefault(_key(a["title"]), a)
    arts = sorted(by_key.values(), key=lambda a: a["date"], reverse=True)
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(arts, ensure_ascii=False, indent=0), encoding="utf-8")
    return arts


# ═════════════════════════════════════════════════════════════════
# 3. SORT EACH HEADLINE
# ═════════════════════════════════════════════════════════════════

_GCC = re.compile(
    r"\bgccs?\b|global capability|capability cent(?:re|er)s?|global in-house cent|captive (?:cent|unit|operation)|"
    r"shared services? cent|global business services|\bgbs\b|innovation cent(?:re|er)|"
    r"tech(?:nology)? cent(?:re|er)|engineering (?:cent(?:re|er)|hub)|development cent(?:re|er)|"
    r"r&d (?:cent(?:re|er)|hub)|global (?:delivery|technology|tech|operations|engineering) (?:cent(?:re|er)|hub)", re.I)
_LEADER_ROLE = re.compile(r"site leader|cent(?:re|er) head|gcc head|head of (?:the )?(?:india )?(?:gcc|global capability)", re.I)
# "GCC" is also the Gulf Cooperation Council
_GULF = re.compile(r"\bgulf\b|saudi|\buae\b|dubai|abu dhabi|qatar|kuwait|bahrain|\boman\b|riyadh|doha|sharjah|"
                   r"\bmena\b|middle east|\bregional\b[^.]*\bgcc\b|"
                   r"gcc (?:countries|region|states|nations|markets|economies|ministers|nationals|citizens|"
                   r"residents|visa|carriers|airlines|stock|banks|bourses|central banks|residential|real estate|"
                   r"property|retail|hospitality|tourism|aviation|oil|gas)", re.I)
_CAPABILITY = re.compile(r"capability|captive|shared services|business services", re.I)
_NOT_NEWS = re.compile(r"vacanc|hiring for (?:the )?(?:role|post|position)|apply now|walk-?in|\binternship\b|"
                       r"\(.*\b[A-Z]{2}\b.*\)$", re.I)
_GUIDE = re.compile(r"^(?:how to|a guide|guide to|complete guide|step-by-step)|\bguide\b.*\(|\(.*guide", re.I)
# "GCC" is also the Greater Chennai Corporation
_CIVIC = re.compile(r"monsoon|flood|\bdrains?\b|stormwater|garbage|sanitation|\bpark\b|\bward\b|councillor|mayor|"
                    r"property tax|relief cent|kitchens|\broads?\b|bus shelter|commissioner|mosquito|dengue|stray|"
                    r"cattle|encroach|chennai corporation|greater chennai|mylapore|adyar|velachery|t\.? nagar|"
                    r"anna nagar|tambaram|perambur|royapuram|corporation school|crematori|burial|zone \d", re.I)


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


# Eight categories in two groups — the way a GCC analyst would file the news.
TYPES = {
    "new":       {"group": "Company moves", "label": "New GCCs",
                  "hint": "Companies setting up a GCC, or announcing one"},
    "expansion": {"group": "Company moves", "label": "Expansions",
                  "hint": "New cities, campuses, headcount or capabilities at existing GCCs"},
    "leaders":   {"group": "Company moves", "label": "Leadership",
                  "hint": "Appointments and exits at GCCs"},
    "deals":     {"group": "Company moves", "label": "Deals & partnerships",
                  "hint": "GCCs bought, sold or transferred, and the partners that build them"},
    "cuts":      {"group": "Company moves", "label": "Cuts & closures",
                  "hint": "Layoffs, shutdowns and downsizing at GCCs"},
    "talent":    {"group": "Sector trends", "label": "Talent & jobs",
                  "hint": "Hiring, salaries, skills and workforce trends"},
    "policy":    {"group": "Sector trends", "label": "Policy & locations",
                  "hint": "Government policy, incentives, cities and office space"},
    "market":    {"group": "Sector trends", "label": "Market & research",
                  "hint": "Reports, surveys, market size and commentary"},
}

_LEAD_VERB = _rx(r"\bappoint\w*|\bnames?\b|\bnamed\b|\belevat\w+|\bjoins?\b|\bhires?\b(?! \d)|\bropes in\b|"
                 r"\btakes? (?:over|charge) as\b|\btakes charge\b|\bto (?:lead|head)\b|\bsteps? down\b|\bresign\w*|"
                 r"\bquits?\b|\bmoves on\b|\bnew (?:head|leader|md|ceo|chief)\b|\bpromot\w+|\bsucceeds?\b")
_LEAD_ROLE = _rx(r"\bhead\b|\bleader\b|\bceo\b|\bmd\b|managing director|\bpresident\b|\bdirector\b|\bchief\b|"
                 r"\bvp\b|vice president|country manager|\bto (?:lead|head)\b|\bcoo\b|\bcfo\b|\bcto\b|\bcio\b")
_CUTS = _rx(r"lay-?offs?\b|laid off|lays off|laying off|job cuts?|jobs? cut\b|\bcuts? \d[\d,]* (?:jobs|roles|"
            r"employees|staff|people)|\bcuts? jobs\b|retrench|redundanc|downsiz|\bshuts?\b|\bshutting\b|shut down|"
            r"\bcloses?\b(?! (?:deal|round|funding|in on))|\bclosure\b|winds? down|wind-down|scal(?:es|ing) back|"
            r"exits? india|pulls? out of|\bvrs\b")
_VENDOR = _rx(r"\bfor (?:\w+ (?:and|&) )?(?:mncs? (?:and|&) )?gccs?\b|\bgcc (?:banking|vertical|practice|solutions?|"
              r"services|platform|os|offering|desk|unit|suite|programme|program)\b|\bgccs? as a service\b")
_AGGREGATE = _rx(r"^(?:over |nearly |around |about )?\d[\d,.+%]*\s|\b\d{2,}\+? (?:new )?(?:gccs|centres|centers|"
                 r"companies|firms|mncs|japanese|us|german)\b|^india'?s gccs?\b|^india'?s\b")
_CAP_ADD = _rx(r"\bcoe\b|cent(?:re|er) of excellence|\blab\b|\bai hub\b|\bnew capabilit|\bpractice\b")
_SETUP = _rx(r"\bsets? up\b|\bsetting up\b|\bestablish(?:es|ed|ing)?\b|\binaugurat\w+|\bopens?\b|\bopened\b|"
             r"\blaunch(?:es|ed)?\b|\bunveil\w*|\bdebuts?\b|\bstarts? operations\b|\bbegins operations\b|"
             r"\bgoes live\b|\bfirst (?:gcc|global capability|india (?:gcc|centre|center)|tech(?:nology)? cent)|"
             r"\b(?:plans?|to) (?:open|set up|launch|establish)\b|\bmarks? (?:its )?entry\b|\benters? india\b")
_EXPAND = _rx(r"\bexpand(?:s|ed|ing)?\b|\bexpansion\b|\bscal(?:es|ing) up\b|\bramps? up\b|\bdoubles?\b|"
              r"\bsecond (?:gcc|cent(?:re|er)|campus|site|office|facility)\b|\bnew (?:campus|office|facility|site|"
              r"floor|tower)\b|\bbigger\b|\blarger (?:campus|office)\b|\badds? \d[\d,]* (?:jobs|seats|employees|"
              r"roles|people|engineers)\b|\bto hire \d|\bhire \d[\d,]*\b|\bheadcount to\b")
_DEALS = _rx(r"\bacquir\w*|\bacquisition\b|\btakes? over\b(?! as)|\btakeover\b|\bsells?\b|\bsold\b|\bdivest\w*|"
             r"\btransfer(?:s|red)? (?:of )?(?:its |the |a )?(?:gcc|global capability|cent(?:re|er)|captive|operations)|"
             r"build[- ]operate[- ]transfer|\bbot\b|\bpartner(?:s|ship|ed|ing)?\b|\bties? up\b|\btie-up\b|"
             r"\bjoint venture\b|\bhelps? (?:build|set up|scale|launch)\b|\bwins? (?:deal|contract|mandate)\b|"
             r"\bstake\b")
_TREND = _rx(r"\bboom\b|\brise\b|\bpowerhouse\b|\bdestination\b|\bgrowth story\b|\bwave\b|\bmomentum\b|\bera\b|"
             r"\bfuture of\b|\bgetting bigger\b")
_QUOTE = _rx(r"\bsays\b|\bsaid\b|\baccording to\b")
_STATE_START = r"(?:uttar pradesh|up|maharashtra|karnataka|telangana|tamil nadu|gujarat|kerala|andhra pradesh|" \
               r"odisha|haryana|punjab|rajasthan|madhya pradesh|west bengal|bihar|assam|j&k|meghalaya|goa)"
_POLICY = _rx(rf"^{_STATE_START}\b|\bpolic(?:y|ies)\b|\brules\b|\bincentive|\bsubsid|\bsummit\b|\bconclave\b|"
              r"\bminister|\bgovernment\b|\bgovt\b|\bcm\b|\bchief minister\b|\bmou\b|\binvites?\b|\bwoos?\b|"
              r"\battract\w*|\btargets? \d|\btier[- ](?:2|ii|two|3)\b|\bnon-metro|\bsmaller cities\b|\bleasing\b|"
              r"\blease[sd]?\b|\bsq\.? ?ft\b|\bsquare feet\b|\boffice (?:space|demand|market|absorption|stock)\b|"
              r"\breal estate\b|\bco-?working\b|\bflex(?:ible)? (?:space|office|workspace)|\bgift city\b|\bsez\b|"
              r"\bdata cent")
_TALENT = _rx(r"\bhiring\b|\bhires?\b|\bjobs?\b|\btalent\b|\bskills?\b|\bsalar(?:y|ies)\b|\bpay\b|\bwages?\b|"
              r"\bworkforce\b|\bemployees?\b|\bfreshers?\b|\bgraduates?\b|\bwomen\b|\bdiversity\b|\battrition\b|"
              r"\bupskill|\breskill|\bcareers?\b|\bprofessionals\b|\bcampus hiring\b|\bwork from|\bhybrid work\b|"
              r"\bcompensation\b|\bappraisal")
_OPINION = _rx(r"^opinion\b|^bs edit|\beditorial\b|\bshould\b|\bwhy\b|\bhow\b.*\?|\?$")


def _category(t: str, company: str) -> str:
    """One category per headline. Rules are checked in this order; the first that fits wins."""
    named = bool(company) and t.lower().startswith(company.lower())   # the company is the headline's subject
    if _LEAD_VERB.search(t) and _LEAD_ROLE.search(t):
        return "leaders"
    if _CUTS.search(t):
        return "cuts"
    if _GUIDE.search(t) or _VENDOR.search(t):
        return "market"
    if _POLICY.search(t) and not (named and _SETUP.search(t)):
        return "policy"
    if _AGGREGATE.search(t) and not named:
        return "talent" if _TALENT.search(t) else "market"
    # opinion, trend pieces and quotes are sector news, not a company's move
    commentary = _OPINION.search(t) or (_TREND.search(t) and not named) or (_QUOTE.search(t) and not _SETUP.search(t))
    if not commentary:
        if _DEALS.search(t):
            return "deals"
        if _CAP_ADD.search(t) and (named or _CITY.search(t)):
            return "expansion"
        if _SETUP.search(t):
            return "new"
        if _EXPAND.search(t) and (named or _CITY.search(t)):
            return "expansion"
    if _TALENT.search(t):
        return "talent"
    return "market"


# ── where the news is ─────────────────────────────────────────────

_INDIA = re.compile(r"\bindia\b|\bindian\b|bengaluru|bangalore|hyderabad|chennai|\bpune\b|mumbai|\bdelhi\b|noida|"
                    r"gurugram|gurgaon|kolkata|ahmedabad|kochi|coimbatore|jaipur|indore|chandigarh|"
                    r"thiruvananthapuram|visakhapatnam|vizag|bhubaneswar|mysuru|mysore|nagpur|vadodara|lucknow|"
                    r"gift city|karnataka|telangana|tamil nadu|maharashtra|kerala|gujarat|uttar pradesh|andhra|odisha|"
                    r"haryana|punjab|rajasthan|madhya pradesh|bihar|west bengal|uttarakhand|dehradun|j&k|jammu|"
                    r"kashmir|srinagar|goa|assam|guwahati|meghalaya|madras|\biit\b|nasscom|\bstpi\b|meity|madhapur|"
                    r"whitefield|hitec city|\bcrore\b|\blakh\b|\brs\.? ?\d", re.I)
_INDIA_CASED = re.compile(r"\bUP\b|₹|\bCM\b")
# Untagged headlines from Indian outlets are almost always about India
_INDIAN_PUB = re.compile(
    r"economic ?times|economictimes|\bet\b|livemint|\bmint\b|business standard|moneycontrol|businessline|"
    r"financial express|the hindu|times of india|hindustan times|indian express|ndtv|news18|deccan|telangana today|"
    r"yourstory|inc42|businessworld|\bbw\b|people matters|ethrworld|etcio|express computer|cxotoday|dataquest|\bdq\b|"
    r"india today|outlook|firstpost|asianet|the print|scroll|medianama|entrackr|analytics india|\baim\b|"
    r"indian masterminds|hrtoday|hr today|siliconindia|free press|mid-day|pioneer|tribune|the statesman|"
    r"business today|fortune india|forbes india|cnbc-?tv18|cnbctv18|zee|\bpti\b|\bians\b|devdiscourse|the federal|"
    r"south first|newsbytes|lokmat|odisha tv|orissapost|new indian express|rediff|thehansindia|hans india|"
    r"\.in$|\.co\.in$", re.I)

_PLACES_INDIA = [
    ("Bengaluru", r"bengaluru|bangalore|whitefield|manyata"), ("Hyderabad", r"hyderabad|madhapur|hitec city"),
    ("Chennai", r"chennai|madras"), ("Pune", r"pune"), ("Mumbai", r"navi mumbai|mumbai"), ("Noida", r"noida"),
    ("Gurugram", r"gurugram|gurgaon"), ("Delhi NCR", r"delhi|\bncr\b"), ("Kolkata", r"kolkata"),
    ("Ahmedabad", r"ahmedabad"), ("GIFT City", r"gift city"), ("Kochi", r"kochi"), ("Coimbatore", r"coimbatore"),
    ("Jaipur", r"jaipur"), ("Indore", r"indore"), ("Chandigarh", r"chandigarh|mohali"),
    ("Thiruvananthapuram", r"thiruvananthapuram|trivandrum"), ("Visakhapatnam", r"visakhapatnam|vizag"),
    ("Bhubaneswar", r"bhubaneswar"), ("Mysuru", r"mysuru|mysore"), ("Nagpur", r"nagpur"), ("Vadodara", r"vadodara"),
    ("Lucknow", r"lucknow"), ("Dehradun", r"dehradun"), ("Srinagar", r"srinagar"),
    ("Uttar Pradesh", r"uttar pradesh"), ("Maharashtra", r"maharashtra"), ("Karnataka", r"karnataka"),
    ("Telangana", r"telangana"), ("Tamil Nadu", r"tamil nadu"), ("Kerala", r"kerala"), ("Gujarat", r"gujarat"),
    ("Odisha", r"odisha"), ("Andhra Pradesh", r"andhra"), ("Haryana", r"haryana"), ("Punjab", r"punjab"),
    ("Rajasthan", r"rajasthan"), ("Madhya Pradesh", r"madhya pradesh"), ("West Bengal", r"west bengal"),
    ("Jammu & Kashmir", r"j&k|jammu|kashmir"), ("Meghalaya", r"meghalaya"), ("Assam", r"assam|guwahati"),
    ("Goa", r"\bgoa\b"),
]
_PLACES_INDIA = [(name, re.compile(rf"\b(?:{pat})\b", re.I)) for name, pat in _PLACES_INDIA]

_COUNTRIES = [
    ("Poland", r"poland|polish|krak[oó]w|warsaw|wroc[lł]aw|gda[nń]sk|[lł][oó]d[zź]|katowice"),
    ("Philippines", r"philippines|manila|cebu|makati|taguig"),
    ("Mexico", r"mexico|guadalajara|monterrey"), ("Costa Rica", r"costa rica"),
    ("Malaysia", r"malaysia|kuala lumpur|penang"), ("Vietnam", r"vietnam|ho chi minh|hanoi"),
    ("Egypt", r"egypt|cairo"), ("Portugal", r"portugal|lisbon|porto"), ("Romania", r"romania|bucharest|cluj"),
    ("Brazil", r"brazil|s[aã]o paulo|curitiba"), ("Colombia", r"colombia|bogot[aá]|medell[ií]n"),
    ("Sri Lanka", r"sri lanka|colombo"), ("Hungary", r"hungary|budapest"), ("Czechia", r"czech|prague|brno"),
    ("Ireland", r"ireland|dublin|cork"), ("Spain", r"spain|madrid|barcelona|m[aá]laga"),
    ("Morocco", r"morocco|casablanca"), ("South Africa", r"south africa|cape town|johannesburg"),
    ("Kenya", r"kenya|nairobi"), ("Argentina", r"argentina|buenos aires"), ("Chile", r"chile|santiago"),
    ("Singapore", r"singapore"), ("China", r"china|shanghai|dalian|chengdu"), ("Japan", r"japan|tokyo"),
    ("Indonesia", r"indonesia|jakarta"), ("Thailand", r"thailand|bangkok"), ("Bulgaria", r"bulgaria|sofia"),
    ("Serbia", r"serbia|belgrade"), ("Lithuania", r"lithuania|vilnius"), ("Turkey", r"turkey|t[uü]rkiye|istanbul"),
    ("Greece", r"greece|athens"), ("Saudi Arabia", r"saudi|riyadh"), ("UAE", r"\buae\b|dubai|abu dhabi"),
    ("Canada", r"canada|toronto"), ("Latin America", r"latin america|latam"),
    ("United States", r"\bus\b|u\.s\.|united states|(?<!latin )america\b|texas|dallas"), ("UK", r"\buk\b|britain|london|manchester|birmingham|belfast|glasgow|edinburgh|leeds"),
    ("Australia", r"australia|sydney|melbourne"), ("Germany", r"germany|berlin|munich"),
    ("Uruguay", r"uruguay|montevideo"), ("Guatemala", r"guatemala"), ("Nigeria", r"nigeria|lagos"),
    ("Europe", r"europe|european|\bcee\b"), ("Asia-Pacific", r"asia-pacific|asia pacific|\bapac\b|southeast asia"),
]
_COUNTRIES = [(name, re.compile(rf"\b(?:{pat})\b", re.I)) for name, pat in _COUNTRIES]
_CITY = re.compile(r"\b(?:bengaluru|bangalore|hyderabad|chennai|pune|mumbai|delhi|noida|gurugram|gurgaon|kolkata|"
                   r"ahmedabad|kochi|coimbatore|jaipur|indore|chandigarh|thiruvananthapuram|visakhapatnam|vizag|"
                   r"bhubaneswar|mysuru|mysore|nagpur|vadodara|lucknow|gift city|dehradun|krak[oó]w|warsaw|"
                   r"wroc[lł]aw|manila|cebu|guadalajara|monterrey|kuala lumpur|penang|ho chi minh|cairo|lisbon|porto|"
                   r"bucharest|cluj|bogot[aá]|medell[ií]n|colombo|budapest|prague|brno|m[aá]laga|casablanca|"
                   r"cape town)\b", re.I)


def _where(t: str, publisher: str) -> tuple[str, str]:
    """(tab, place): tab is "india" or "global"; place is a city, state or country, or ""."""
    if _INDIA.search(t) or _INDIA_CASED.search(t):
        place = next((n for n, rx in _PLACES_INDIA if rx.search(t)), "")
        if not place and re.search(r"\bUP\b", t):
            place = "Uttar Pradesh"
        return "india", place or "India"
    country = next((n for n, rx in _COUNTRIES if rx.search(t)), "")
    if country:
        return "global", country
    return ("india", "") if _INDIAN_PUB.search(publisher or "") else ("global", "")


def classify(a: dict) -> dict | None:
    """Category, tab, place and company for one headline — or None if it isn't GCC news."""
    t = a["title"]
    if not (_GCC.search(t) or _LEADER_ROLE.search(t)):
        return None
    if _GULF.search(t) and not _CAPABILITY.search(t) and not _INDIA.search(t):
        return None
    if _CIVIC.search(t) and not _CAPABILITY.search(t):
        return None
    if _NOT_NEWS.search(t):
        return None
    # a university or startup "innovation centre" isn't a GCC
    if re.search(r"universit|college|\bschool\b|students|incubat|startups?\b", t, re.I) \
            and not re.search(r"\bgccs?\b|capability|captive|shared services", t, re.I):
        return None
    company = company_of(t)
    tab, place = _where(t, a.get("publisher", ""))
    return {**a, "type": _category(t, company), "tab": tab, "place": place, "company": company}


# ── company name from the headline (no company list) ─────────────

_END = re.compile(
    r"(?:'s|’s)(?=\s)|[:;|]|\s(?=[$₹€£\d])|\s(?:to|will|has|have|had|is|are|was|plans?|announces?|appoints?|names?|"
    r"named|opens?|opened|launch(?:es|ed)?|inaugurates?|inaugurated|sets?|setting|establishes|unveils?|expands?|"
    r"expanding|adds?|picks|chooses|selects|bets|eyes|doubles|scales|starts|begins|debuts|goes|gets|hires?|"
    r"elevates?|ropes|in|on|for|at|with|amid|after|as|says?|said|GCC|GCCs|global|capability|India|new|first|"
    r"second|tech|technology|innovation|engineering|shared|centre|center|hub|campus|office)\b", re.I)
_DESCRIPTOR = re.compile(r"^(?:[A-Z][\w.]*-(?:based|headquartered|owned|backed)|American|German|British|Japanese|"
                         r"French|Swiss|Dutch|Danish|Swedish|Norwegian|Finnish|Irish|Australian|Canadian|Israeli|"
                         r"Korean|Singaporean|Italian|Spanish|European|Nordic|US|UK|Global|Fortune|Nasdaq-listed|"
                         r"NYSE-listed)$")
_NOT_COMPANY = {
    "the", "a", "an", "india", "india's", "indian", "indias", "gcc", "gccs", "global", "top", "new", "how", "why",
    "what", "when", "who", "this", "these", "report", "exclusive", "big", "tech", "it", "firm", "firms", "companies",
    "company", "employees", "govt", "government", "state", "states", "centre", "center", "cabinet", "budget",
    "minister", "cm", "it minister", "nasscom", "hiring", "jobs", "talent", "women", "ai", "genai", "more", "over",
    "here", "inside", "watch", "opinion", "explained", "analysis", "podcast", "news", "latest", "breaking", "us",
    "uk", "eu", "asia", "world", "year", "week", "today", "karnataka", "telangana", "tamil", "maharashtra", "kerala",
    "gujarat", "bengaluru", "bangalore", "hyderabad", "chennai", "pune", "mumbai", "delhi", "noida", "gurugram",
    "kolkata", "ahmedabad", "kochi", "coimbatore", "poland", "philippines", "mexico", "malaysia", "vietnam",
    "egypt", "portugal", "romania", "brazil", "colombia", "global capability", "capability", "survey", "study",
    "up", "uttar", "yogi", "nearly", "data", "healthcare", "japanese", "american", "german", "how", "smartworks",
    "enterprise", "opinion", "business",
}


def company_of(title: str) -> str:
    t = re.sub(r"^(?:[A-Z][\w.&-]*\s?){0,3}(?:exclusive|breaking|update|explained|watch|opinion)\s*[:|-]\s*",
               "", title.strip(), flags=re.I)
    t = re.sub(r"^[^:]{2,40} on (?:X|LinkedIn|Twitter)\s*:\s*", "", t)
    t = re.sub(r"^[^\w]+", "", t)
    m = _END.search(" " + t)
    if not m or m.start() == 0 or re.match(r"\s(?:says?|said)\b", m.group(0), re.I):
        return ""
    words = t[:max(m.start() - 1, 0)].strip(" ,:;-|'\"‘’“”").split()
    run = []
    for w in reversed(words):            # trailing run of capitalised words
        if re.match(r"[A-Z0-9]", w) or (run and w.lower() in {"of", "&", "and", "de"}):
            run.insert(0, w)
        else:
            break
    while run and (_DESCRIPTOR.match(run[0]) or run[0].lower() in {"of", "&", "and", "de"}):
        run.pop(0)
    lead = words[:len(words) - len(run)]
    if not run or len(run) > 4 or any(re.match(r"[A-Z]", w) for w in lead[1:]):
        return ""
    name = re.sub(r",?\s+(?:Inc|Ltd|Limited|LLC|PLC|plc|Corp|Corporation|AG|SE|GmbH|Group)\.?$", "", " ".join(run))
    name = re.sub(r"\s+India$", "", name).strip(" ,.")
    if not name or name.lower() in _NOT_COMPANY or name.split()[0].lower().strip("'’s") in _NOT_COMPANY \
            or re.fullmatch(r"[\d,.%+$₹]+.*", name):
        return ""
    return name


# ═════════════════════════════════════════════════════════════════
# 4. ONE ENTRY PER STORY
# ═════════════════════════════════════════════════════════════════

_MAJOR = re.compile(r"reuters|bloomberg|financial times|economic ?times|livemint|\bmint\b|business standard|"
                    r"moneycontrol|businessline|financial express|the hindu|times of india|cnbc|forbes|fortune|"
                    r"techcrunch|business insider|\bbbc\b|nikkei|wall street journal|\bwsj\b|yourstory|inc42|"
                    r"deccan herald|indian express|ndtv|hindustan times", re.I)


_STOP = {"the", "and", "for", "with", "its", "from", "into", "india", "indian", "india's", "gcc", "gccs", "global",
         "capability", "centre", "center", "centres", "centers", "new", "says", "report", "over", "more", "than",
         "after", "amid", "will", "has", "have", "are", "was", "this", "that", "how", "why", "what", "now", "set",
         "sets", "via", "per", "cent", "its", "their", "who", "about"}


def _tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in _STOP}


_COMMON_CAPS = {"the", "and", "for", "with", "how", "why", "what", "new", "first", "second", "india", "indian",
                "gcc", "gccs", "global", "capability", "centre", "center", "centres", "centers", "report", "ai",
                "tech", "technology", "innovation", "hub", "hubs", "jobs", "over", "top", "its", "india's", "opinion"}


def _names(title: str) -> set[str]:
    """Proper names in a headline (Infosys, Truist, Hyderabad) — skipping the first word and common words."""
    words = re.findall(r"[A-Za-z][\w&-]+", title)
    return {w.lower() for w in words if w[0].isupper() and len(w) > 2 and w.lower() not in _COMMON_CAPS}


def stories(arts: list[dict]) -> list[dict]:
    """Group headlines about the same news within a few days into one story.

    Same story = same company and type of news, or headlines that share most of
    their distinctive words ("100+ Japanese companies expand GCCs in India" ×3).
    """
    out = []
    for a in sorted(arts, key=lambda x: x["date"]):
        # first word of the company name, so "Thyssenkrupp Automotive" and "thyssenkrupp" match
        d, tok, co = date.fromisoformat(a["date"]), _tokens(a["title"]), a["company"].lower().split(" ")[0]
        names = _names(a["title"]) | ({co} if co else set())
        match = None
        for s in reversed(out):
            if (d - date.fromisoformat(s["last"])).days > STORY_GAP:
                continue
            if (co and co in s["companies"] and a["type"] in s["types"]) \
                    or len(names & s["names"]) >= 2 \
                    or (tok and max(len(tok & t) / len(tok | t) for t in s["toks"] if t) >= 0.45):
                match = s
                break
        if match:
            match["arts"].append(a)
            match["last"] = a["date"]
            match["toks"].append(tok)
            match["types"].add(a["type"])
            match["names"] |= names
            if co:
                match["companies"].add(co)
        else:
            out.append({"last": a["date"], "arts": [a], "toks": [tok or {""}], "types": {a["type"]},
                        "names": set(names), "companies": {co} if co else set()})
    result = []
    for s in out:
        for a in s["arts"]:              # Google sometimes gives a web address as the publisher
            a["publisher"] = re.sub(r"^https?://(?:www\.)?|/+$", "", a["publisher"])
        arts_ = sorted(s["arts"], key=lambda a: (not _MAJOR.search(a["publisher"]), a["date"]))
        lead = arts_[0]
        result.append({
            "id": hashlib.sha1(_key(lead["title"]).encode()).hexdigest()[:12],
            "date": min(a["date"] for a in s["arts"]),
            "type": lead["type"],
            # a story is Indian if any headline about it is; the most specific place wins
            "tab": "india" if any(a["tab"] == "india" for a in arts_) else "global",
            "place": next((a["place"] for a in arts_ if a["place"] and a["place"] != "India"),
                          next((a["place"] for a in arts_ if a["place"]), "")),
            "company": lead["company"],
            "title": lead["title"], "url": lead["url"], "publisher": lead["publisher"],
            "more": [{"publisher": a["publisher"], "url": a["url"], "title": a["title"]} for a in arts_[1:6]],
            "count": len(arts_),
        })
    return sorted(result, key=lambda s: (s["date"], s["count"]), reverse=True)


# ═════════════════════════════════════════════════════════════════
# 5. OUTPUT
# ═════════════════════════════════════════════════════════════════

def _fmt(d: str) -> str:
    dt = date.fromisoformat(d)
    return f"{dt.day} {dt.strftime('%b %Y')}"


def write_rss(items: list[dict]) -> None:
    since = (date.today() - timedelta(days=RSS_DAYS)).isoformat()
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           "<channel>", f"<title>{escape(TITLE)}</title>", f"<link>{escape(SITE_URL or 'https://news.google.com/')}</link>",
           "<description>Global Capability Centre (GCC) news from around the world, updated daily.</description>",
           "<language>en</language>", f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>"]
    if SITE_URL:
        out.append(f'<atom:link href="{escape(SITE_URL.rstrip("/") + "/feed.xml")}" rel="self" type="application/rss+xml"/>')
    for s in [s for s in items if s["date"] >= since][:300]:
        label = TYPES[s["type"]]["label"]
        title = f"[{label}] " + (f"{s['company']}: " if s["company"] else "") + s["title"]
        when  = format_datetime(datetime.fromisoformat(s["date"]).replace(tzinfo=timezone.utc))
        out += ["<item>", f"<title>{escape(title)}</title>", f"<link>{escape(s['url'])}</link>",
                f'<guid isPermaLink="false">{s["id"]}</guid>', f"<pubDate>{when}</pubDate>",
                f"<category>{escape(label)}</category>",
                f"<description>{escape(s['publisher'] + (' · ' + s['place'] if s['place'] else ''))}</description>",
                "</item>"]
    out += ["</channel>", "</rss>", ""]
    RSS.write_text("\n".join(out), encoding="utf-8")


def write_page(items: list[dict], checked: int) -> None:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    data = {
        "title": TITLE, "author": AUTHOR, "authorUrl": AUTHOR_URL,
        "updated": f"{now.day} {now.strftime('%b %Y, %H:%M')} IST",
        "today": now.date().isoformat(),
        "checked": checked,
        "types": [{"key": k, "label": v["label"], "hint": v["hint"], "group": v["group"]} for k, v in TYPES.items()],
        "stories": items,
    }
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    PAGE.write_text(TEMPLATE.replace("__TITLE__", html.escape(TITLE)).replace("/*__DATA__*/null", blob),
                    encoding="utf-8")


TEMPLATE = (ROOT / "template.html").read_text(encoding="utf-8") if (ROOT / "template.html").exists() else ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the GCC News Tracker page from Google News.")
    ap.add_argument("--backfill", type=int, default=0, metavar="DAYS",
                    help="fetch this many past days, week by week (for the first run)")
    ap.add_argument("--no-fetch", action="store_true", help="rebuild the page from stored articles only")
    args = ap.parse_args()

    stats = {"requests": 0, "failed": 0}
    new = []
    if not args.no_fetch:
        days = args.backfill or DAILY_DAYS
        print(f"Fetching the last {days} days from Google News", flush=True)
        new, stats = fetch(days)
    arts = merge(new)
    print(f"{len(arts):,} headlines stored ({len(new):,} fetched now, {stats['requests']} requests, "
          f"{stats['failed']} failed)", flush=True)

    sorted_ = [c for c in (classify(a) for a in arts) if c]
    items = stories(sorted_)
    write_page(items, len(arts))
    write_rss(items)
    counts = {k: sum(1 for s in items if s["type"] == k) for k in TYPES}
    print(f"{len(sorted_):,} GCC headlines → {len(items):,} stories "
          + ", ".join(f"{TYPES[k]['label']}: {n}" for k, n in counts.items()))
    print(f"Wrote {PAGE.name} and {RSS.name}")


if __name__ == "__main__":
    main()
