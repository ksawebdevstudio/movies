#!/usr/bin/env python3
"""Personal aggregator: what is playing TODAY in four Coachella Valley theaters
(+ upcoming US releases). Data: the theaters' own websites, posters/ratings: TMDB.

  TMDB_API_KEY=... python build.py     -> site/index.html
  python build.py --mock               -> same, from built-in sample data (no network)

Standard library only. Each theater is fetched independently: if one fails, the
page is still built and shows a notice. If all of them fail, the script exits with
an error so the previously published page stays online.
"""
import base64
import gzip
import html
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
UA = "Mozilla/5.0 (compatible; PersonalShowtimesPage/1.0)"
TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w342"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "index.html")
# Problems found during a run (not published). The workflow reads it and marks the run as failed.
PROBLEMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "problems.txt")

HIDE_NON_IMAX_3D = True   # you only watch 3D in IMAX, so other 3D showtimes are dropped
MAX_UPCOMING = 12        # how many "coming soon" cards
UPCOMING_DAYS = 28       # only releases within this many days from today
MIN_SHOWTIMES = 3
RERELEASE_DAYS = 548   # first released more than ~18 months ago -> "Re-release" (festival films often open a year late)
# not films: opera/theatre broadcasts, fights, concerts. Strong words mark an event always; weak ones only
# when TMDB did not recognise the title (so a film such as "The Phantom of the Opera" stays a film).
EVENT_STRONG = re.compile(r"\b(met(ropolitan)? opera|national theatre|nt live|bolshoi|royal ballet|ufc|wwe|aew|"
                          r"fathom events?|in concert|special event|live broadcast)\b", re.I)
# a theater's own listing for a special showing of a film, e.g. "Sunrise Screening - The Hunger Games: ..."
SPECIAL_SHOW = re.compile(r"^[\w' ]{1,24}\bScreenings?\s+-\s+\S", re.I)
EVENT_WEAK = re.compile(r"\b(opera|ballet|concert|live|encore|tour|fight night|boxing|trivia|q ?& ?a|"
                        r"marathon|double feature|festival)\b", re.I)        # fewer showtimes than this for a theater on a day = probably broken, raise an alert

# Order = display order on the cards.
# Default order: west to east along the valley (Palm Springs -> La Quinta); the page re-sorts by distance
# when the visitor allows location.
THEATERS = [
    ("festival", "Festival Theaters"),
    ("pickford", "Mary Pickford Theatre"),
    ("cinemark", "Cinemark Century at the River + XD"),
    ("regal", "Regal Rancho Mirage"),
    ("laquinta", "Cinemark Century La Quinta XD + ScreenX"),
]
# Approximate coordinates (lat, lon) for the distance sort.
THEATER_GEO = {   # theater addresses, rounded to 4 decimals (about 30 ft); distances are shown to 0.1 mi at best
    "festival": (33.8228, -116.5398),   # 789 E Tahquitz Canyon Way, Palm Springs
    "pickford": (33.7811, -116.4631),   # 36850 Pickfair St, Cathedral City
    "cinemark": (33.7396, -116.4095),   # 71800 Hwy 111, Rancho Mirage (The River)
    "regal": (33.7998, -116.3927),      # 72777 Dinah Shore Dr, Rancho Mirage
    "laquinta": (33.7089, -116.2927),   # 46800 Washington St, La Quinta
}
THEATER_NAMES = dict(THEATERS)
THEATER_SLUG = {"festival": "festival-theaters", "pickford": "mary-pickford", "cinemark": "cinemark-rancho-mirage",
                "regal": "regal-rancho-mirage", "laquinta": "cinemark-la-quinta"}   # page address /movies/<slug>/
SHORT_NAMES = {"festival": "Festival", "cinemark": "Cinemark XD", "regal": "Regal", "pickford": "Mary Pickford", "laquinta": "La Quinta"}  # used on phones
# Where the "open showtimes" link goes when a theater could not be read.
THEATER_URLS = {
    "festival": "https://festivaltheaters.com/buy-tickets/",
    "cinemark": "https://www.cinemark.com/theatres/ca-rancho-mirage/cinemark-century-at-the-river-and-xd",
    "regal": "https://www.regmovies.com/theatres/regal-rancho-mirage-1801",
    "pickford": "https://www.dplaceentertainment.com/california/cathedral-city/0000000673/showtimes",
    "laquinta": "https://www.cinemark.com/theatres/ca-la-quinta/cinemark-century-la-quinta-xd-and-screenx",
}
THEATER_ADDR = {
    "festival": "789 E Tahquitz Canyon Way, Palm Springs",
    "cinemark": "71800 Hwy 111, Suite A-137, Rancho Mirage",
    "regal": "72-777 Dinah Shore Dr, Rancho Mirage",
    "pickford": "36850 Pickfair St, Cathedral City",
    "laquinta": "46800 Washington St, La Quinta",
}
TMDB_GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
               18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
               9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 10770: "TV Movie", 53: "Thriller",
               10752: "War", 37: "Western"}
NOTES = []  # extra footer lines (e.g. which backup source was used)

CINEMARK_ID = 479
CINEMARK_LQ_ID = 1126   # Cinemark Century La Quinta XD and SCREENX (from cinemark.com/theatre-1126)
CINEMARK_BASE = "https://www.cinemark.com"
REGAL_BASE = "https://www.regmovies.com"
REGAL_SITE = "1801"
REGAL_THEATRE = f"{REGAL_BASE}/theatres/regal-rancho-mirage-{REGAL_SITE}"
BIGSCREEN_REGAL = "https://www.bigscreen.com/Marquee.php?theater=6355&view=sched&showdate={date}"
# Mary Pickford: BigScreen is the main source (it marks D-BOX showings and already works from GitHub);
# the theater's own D'Place page gives direct movie links, a cross-check and a backup.
BIGSCREEN_PICKFORD = "https://www.bigscreen.com/Marquee.php?theater=7873&view=sched&showdate={date}"
DPLACE_BASE = "https://www.dplaceentertainment.com"
# Visit statistics (GoatCounter: no cookies, no personal data). Put the site code here (the part before
# ".goatcounter.com"). While it is empty the page has no statistics script and sends nothing anywhere.
GOATCOUNTER_CODE = "geniusgorilla"
SITE_URL = "https://ksawebdevstudio.github.io/movies/"   # canonical address of the page
GSC_VERIFY = ""    # Google Search Console "HTML tag" token (content value only); empty = no tag
GSC_VERIFY_FILE = "google4b07d58ef1fdd6e8.html"   # Search Console file verification, served next to index.html
BING_VERIFY = ""   # Bing Webmaster Tools msvalidate.01 token; empty = no tag
DPLACE_THEATRE_ID = "0000000673"
# D'Place answered GitHub's server with "405 Not Allowed" while the default Accept header was sent;
# these are the ordinary headers of a page request (same honest User-Agent).
DPLACE_HEADERS = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9",
                  "Upgrade-Insecure-Requests": "1"}
FESTIVAL_BASE = "https://festivaltheaters.com"
FESTIVAL_SITE_ID = "177"
FESTIVAL_CIRCUIT_ID = "106"


# ---------------------------------------------------------------- http helpers
def http(url, data=None, headers=None, tries=3):
    """GET/POST with gzip, a 20 s timeout and retries only for errors that can go away
    (network trouble, 5xx, 429). A 403/404 fails at once instead of wasting seconds."""
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = f"HTTP Error {e.code}: {e.reason}"
            try:
                e.close()      # Python 3.14 prints "I/O operation on closed file" at exit otherwise
            except Exception:  # noqa: BLE001
                pass
            if e.code < 500 and e.code not in (408, 429):
                break
        except Exception as e:  # noqa: BLE001
            last = e
        if i < tries - 1:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def pmap(fn, items, workers=8):
    """Run fn over items in parallel threads; results in input order."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


def la(dt_utc):
    return dt_utc.astimezone(TZ)


def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# ------------------------------------------------------------ theater parsers
# Each parser returns a list of dicts:
#   {theater, title, ts (aware datetime), fmt ("" | "IMAX" | "D-BOX"), url, tmdb_id (int|None)}

def fmt_of(attrs, imax):
    """The one format worth a tag on a showtime: IMAX, else D-BOX (motion seats), else nothing."""
    if imax:
        return "IMAX"
    return "D-BOX" if re.search(r"d-?\s?box", attrs, re.I) else ""


def parse_cinemark(data, today, key="cinemark", tid=None):
    tid = tid or CINEMARK_ID
    out = []
    for m in data:
        # The movie page of cinemark.com lists several theaters and asks where you are, so the colored block
        # opens this theater's own page instead; each time opens that showing's seat map at this theater.
        url = THEATER_URLS[key]
        movie_id = m.get("cinemarkMovieId") or m.get("movieId")
        rt = m.get("movieRuntimeMinutes") or None
        genre = m.get("movieGenre") or None
        poster = m.get("moviePosterUrl") or None
        if poster and poster.startswith("/"):
            poster = CINEMARK_BASE + poster
        for pt in m.get("showtimesByPrintType", []):
            name = (pt.get("printTypeName") or "").lower()
            imax = "imax" in name
            if "3d" in name and not imax and HIDE_NON_IMAX_3D:
                continue
            for s in pt.get("showtimes", []):
                ts = la(parse_utc(s["startTime"]))
                if ts.strftime("%Y-%m-%d") == today:
                    buy = None
                    if movie_id and s.get("id"):
                        buy = f"{CINEMARK_BASE}/TicketSeatMap/?" + urllib.parse.urlencode(
                            {"cinemarkMovieId": movie_id, "linkedShowtimeId": s.get("linkedShowtimeId") or 0,
                             "showtime": ts.strftime("%Y-%m-%dT%H:%M:%S"), "showtimeId": s["id"],
                             "theaterId": tid})
                    out.append(dict(theater=key, title=m.get("movieTitle", ""), ts=ts,
                                    fmt=fmt_of(name, imax), url=url, buy=buy, tmdb_id=None, poster=poster, rt=rt,
                                    genre=genre))
    return out


def slugify(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def parse_regal(page_html, today):
    mm = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
    if not mm:
        t = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S | re.I)
        raise RuntimeError(f"Regal: __NEXT_DATA__ not found (page title: {t.group(1).strip()[:80] if t else 'none'}, {len(page_html)} bytes)")
    days = json.loads(mm.group(1))["props"]["pageProps"]["showtimes"]
    links = {code: path for path, code in
             re.findall(r'href="(/movies/[a-z0-9\-]*?-(ho\d+))"', page_html)}
    out = []
    for day in days:
        if str(day.get("AdvertiseShowDate", ""))[:10] != today:
            continue
        for film in day.get("Film", []):
            title = film.get("Title", "")
            code = (film.get("MasterMovieCode") or "").lower()
            path = links.get(code) or f"/movies/{slugify(title)}-{code}"
            for p in film.get("Performances", []):
                attrs = [a.lower() for a in p.get("PerformanceAttributes", [])]
                imax = "imax" in attrs or title.upper().startswith("IMAX")
                if "3d" in attrs and not imax and HIDE_NON_IMAX_3D:
                    continue
                naive = datetime.fromisoformat(p["CalendarShowTime"])
                ts = naive.replace(tzinfo=TZ)
                buy = None
                if p.get("PerformanceId") and not p.get("StopSales"):
                    buy = (f"{REGAL_BASE}{path}?site={REGAL_SITE}&id={p['PerformanceId']}"
                           f"&date={ts.strftime('%m-%d-%Y')}")
                out.append(dict(theater="regal", title=title, ts=ts, fmt=fmt_of(" ".join(attrs), imax),
                                url=THEATER_URLS["regal"], buy=buy, tmdb_id=None))
    return out


def parse_bigscreen(page_html, today, theater="regal", url=None):
    """BigScreen Cinema Guide theater schedule (plain HTML): backup for Regal, main source for Mary Pickford."""
    url = url or THEATER_URLS[theater]
    rows = re.findall(r'<tr class="graybar_\d">(.*?)</tr>', page_html, re.S)
    if not rows:
        t = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S | re.I)
        raise RuntimeError(f"BigScreen: no schedule rows (page title: {t.group(1).strip()[:80] if t else 'none'})")
    want = datetime.strptime(today, "%Y-%m-%d").strftime("%B %-d, %Y")
    head = re.search(r"Showtimes on \w+, ([A-Z][a-z]+ \d{1,2}, \d{4})", page_html)
    if not head or head.group(1) != want:
        raise RuntimeError(f"BigScreen: page is for '{head.group(1) if head else 'unknown date'}', wanted {want}")
    out = []
    for row in rows:
        m = re.search(r'class="movieNameList">([^<]+)<', row)
        cell = row.split('class="col_showtimes"', 1)
        if not m or len(cell) < 2:
            continue
        title = html.unescape(m.group(1)).strip()
        rm = re.search(r"Running Time: (\d+):(\d\d)", row)
        rt = int(rm.group(1)) * 60 + int(rm.group(2)) if rm else None
        ym = re.search(r'<span class="small">((?:19|20)\d\d) - </span>', row)
        year = ym.group(1) if ym else None
        pm = re.search(r"MovieGraphics/thumbs/(\d+)\.jpg", row)
        poster = f"https://www.bigscreenimages.com/Graphics/MovieGraphics/{pm.group(1)}.jpg" if pm else None
        # the cell is a sequence of groups: times..., then <span class="showcomment">attributes</span>
        for group in re.split(r'(?<=</span>)', cell[1]):
            c = re.search(r'class="showcomment">([^<]*)<', group)
            attrs = (c.group(1) if c else "").lower()
            imax = "imax" in attrs or "imax" in title.lower()
            is3d = "3d projection" in attrs or re.search(r"\b3d\b", title, re.I)
            if is3d and not imax and HIDE_NON_IMAX_3D:
                continue
            # times are plain text like "10:50a, 1:40, 3:30" ("a" = AM, everything else PM); some are links
            text = group.split('class="showcomment"')[0]
            text = re.sub(r"<!--.*?-->|<div.*?</div>|<[^>]+>", " ", text, flags=re.S)
            for hh, mm, suf in re.findall(r"(?<![\d:])(\d{1,2}):(\d\d)([ap])?(?![\d:])", text):
                hour = int(hh) % 12 + (0 if suf == "a" else 12)
                if suf == "a" and hour < 4:
                    continue  # after-midnight show, belongs to the next calendar day
                ts = datetime.strptime(f"{today} {hour}:{mm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                out.append(dict(theater=theater, title=title, ts=ts, fmt=fmt_of(attrs, imax), url=url, tmdb_id=None,
                                poster=poster, year=year, rt=rt))
    return out


def parse_festival(data, today):
    rows = (((data or {}).get("data") or {}).get("showingsForDate") or {}).get("data") or []
    out = []
    for s in rows:
        if s.get("published") is False or s.get("private"):
            continue
        mv = s.get("movie") or {}
        ts = la(parse_utc(s["time"]))
        if ts.strftime("%Y-%m-%d") != today:
            continue
        tid = mv.get("tmdbId")
        pimg = mv.get("posterImage")
        poster = (f"https://indy-systems.imgix.net/{pimg}?fit=crop&w=400&h=600&fm=jpeg&auto=format,compress"
                  if pimg else None)
        out.append(dict(theater="festival", title=mv.get("name", ""), ts=ts, fmt="",
                        url=f"{FESTIVAL_BASE}/movie/{mv.get('urlSlug', '')}/",
                        tmdb_id=int(tid) if tid and str(tid).isdigit() else None, poster=poster,
                        rt=mv.get("duration") or None, genre=mv.get("genre") or None))
    return out


# --------------------------------------------------------------- live fetchers
def fetch_cinemark(today):
    q = urllib.parse.urlencode({"date": today, "timeZone": "America/Los_Angeles"})
    return parse_cinemark(json.loads(http(f"{CINEMARK_BASE}/papi/theaters/{CINEMARK_ID}/showtimes?{q}")), today)


def fetch_laquinta(today):
    q = urllib.parse.urlencode({"date": today, "timeZone": "America/Los_Angeles"})
    return parse_cinemark(json.loads(http(f"{CINEMARK_BASE}/papi/theaters/{CINEMARK_LQ_ID}/showtimes?{q}")), today,
                          key="laquinta", tid=CINEMARK_LQ_ID)


def fetch_regal(day, official=True):
    """Regal's own theatre page first (it only holds today's schedule); if it refuses this
    server or has nothing, use BigScreen's listing for the same theater."""
    e1 = "skipped (the theatre page only lists today)"
    if official:
        try:
            got = parse_regal(http(REGAL_THEATRE, tries=2), day)
            if got:
                return got
            e1 = "no showtimes for that day on the theatre page"
        except Exception as e:  # noqa: BLE001
            e1 = e
        print(f"  Regal official site: {e1}; trying BigScreen")
    try:
        got = parse_bigscreen(http(BIGSCREEN_REGAL.format(date=day), tries=2), day)
    except Exception as e2:  # noqa: BLE001
        raise RuntimeError(f"regmovies.com: {e1} | bigscreen.com: {e2}") from None
    if not got:
        raise RuntimeError(f"regmovies.com: {e1} | bigscreen.com: page loaded but no showtimes for {day}")
    if official:
        NOTES.append("Regal showtimes via BigScreen Cinema Guide (bigscreen.com); Regal's own site could not be read, so its times open the theater's page.")
    return got


def fetch_festival(today):
    query = ("query($date:String,$siteIds:[ID]){ showingsForDate(date:$date, siteIds:$siteIds)"
             "{ data { time published private movie { name urlSlug tmdbId posterImage duration genre } } } }")
    raw = http(f"{FESTIVAL_BASE}/graphql",
               data={"query": query, "variables": {"date": today, "siteIds": [FESTIVAL_SITE_ID]}},
               headers={"Content-Type": "application/json", "site-id": FESTIVAL_SITE_ID,
                        "circuit-id": FESTIVAL_CIRCUIT_ID, "client-type": "consumer"})
    data = json.loads(raw)
    if data.get("errors"):
        raise RuntimeError(f"Festival GraphQL: {data['errors'][0].get('message')}")
    return parse_festival(data, today)


def fetch_pickford(day):
    got = parse_bigscreen(http(BIGSCREEN_PICKFORD.format(date=day), tries=2), day, theater="pickford")
    if not got:
        raise RuntimeError(f"bigscreen.com: page loaded but no showtimes for {day}")
    return got


def _strip(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_dplace(page_html):
    """Mary Pickford's own showtimes page (six days on one page). Returns
    {"links": {norm title: movie page url}, "days": {date: [(title, ts)]}, "how": how the times were read,
     "tickets": [(title, ts, ticket url)]}.
    The times are read two ways (schema.org events, else the visible day sections); "days" stays
    empty when neither matches, so a redesign of the site can never put wrong times on the page."""
    links, days, how = {}, {}, ""
    tickets = []
    # ticket links: <a href=".../Tickets/<theater>/<id>/<id>" aria-label="Buy tickets for TITLE at 11:00 AM">
    #               <time datetime="2026-11-15T11:00:00-08:00">11:00 AM</time></a>
    for href, label, dt in re.findall(
            r'<a\b[^>]*href="([^"]*/Tickets/[^"]+)"[^>]*aria-label="Buy tickets for ([^"]+?) at [^"]*"[^>]*>\s*'
            r'<time[^>]*datetime="([^"]+)"', page_html, re.S):
        try:
            tts = datetime.fromisoformat(dt)
            tts = tts.replace(tzinfo=TZ) if tts.tzinfo is None else tts.astimezone(TZ)
        except ValueError:
            continue
        tickets.append((html.unescape(label), tts, urllib.parse.urljoin(DPLACE_BASE, html.unescape(href))))

    def add_link(title, url):
        if title and url and f"/{DPLACE_THEATRE_ID}/movies/" in url:
            links.setdefault(norm(title), urllib.parse.urljoin(DPLACE_BASE, url))

    # 1) movie links: <a href=".../0000000673/movies/<slug>">Title or <img alt="Title"></a>
    for href, slug, inner in re.findall(
            rf'<a\b[^>]*href="([^"]*/{DPLACE_THEATRE_ID}/movies/([^"/?#]+)[^"]*)"[^>]*>(.*?)</a>', page_html, re.S):
        alt = re.search(r'alt="([^"]+)"', inner)
        add_link(_strip(inner) or (html.unescape(alt.group(1)) if alt else ""), href)
        add_link(slug.replace("-", " "), href)

    # 2a) schema.org events: {"@type": "ScreeningEvent", "name": "X at Mary Pickford...", "startDate": "...-08:00"}
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for o in _walk(data):
            sd = o.get("startDate")
            if not isinstance(sd, str) or not re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", sd):
                continue
            work = o.get("workPresented") if isinstance(o.get("workPresented"), dict) else {}
            title = work.get("name") or re.sub(r"\s+at\s+(the\s+)?Mary Pickford.*$", "", str(o.get("name", "")), flags=re.I)
            try:
                ts = datetime.fromisoformat(sd)
                ts = ts.replace(tzinfo=TZ) if ts.tzinfo is None else ts.astimezone(TZ)
            except ValueError:
                continue
            if title:
                days.setdefault(ts.strftime("%Y-%m-%d"), []).append((html.unescape(title), ts))
                add_link(title, work.get("url") or o.get("url") or "")
    if days:
        how = "schema.org events"

    # 2b) visible sections: id="day-YYYY-MM-DD" ... movie link (title) ... ticket links "9:10 PM"
    if not days:
        marks = [(m.start(), m.group(1)) for m in re.finditer(r'id="day-(\d{4}-\d\d-\d\d)"', page_html)]
        tok = re.compile(rf'<a\b[^>]*href="[^"]*/{DPLACE_THEATRE_ID}/movies/([^"/?#]+)[^"]*"[^>]*>(.*?)</a>'
                         r'|<a\b[^>]*href="[^"]*/Tickets/[^"]*"[^>]*>(.*?)</a>', re.S)
        for i, (pos, day) in enumerate(marks):
            seg = page_html[pos: marks[i + 1][0] if i + 1 < len(marks) else len(page_html)]
            title = None
            for slug, inner, tick in tok.findall(seg):
                if slug:
                    alt = re.search(r'alt="([^"]+)"', inner)
                    title = _strip(inner) or (html.unescape(alt.group(1)) if alt else "") or title or slug.replace("-", " ")
                    continue
                tm = re.search(r"(\d{1,2}):(\d\d)\s*([AaPp])\.?[Mm]", _strip(tick))
                if title and tm:
                    hour = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "p" else 0)
                    ts = datetime.strptime(f"{day} {hour}:{tm.group(2)}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                    days.setdefault(day, []).append((title, ts))
        if days:
            how = "day sections"
    return {"links": links, "days": days, "how": how, "tickets": tickets}


def fetch_dplace(_=None):
    return parse_dplace(http(THEATER_URLS["pickford"], tries=2, headers=DPLACE_HEADERS))


def dplace_ticket(title, ts, tickets):
    """The ticket link of the D'Place showing with this title at this exact time, if there is one."""
    for t, tts, url in tickets:
        if tts == ts and titles_match(t, title):
            return url
    return None


def apply_dplace(results, dp, dp_err, days, problems):
    """Mary Pickford: a ticket link for every showtime D'Place lists, a BigScreen-vs-D'Place check, and D'Place
    as the backup source when BigScreen fails. D'Place being unreachable never fails the run (the times then
    simply have no ticket link); a real disagreement between the two sources does. The colored block always
    opens the theater's showtimes page, because D'Place's movie pages ask which theater you mean."""
    if dp_err or not dp:
        print(f"  D'Place: not available ({dp_err}); Mary Pickford links go to the showtimes page")
        step_summary(f"D'Place site not available from GitHub ({str(dp_err)[:200]}) - Mary Pickford uses BigScreen only, "
                     "its times have no ticket links.")
        return
    print(f"  D'Place: {len(dp['tickets'])} ticket links, times read via {dp['how'] or 'nothing (layout not recognised)'}: "
          + ", ".join(f"{d} {len(v)}" for d, v in sorted(dp["days"].items())))
    if not dp["how"]:
        step_summary("D'Place: showtimes layout not recognised - no cross-check / backup for Mary Pickford.")
    for day in days:
        got, err = results[("pickford", day)]
        theirs = dp["days"].get(day, [])
        if err and len(theirs) >= MIN_SHOWTIMES:   # BigScreen failed: use the theater's own page
            got = [dict(theater="pickford", title=t, ts=ts, fmt="", url=THEATER_URLS["pickford"], tmdb_id=None,
                        buy=dplace_ticket(t, ts, dp["tickets"])) for t, ts in theirs]
            results[("pickford", day)] = (got, None)
            problems.append(f"Mary Pickford Theatre: BigScreen failed for {day} ({str(err)[:200]}); "
                            "used the D'Place site instead (no D-BOX marks)")
            note = "Mary Pickford showtimes taken from the theater's site; D-BOX showings are not marked."
            if note not in NOTES:
                NOTES.append(note)
            err = None
        if err or not got:
            continue
        for s in got:
            s["buy"] = dplace_ticket(s["title"], s["ts"], dp["tickets"])
        if len(theirs) >= MIN_SHOWTIMES:            # both sources answered: they should agree
            # D'Place drops showings that have already started, BigScreen keeps the whole day:
            # compare only what is still ahead (with a little margin)
            ahead = datetime.now(TZ) + timedelta(minutes=10)
            a = Counter(s["ts"].strftime("%H:%M") for s in got if s["ts"] >= ahead)
            b = Counter(ts.strftime("%H:%M") for _, ts in theirs if ts >= ahead)
            diff = sum(((a - b) + (b - a)).values())
            if diff > max(4, 0.2 * max(sum(a.values()), sum(b.values()))):
                # say exactly which showings differ, so one look at the log shows which source is wrong
                names = {}
                for s in got:
                    names.setdefault(s["ts"].strftime("%H:%M"), s["title"])
                for t, ts in theirs:
                    names.setdefault(ts.strftime("%H:%M"), t)
                only_bs = ", ".join(f"{k} {names.get(k, '?')}" for k in sorted((a - b).elements()))
                only_dp = ", ".join(f"{k} {names.get(k, '?')}" for k in sorted((b - a).elements()))
                problems.append(f"Mary Pickford Theatre: BigScreen ({sum(a.values())}) and the D'Place site "
                                f"({sum(b.values())}) disagree on {diff} showtimes for {day} - check which one is right. "
                                f"Only on BigScreen: {only_bs or 'none'}. Only on D'Place: {only_dp or 'none'}")


# ------------------------------------------------------------------------ TMDB
TMDB_STATS = {"ok": 0, "fail": 0}


def tmdb(path, **params):
    try:
        out = _tmdb(path, **params)
        TMDB_STATS["ok"] += 1
        return out
    except Exception:
        TMDB_STATS["fail"] += 1
        raise


def _tmdb(path, **params):
    key = os.environ["TMDB_API_KEY"].strip()
    params.setdefault("language", "en-US")
    headers = {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    return json.loads(http(f"{TMDB}{path}?{urllib.parse.urlencode(params)}", headers=headers))


ANNIV_TAIL = re.compile(r"\s*(?:[-:]\s*)?\d+(?:st|nd|rd|th)\s+anniversary\b.*$", re.I)
ANNIV_ANY = re.compile(r"\b\d+(?:st|nd|rd|th)\s+anniversary\b", re.I)


def clean_title(t):
    t = re.sub(r"^(IMAX|CUT\d+|DOLBY CINEMA|FATHOM|TCM)\s*:\s*", "", t.strip(), flags=re.I)
    t = re.sub(r"\([^)]*\)", " ", t)
    suffix = (r"\s+-\s+(the imax experience|sensory friendly|open caption\w*|(english )?dubbed|(english )?subtitled|(\d{4}\s+)?re-?issue(\s+\d{4})?"
              r"|early access.*|fan event.*)\s*$")
    for _ in range(3):
        t = re.sub(suffix, "", t, flags=re.I)
        cut = ANNIV_TAIL.sub("", t)          # "... 25th Anniversary", "...: 25th Anniversary", "... - 25th Anniversary - Fest"
        if len(cut.strip()) >= 2:            # never reduce a title to nothing
            t = cut
    t = re.sub(r"\s+in 3d\s*$", "", t, flags=re.I)
    t = re.sub(r"\b(early access|reissue|encore|anniversary.*)$", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def norm(t):
    return re.sub(r"[^a-z0-9]+", "", clean_title(t).lower().replace("&", "and"))


# These theaters' pages cannot open one movie for our theater (their movie pages ask where you are), so the
# colored block opens the theater's page and a "text fragment" scrolls it to the movie's title and highlights it.
# Browsers that do not know text fragments, or a title that is not found, simply show the top of the page.
FRAGMENT_THEATERS = ("cinemark", "regal", "pickford", "laquinta")


def text_fragment(title, theater, others):
    """'#:~:text=Title' for the theater's page, or '' when it could scroll to the wrong film: the title is part of
    another title shown at the same theater ("Dune" / "Dune: Part Two" -> the first match would be Part Two)."""
    if theater not in FRAGMENT_THEATERS:
        return ""
    shown = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", title)).strip()
    n = norm(shown)
    if not n or any(o != n and n in o for o in others):
        return ""
    return "#:~:text=" + urllib.parse.quote(shown, safe="").replace("-", "%2D")


def titles_match(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    s, l = sorted((a, b), key=len)
    return len(s) >= 5 and l.startswith(s)


_cache = {}


def tmdb_by_id(i):
    k = ("id", i)
    if k not in _cache:
        try:
            _cache[k] = tmdb(f"/movie/{i}")
        except Exception as e:  # noqa: BLE001
            print(f"  TMDB id {i}: {e}")
            _cache[k] = None
    return _cache[k]


def tmdb_by_title(title, year=None):
    k = ("t", norm(title), year)
    if k not in _cache:
        found = None
        try:
            queries = [clean_title(title)]
            alt = re.sub(r":?\s+part (one|1)$", "", queries[0], flags=re.I)
            if alt != queries[0]:
                queries.append(alt)
            if "&" in queries[0]:                  # "Planes, Trains & Automobiles" is "... and ..." on TMDB
                queries.append(queries[0].replace("&", "and"))
            for q in queries:
                res = tmdb("/search/movie", query=q, include_adult="false")["results"]
                exact = [r for r in res if norm(q) in (norm(r.get("title", "")), norm(r.get("original_title", "")))]
                loose = [r for r in res if titles_match(q, r.get("title", "")) or
                         titles_match(q, r.get("original_title", ""))]
                pick = exact or loose
                if pick and year:  # a known release year narrows it down (re-releases)
                    pick = [r for r in pick if (r.get("release_date") or "").startswith(year)] or pick
                if pick:
                    found = max(pick, key=lambda r: r.get("popularity", 0))
                    break
        except Exception as e:  # noqa: BLE001
            print(f"  TMDB search '{title}': {e}")
        _cache[k] = found
    return _cache[k]


# ----------------------------------------------------------------- aggregation
FEW_VOTES = 100   # below this a rating is shown dimmed and counts less in the sort


REACH_W = 1.25   # how many rating points the most widely shown title earns on top of its quality score


def sessions_of(g):
    """Showtimes of a title over the days that were loaded (today + tomorrow), across all theaters."""
    return sum(len(v) for v in g["by_theater"].values())


def set_reach(movies):
    """Reach (0..1): how widely the title is shown, on a log scale relative to the busiest title of the day, so the
    scale follows weekdays, weekends and holidays by itself and a film fades as theaters cut its showtimes.
    Events and re-releases get none (they are one-off screenings by nature)."""
    top = max([sessions_of(g) for g in movies if not g["label"]] or [1])
    for g in movies:
        n = sessions_of(g)
        g["reach"] = 0.0 if g["label"] or top < 2 else min(1.0, math.log(max(n, 1)) / math.log(top))


def sort_key(g):
    """Quality plus reach. Quality: best rated first, but a rating built on few votes is pulled toward an average,
    so '8.8 from 14 votes' does not outrank '8.5 from 900 votes'. Reach: a title shown all over town and all day
    ranks above a slightly better rated one shown once (see set_reach). Unrated titles go last, most popular first."""
    v, r = g["votes"], g["rating"]
    if v >= 10 and r > 0:
        return (0, -((v * r + 50 * 6.5) / (v + 50) + REACH_W * g.get("reach", 0.0)), g["title"])
    return (1 if g["tmdb_id"] else 2, -g["pop"], g["title"])


def fmt_votes(v):
    return f"{v / 1000:.1f}k".replace(".0k", "k") if v >= 1000 else str(v)


def info_genres(info):
    if not info:
        return []
    if info.get("genres"):
        names = [x.get("name") for x in info["genres"]]
    else:
        names = [TMDB_GENRES.get(i) for i in info.get("genre_ids", [])]
    return [n.replace("Science Fiction", "Sci-Fi") for n in names if n][:2]


def prefetch_tmdb(showings):
    """Do all TMDB lookups up front, in parallel; build_movies then reads the cache."""
    ids = {s["tmdb_id"] for s in showings if s["tmdb_id"]}
    titles = {(s["title"], s.get("year")) for s in showings if not s["tmdb_id"]}
    titles |= {(s["title"], None) for s in showings if s["tmdb_id"]}  # fallback if an id lookup fails
    pmap(tmdb_by_id, ids)
    pmap(lambda ty: tmdb_by_title(*ty), titles)


def build_movies(showings, use_tmdb):
    if use_tmdb:
        prefetch_tmdb(showings)
    groups = {}
    for s in showings:
        info = None
        if use_tmdb:
            info = tmdb_by_id(s["tmdb_id"]) if s["tmdb_id"] else tmdb_by_title(s["title"], s.get("year"))
            if info is None and s["tmdb_id"]:
                info = tmdb_by_title(s["title"])
        key = ("tmdb", info["id"]) if info else ("t", norm(s["title"]))
        g = groups.setdefault(key, dict(title=(info or {}).get("title") or clean_title(s["title"]) or s["title"],
                                        poster=(IMG + info["poster_path"]) if (info or {}).get("poster_path") else None,
                                        alt_posters={},
                                        votes=(info or {}).get("vote_count", 0),
                                        rating=(info or {}).get("vote_average", 0),
                                        pop=(info or {}).get("popularity", 0),
                                        tmdb_id=(info or {}).get("id"), buy={},
                                        genres=info_genres(info), runtime=(info or {}).get("runtime") or None,
                                        released=(info or {}).get("release_date") or "", year_hint=None,
                                        raw_title=s["title"], th_rt=None, th_genre=None, by_theater={}))
        g["year_hint"] = g["year_hint"] or s.get("year")
        if ANNIV_ANY.search(s["title"]):
            g["anniv"] = True
        g["th_rt"] = g["th_rt"] or s.get("rt")
        g["th_genre"] = g["th_genre"] or s.get("genre")
        g["by_theater"].setdefault(s["theater"], {})[(s["ts"], s.get("fmt", ""))] = s["url"]
        if s.get("buy"):
            g["buy"][(s["theater"], s["ts"], s.get("fmt", ""))] = s["buy"]
        if s.get("poster"):
            g["alt_posters"].setdefault(s["theater"], s["poster"])
    # a title that TMDB did not recognise in one theater but did in another: fold it in
    by_norm = {norm(g["title"]): g for k, g in groups.items() if k[0] == "tmdb"}
    for k in [k for k in groups if k[0] == "t"]:
        target = by_norm.get(k[1])
        if target:
            src = groups.pop(k)
            for th, shows in src["by_theater"].items():
                target["by_theater"].setdefault(th, {}).update(shows)
            for th, pst in src["alt_posters"].items():
                target["alt_posters"].setdefault(th, pst)
            target["buy"].update(src["buy"])
            target["anniv"] = target.get("anniv") or src.get("anniv")
            target["th_rt"] = target["th_rt"] or src["th_rt"]
            target["th_genre"] = target["th_genre"] or src["th_genre"]
    movies = list(groups.values())
    for g in movies:
        g["runtime"] = g["th_rt"] or g["runtime"]
        if not g["genres"] and g["th_genre"]:
            g["genres"] = [g["th_genre"]]
    old = (datetime.now(TZ) - timedelta(days=RERELEASE_DAYS)).strftime("%Y-%m-%d")
    for g in movies:  # small poster label: Event / Re-release <year>
        names = f'{g["raw_title"]} {g["title"]} {g["th_genre"] or ""}'
        g["label"] = None
        if SPECIAL_SHOW.search(g["raw_title"]):
            g["label"] = "Special screening"
        elif EVENT_STRONG.search(names) or (not g["tmdb_id"] and EVENT_WEAK.search(names)) \
                or re.search(r"\b(event|concert|opera)\b", g["th_genre"] or "", re.I):
            g["label"] = "Event"
        elif g["released"][:4].isdigit() and g["released"] < old:
            g["label"] = f'Re-release <i>{g["released"][:4]}</i>'
        elif not g["released"] and str(g["year_hint"] or "").isdigit() and f'{g["year_hint"]}-12-31' < old:
            g["label"] = f'Re-release <i>{g["year_hint"]}</i>'
        elif g.get("anniv"):   # "Nth Anniversary" in the theater's title but no release date known (TMDB miss)
            g["label"] = "Re-release"
    for g in movies:  # no TMDB poster -> use the theater's own artwork
        if not g["poster"]:
            g["poster"] = next((g["alt_posters"][k] for k in ("festival", "cinemark", "regal", "pickford", "laquinta")
                                if k in g["alt_posters"]), None)
    set_reach(movies)
    movies.sort(key=sort_key)
    return movies


# ------------------------------------------------------------------- rendering
def fmt_time(ts):
    return ts.strftime("%-I:%M %p")


def poster_html(g, i):
    if g["poster"]:
        lazy = "eager" if i < 8 else "lazy"
        return (f'<img src="{html.escape(g["poster"])}" alt="{html.escape(g["title"])}" loading="{lazy}" decoding="async" '
                f'onerror="var d=document.createElement(\'div\');d.className=\'poster\';'
                f'd.textContent=this.alt;this.replaceWith(d)">')
    h = (i * 47) % 360
    return (f'<div class="poster" style="background:linear-gradient(160deg,hsl({h} 45% 30%),'
            f'hsl({h + 60} 45% 16%))"><span>{html.escape(g["title"])}</span></div>')


def now_card(g, i, d0, others=None):
    if g["votes"] >= 10 and g["rating"] > 0:
        few = " few" if g["votes"] < FEW_VOTES else ""
        rating = (f'<span class="rating{few}">&#9733; {g["rating"]:.1f}</span>'
                  f'<span class="votes">{fmt_votes(g["votes"])} votes</span>')
    else:
        rating = '<span class="rating none">No ratings yet</span>'
    rows = ""
    for key, name in THEATERS:
        shows = g["by_theater"].get(key)
        if not shows:
            continue
        items = sorted(shows.items())
        url0 = next((u for (ts, _), u in items if ts.strftime("%Y-%m-%d") == d0), None)
        url1 = next((u for (ts, _), u in items if ts.strftime("%Y-%m-%d") != d0), None)
        frag = text_fragment(g["title"], key, (others or {}).get(key, ()))
        if frag:
            url0 = url0.split("#")[0] + frag if url0 else url0
            url1 = url1.split("#")[0] + frag if url1 else url1
        groups = ""
        for label, want_today in (("", True), ("Tomorrow", False)):
            chips = ""
            for (ts, fmt), url in items:
                if (ts.strftime("%Y-%m-%d") == d0) != want_today:
                    continue
                tag = f"<b>{fmt}</b> " if fmt else ""
                cls = {"IMAX": "t imax", "D-BOX": "t fmt"}.get(fmt, "t")
                buy = g["buy"].get((key, ts, fmt))
                bx = f' data-buy="{html.escape(buy)}" title="Open this showing on the theater&rsquo;s site"' if buy else ""
                chips += f'<span class="{cls}" data-ts="{int(ts.timestamp())}"{bx}>{tag}{fmt_time(ts)}</span>'
            if chips:
                lab = f'<span class="lab">{label}</span>' if label else ""
                groups += f'<div class="times {"d0" if want_today else "d1"}">{lab}{chips}</div>'
        u1 = f' data-u1="{html.escape(url1)}"' if url1 else ""
        rows += (f'<a class="th" data-th="{key}" href="{html.escape(url0 or url1)}"{u1} target="_blank" rel="noopener">'
                 f'<span class="thn"><span class="full">{html.escape(name)}</span>'
                 f'<span class="short">{html.escape(SHORT_NAMES[key])}</span><i>&#8599;</i></span>{groups}</a>')
    rt = f' data-rt="{g["runtime"]}"' if g.get("runtime") else ""
    gen = f' data-genre="{html.escape(" / ".join(g["genres"]))}"' if g.get("genres") else ""
    lab = f'<span class="plab">{g["label"]}</span>' if g.get("label") else ""
    return (f'<article class="card now" tabindex="0"{rt}{gen}>{poster_html(g, i)}{lab}<div class="pbox" aria-hidden="true"><span class="plab2">Today only*</span></div><div class="body"><h3>{html.escape(g["title"])}</h3>'
            f'<div class="meta">{rating}</div><div class="status" hidden></div>{rows}</div></article>')


def soon_card(m, i):
    title = m.get("title") or "Untitled"
    try:
        d = datetime.strptime(m.get("_date") or m["release_date"], "%Y-%m-%d").strftime("%b %-d")
    except (KeyError, ValueError):
        d = "TBA"
    g = dict(title=title, poster=(IMG + m["poster_path"]) if m.get("poster_path") else None)
    return (f'<article class="card soon" title="{html.escape(title)}">{poster_html(g, i + 30)}<span class="badge">{d}</span>'
            f'<h3 class="vh">{html.escape(title)}</h3></article>')


CSS = """
:root{--bg:#111318;--fg:#eceef2;--mut:#9aa1ad;--card:#1b1e26;--line:#2b303b;--cline:#565e70;--acc:#f5b301;--chip:#262b36}
@media (prefers-color-scheme:light){:root{--bg:#f5f6f8;--fg:#15171c;--mut:#616977;--card:#fff;--line:#e3e6eb;--cline:#c8c8c8;--acc:#b97d00;--chip:#eef0f4}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header,main,footer{max-width:1100px;margin:0 auto;padding:16px}
h1{margin:8px 0 0;font-size:1.6rem} h2{margin:26px 0 12px;font-size:1.2rem} h2 .sub{font-weight:400;font-size:.8rem}
.sub{color:var(--mut);font-size:.9rem}
/* page title as a quiet background element: centered, small, spaced, faded; date/update line muted below it */
header{text-align:center;padding-bottom:0}
header h1{font-size:1.05rem;font-weight:600;letter-spacing:.32em;text-transform:uppercase;opacity:.38;margin:4px 0 0;padding-left:.32em}
header .sub{font-size:.78rem;opacity:.8;margin-top:9px}
header h1 .reg{display:block;font-size:.78rem;letter-spacing:.16em;margin-top:0;line-height:1.25;opacity:1;padding-left:.16em}
main > h2:first-child{margin-top:12px}
.notice{max-width:1100px;margin:8px auto;padding:8px 14px;border-radius:8px;background:var(--chip);color:var(--fg);font-size:.85rem;border-left:4px solid var(--acc)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;align-items:start}
@media (max-width:700px){.grid{grid-template-columns:repeat(2,1fr);gap:10px}}
/* double edge: a clear 1px line plus a half-strength, half-width ring around it (no drop shadow) */
.card{background:var(--card);border-radius:12px;overflow:hidden;position:relative;border:1px solid var(--cline);
  box-shadow:0 0 0 .5px color-mix(in srgb,var(--cline) 50%,transparent)}
.card img,.poster{width:100%;aspect-ratio:2/3;object-fit:cover;display:block}
.poster{background:linear-gradient(160deg,#3a4a66,#1c2233);display:flex;align-items:center;justify-content:center;text-align:center;padding:14px;color:#fff;font-weight:600}
.body{padding:10px}
h3{margin:0;font-size:.95rem;line-height:1.25}
.meta{margin:4px 0 8px;font-size:.85rem}
.rating{color:var(--acc);font-weight:600} .rating.few{color:var(--mut)} .votes{color:var(--mut);font-size:.75rem;margin-left:6px} .rating.none{color:var(--mut);font-weight:400}
.th{--tc:#888;--tb:rgba(136,136,136,.10);display:block;margin-top:8px;padding:8px 10px;border-radius:8px;background:var(--tb);color:var(--fg);text-decoration:none;transition:filter .12s,transform .12s}
@media (hover:hover){.th:hover{background:color-mix(in srgb,var(--tc) 30%,transparent)}}   /* hover only where a pointer can hover: on a phone it stuck after a tap */
.th:active{transform:scale(var(--thx,.98),var(--thy,.98))}   /* --thx/--thy set in JS: the same 3px shrink on a 1-row block and a 12-row block */
@media (hover:hover) and (prefers-color-scheme:light){.th:hover{background:color-mix(in srgb,var(--tc) 22%,transparent)}}
/* red / green / blue / purple, plus a muted honey for La Quinta */
[data-th="cinemark"]{--tc:#f0506a;--tb:rgba(240,80,106,.17)}
[data-th="regal"]{--tc:#2fc48a;--tb:rgba(47,196,138,.16)}
[data-th="festival"]{--tc:#5b8cff;--tb:rgba(91,140,255,.18)}
[data-th="pickford"]{--tc:#b98cff;--tb:rgba(170,120,255,.18)}
[data-th="laquinta"]{--tc:#d6a24c;--tb:rgba(214,162,76,.16)}
@media (prefers-color-scheme:light){
[data-th="cinemark"]{--tc:#d7193a;--tb:rgba(215,25,58,.08)}
[data-th="regal"]{--tc:#0f9d64;--tb:rgba(15,157,100,.09)}
[data-th="festival"]{--tc:#2d63e0;--tb:rgba(45,99,224,.09)}
[data-th="pickford"]{--tc:#7a3fd6;--tb:rgba(122,63,214,.09)}
[data-th="laquinta"]{--tc:#a8741a;--tb:rgba(168,116,26,.13)}}
.th .thn::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--tc);flex:none;margin:4px 6px 0 0}
.th .thn{justify-content:flex-start} .th .thn i{margin-left:auto}
.th .t{background:rgba(255,255,255,.07)}
@media (prefers-color-scheme:light){.th .t{background:rgba(255,255,255,.85)}}
.th.off{opacity:.7}
.thn .short{display:none}
@media (max-width:700px){
.thn .full{display:none} .thn .short{display:inline}
.th{padding:6px 7px;margin-top:6px}
.times{gap:4px}
.t{font-size:.74rem;padding:3px 6px}
.body{padding:8px}}
.thn{display:flex;justify-content:space-between;gap:6px;font-size:.78rem;font-weight:600;color:var(--fg);text-decoration:none;line-height:1.2}
.thn i{font-style:normal;color:var(--mut)}
.times{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.t{background:var(--chip);color:var(--fg);text-decoration:none;font-size:.78rem;padding:3px 8px;border-radius:999px;white-space:nowrap}
.t.past{opacity:.45;text-decoration:line-through}
.t[data-buy]:not(.past),.c[data-buy]:not(.past){cursor:pointer;transition:filter .12s}
@media (hover:hover){.t[data-buy]:not(.past):hover,.c[data-buy]:not(.past):hover{filter:brightness(.93);text-decoration:underline;text-underline-offset:2px}}
@media (hover:hover) and (prefers-color-scheme:dark){.t[data-buy]:not(.past):hover,.c[data-buy]:not(.past):hover{filter:brightness(1.35)}}
/* tomorrow: dashed outline in the theater's colour at half strength (plain colour where color-mix is unsupported) */
.times.d1 .t{background:transparent;border:1px dashed var(--tc);border-color:color-mix(in srgb,var(--tc) 55%,transparent)}
.lab{flex-basis:100%;font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
.status{font-size:.78rem;color:var(--mut);margin:-2px 0 8px;font-weight:600}
.card.now:not(.later):not(.done) .status{color:var(--acc)}
.th.off .thn{color:var(--mut)}
.card.later img,.card.later .poster{filter:saturate(.55) brightness(.85)}
.card.done{opacity:.6} .card.done img,.card.done .poster{filter:grayscale(1)}
.t.imax{border:1px solid var(--acc)} .t.imax b{color:var(--acc);font-size:.68rem;letter-spacing:.03em}
.t.fmt{border:1px solid var(--tc)} .t.fmt b{color:var(--tc);font-size:.62rem;letter-spacing:.03em}
.times.d1 .t.imax{border:1px solid var(--acc)} .times.d1 .t.fmt{border:1px solid var(--tc)}
.soon .body{padding:8px 10px 10px}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.plab{position:absolute;top:8px;left:8px;z-index:1;font-size:.7rem;font-weight:700;letter-spacing:.02em;padding:3px 8px;
  border-radius:999px;background:rgba(20,22,28,.62);color:#fff;-webkit-backdrop-filter:blur(8px) saturate(1.3);backdrop-filter:blur(8px) saturate(1.3);
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.24),0 1px 4px rgba(0,0,0,.3)}  /* glass pill: the light hairline keeps the edge visible on dark posters */
.plab i{font-style:normal;font-weight:500;opacity:.8}
.badge{position:absolute;top:8px;left:8px;background:#f5b301;color:#111;font-weight:700;font-size:.78rem;padding:3px 9px;border-radius:999px}
.fallback{max-width:1068px;margin:8px auto;padding:10px 14px;border-radius:10px;background:var(--card);border:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;text-decoration:none;color:var(--fg)}
@media (max-width:1100px){.fallback{margin:8px 16px}}
.fallback small{display:block;color:var(--mut);font-size:.78rem}
.fallback em{font-style:normal;white-space:nowrap;background:var(--acc);color:#111;font-weight:600;font-size:.85rem;padding:6px 12px;border-radius:999px}
.card.now{cursor:pointer;transition:border-color .12s}
.card.now:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.card.now:hover{border-color:var(--mut)}
@media (hover:none){.card.now:hover{border-color:var(--cline)}}   /* touch: no sticky dark border after a tap */
/* tap hint: until a card is opened, a wave runs over the theater blocks on screen (left to right) or over the movie rows (first to last): each one
   comes alive for a moment - richer and brighter color, slightly bigger, a tiny lift - and settles back */
@media (prefers-reduced-motion:no-preference){
  .th.nudge{animation:nudge var(--nt,.9s) ease-in-out var(--nd,0s) both}
  .gw.nudge{animation:nudgew var(--nt,.9s) ease-in-out var(--nd,0s) both}
  @keyframes nudge{
    0%,100%{transform:translateY(0) scale(1);filter:saturate(1) brightness(1)}
    30%,60%{transform:translateY(calc(var(--nl,0)*-1px)) scale(var(--ns,1.05));filter:saturate(var(--nsa,1.8)) brightness(var(--nb,1.06))}}
  @keyframes nudgew{30%,60%{background-color:color-mix(in srgb,var(--acc) 16%,transparent);transform:translateY(calc(var(--nl,0)*-1px)) scale(calc(1 + (var(--ns,1.05) - 1)*.4));filter:saturate(var(--nsa,1.8)) brightness(var(--nb,1.06))}}}
/* permanent "this is tappable" signs on a theater block: sig-arrow = a bolder accent arrow, sig-label = a small round chevron button */
.sig-arrow .th .thn i{color:var(--acc);font-weight:800;font-size:1rem;line-height:1}
.sig-label .th .thn i{font-size:0;line-height:0;margin-left:auto;align-self:flex-start;flex:none}
.sig-label .th .thn i::after{content:"";display:block;width:16px;height:16px;border-radius:50%;
  background:rgba(0,0,0,.12) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%2314161c' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 4.4 10 8l-3.6 3.6'/%3E%3C/svg%3E") center/100% 100% no-repeat}
@media (prefers-color-scheme:dark){.sig-label .th .thn i::after{background-color:rgba(255,255,255,.16);
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%23fff' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 4.4 10 8l-3.6 3.6'/%3E%3C/svg%3E")}}
/* space between the theater name row and the first row of times, and a slightly smaller, lighter round button,
   so the button never looks attached to the last time chip */
.sig-label .th .thn{padding-bottom:4px}
.sig-label .th .thn i{margin-right:-1px}
.sig-label .th .thn i::after{width:15px;height:15px;background-color:rgba(0,0,0,var(--sca,.06))}  /* light mode: the same translucent gray, softer (was .10); on white it matches the Posters pill */
@media (prefers-color-scheme:dark){.sig-label .th .thn i::after{background-color:rgba(255,255,255,.13)}}
/* sizes are variables so they can be tuned: --sc = circle, --sa = arrow */
.sig-label .th .thn i::after{width:var(--sc,20px);height:var(--sc,20px);background-size:var(--sa,16px) var(--sa,16px)}
/* the dot and the name sit on the same horizontal line as the circle: shift them down by half the extra height (a 15px line vs the circle); all three are centred on the same line, whatever the font */
.sig-label .th .thn{--d:max(0px,calc((var(--sc,20px) - 15px)/2))}
.sig-label .th .thn .full,.sig-label .th .thn .short{padding-top:var(--d)}
.sig-label .th .thn::before{margin:calc(3.5px + var(--d)) 2px 0 0}  /* dot centred on the first line (15px line, 8px dot); 6px gap + 2px = 8px to the name */
/* the line under "Now playing" (until the first tap) */
.taphint{margin:-6px 0 10px;font-size:.82rem;color:var(--mut)}
#sheet{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;padding:16px}
.sheet-bg{position:absolute;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(2px)}
.sheet-box{position:relative;width:100%;max-width:520px;max-height:86vh;overflow:hidden;display:flex;flex-direction:column;background:var(--card);
  border:1px solid var(--line);border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
/* only the inside scrolls (the close button stays put); no scrollbar, a soft fade while there is more below */
.sheet-body{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:18px;scrollbar-width:none}
.sheet-body::-webkit-scrollbar{display:none}
.sheet-box::after{content:"";position:absolute;left:0;right:0;bottom:0;height:56px;pointer-events:none;
  background:linear-gradient(to bottom,transparent,var(--card));opacity:0;transition:opacity .2s}
.sheet-box.more::after{opacity:1}
.sheet-x{position:absolute;z-index:2;top:10px;right:10px;width:34px;height:34px;border-radius:50%;border:0;padding:0;
  display:flex;align-items:center;justify-content:center;background:var(--chip);color:var(--fg);cursor:pointer;box-shadow:0 0 0 2px var(--card),0 0 6px 4px var(--card)}
.sheet-x svg{width:14px;height:14px;display:block}
.shead{display:flex;gap:14px;align-items:flex-start;margin-bottom:12px;padding-right:36px}
.shead > img,.shead > .poster{width:92px;flex:none;border-radius:8px;aspect-ratio:2/3;object-fit:cover;font-size:.7rem;padding:6px}
.shead > img{cursor:zoom-in;padding:0}   /* the 6px padding is for the text placeholder; on a photo it hid the rounded corners */
/* tap the small poster in the popup: show it large */
.zoom{position:fixed;inset:0;z-index:60;display:flex;align-items:center;justify-content:center;padding:24px;background:rgba(0,0,0,.82);
  cursor:zoom-out;opacity:0;transition:opacity .2s;will-change:opacity;-webkit-tap-highlight-color:transparent}
.zoom.on{opacity:1}
/* fixed size, so the sharp image replacing the small one never changes the size mid-animation */
.zoom img{flex:none;min-width:0;width:min(calc(100vw - 48px),520px,calc(86vh * 2 / 3));aspect-ratio:2/3;height:auto;object-fit:cover;border-radius:12px;
  box-shadow:0 10px 30px rgba(0,0,0,.45);transform:scale(.94);transition:transform .22s ease;will-change:transform}
.zoom.on img{transform:scale(1)}
.zoom .zx{position:absolute;top:14px;right:14px;width:34px;height:34px;border-radius:50%;border:0;padding:0;display:flex;align-items:center;justify-content:center;
  background:var(--chip);color:var(--fg);cursor:pointer}
.zoom .zx svg{width:14px;height:14px;display:block}
@media (prefers-reduced-motion:reduce){.zoom,.zoom img{transition:none}}
.stitle h3{font-size:1.15rem;margin:2px 0 4px}
.sstatus{font-size:.8rem;color:var(--acc);font-weight:600;margin-top:4px}
.sinfo{font-size:.82rem;color:var(--mut);margin-top:2px}
.snext{font-size:.85rem;font-weight:600;margin-top:8px;padding:6px 10px;border-radius:8px;background:var(--chip);display:inline-block}
.snote{font-size:.75rem;color:var(--mut);margin:12px 2px 0}
.theaters{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:6px 0 14px}
@media (max-width:980px){.theaters{grid-template-columns:repeat(2,1fr)}}
@media (max-width:560px){.theaters{grid-template-columns:1fr}}
.tinfo{background:var(--tb);border-radius:10px;padding:10px 12px;color:var(--fg);font-size:.85rem}
.tinfo b{display:flex;align-items:center;gap:7px}
.tinfo b::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--tc);flex:none}
.tinfo a{color:var(--fg)} .tinfo .tpage{display:block;margin-top:4px;font-weight:700} .tinfo .addr{color:var(--mut);display:block;margin:3px 0 6px;font-size:.8rem}
.foot{font-size:.78rem;line-height:1.5} .foot p{margin:6px 0}
/* author credit at the end of the page */
.credit{text-align:center;margin:26px 0 6px}
.credit .c1{color:var(--fg);font-size:.95rem} .credit .c1 b{font-weight:700}
.credit .c2{color:var(--mut);font-size:.8rem;margin:2px 0 10px}
.credit .c2 a{color:inherit}
.cbtn{display:inline-flex;align-items:center;gap:8px;background:#f5b301;color:#15171c;font-weight:700;font-size:.85rem;text-decoration:none;
  padding:8px 14px;border-radius:999px;transition:filter .15s,transform .15s;-webkit-tap-highlight-color:transparent}
@media (hover:hover){.cbtn:hover{filter:brightness(1.06)}} .cbtn:active{transform:scale(.97)}
.cbtn svg{width:16px;height:16px}
.sth{display:block;color:var(--fg);text-decoration:none;background:var(--tb,rgba(136,136,136,.1));border-radius:10px;padding:10px 12px;margin-top:10px;transition:background .12s}
@media (hover:hover){.sth:hover{background:color-mix(in srgb,var(--tc,#888) 30%,transparent)}}
@media (hover:hover) and (prefers-color-scheme:light){.sth:hover{background:color-mix(in srgb,var(--tc,#888) 22%,transparent)}}
.sthn{display:flex;align-items:center;gap:8px;font-weight:700;font-size:.92rem}
.sthn::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--tc,#888);flex:none}
.sthn i{margin-left:auto;flex:none;white-space:nowrap;font-style:normal;font-weight:600;font-size:.75rem;color:#fff;background:var(--tc,#888);padding:4px 10px;border-radius:999px}
.sgrp{margin-top:8px} .slab{font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin-bottom:5px}
.schips{display:flex;flex-wrap:wrap;gap:6px}
.c{font-size:.85rem;padding:4px 10px;border-radius:999px;background:var(--chip);white-space:nowrap}
.c.past{opacity:.4;text-decoration:line-through}
.c.imax{border:1px solid var(--acc)} .c.imax b{color:var(--acc);font-size:.7rem}
.c.fmt{border:1px solid var(--tc,#888)} .c.fmt b{color:var(--tc,#888);font-size:.66rem}
/* popup chips look exactly like the chips on the cards */
.sth .c{background:rgba(255,255,255,.07);color:var(--fg)}
@media (prefers-color-scheme:light){.sth .c{background:rgba(255,255,255,.85);color:#15171c}}
.sth .c.past{opacity:.45}
.sth .sgrp.tm .c{background:transparent;border:1px dashed var(--tc,#888);border-color:color-mix(in srgb,var(--tc,#888) 55%,transparent)}
.sth .sgrp.tm .c.imax{border:1px solid var(--acc)} .sth .sgrp.tm .c.fmt{border:1px solid var(--tc,#888)}
@media (max-width:700px){
  #sheet{align-items:flex-end;padding:0}
  .sheet-box{max-width:none;max-height:88vh;border-radius:18px 18px 0 0;border-bottom:0}
  .sheet-body{padding:16px 14px 24px}
  .shead > img,.shead > .poster{width:76px}}
/* "Now playing" cards keep their look; only the columns differ in height (masonry, laid out by the
   script; without it a normal grid is shown) */
.grid.masonry{display:flex;gap:16px;align-items:flex-start}
.grid.masonry .mcol{flex:1;display:flex;flex-direction:column;gap:16px;min-width:0}
@media (max-width:700px){.grid.masonry,.grid.masonry .mcol{gap:10px}}
.empty{color:var(--mut)}
footer{color:var(--mut);font-size:.8rem;padding-bottom:32px}
footer h2{color:var(--fg)}

/* ---- view switcher: Showtimes / Posters / List (icons, caption of the active one below) ---- */
.mode{display:inline-grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:2px;padding:3px;background:var(--chip);border-radius:999px;margin:10px 0 0}
.mode button{font:inherit;border:0;background:transparent;color:var(--mut);padding:0;width:58px;height:30px;display:flex;align-items:center;justify-content:center;border-radius:999px;cursor:pointer;
  transition:background .25s,color .25s,box-shadow .25s}
.mode button[aria-pressed="true"]{background:var(--card);color:var(--fg);box-shadow:0 0 0 1px var(--cline) inset}
.mode svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.modecap{font-size:.72rem;color:var(--mut);margin-top:5px;letter-spacing:.02em}
.modecap.peek{color:var(--fg)}
@media (hover:hover){
  .mode button:not([aria-pressed="true"]):not(.tour):hover{background:color-mix(in srgb,var(--fg) 10%,transparent);color:var(--fg)}
  .mode button:active{transform:scale(.94)}}
.mode button:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
/* first-visit hint: the inactive icons light up one by one in neutral gray and "breathe" once */
.mode button.tour{background:hsl(220 6% 55%);color:#14161c;box-shadow:none}
@media (prefers-color-scheme:light){.mode button.tour{color:#fff}}
@media (prefers-reduced-motion:no-preference){
  .mode button.tour svg{animation:breathe .6s ease-in-out both}
  @keyframes breathe{0%,100%{transform:scale(1)}45%{transform:scale(1.25)}}}
/* poster view: no times under the posters (tap a poster for the schedule) */
body.pm .card.now .th,body.pm .card.now .status{display:none}
.pbox{position:absolute;left:0;right:0;top:0;aspect-ratio:2/3;pointer-events:none}
.plab2{display:none;position:absolute;right:8px;bottom:8px;font-size:.7rem;font-weight:700;letter-spacing:.02em;padding:3px 8px;
  border-radius:999px;background:rgba(20,22,28,.62);color:#fff;-webkit-backdrop-filter:blur(8px) saturate(1.3);backdrop-filter:blur(8px) saturate(1.3);
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.24),0 1px 4px rgba(0,0,0,.3)}
body.pm .card.tonly .plab2{display:block}
/* poster view: a plain pill (same look as the view switcher at the top) in the bottom right of the caption, holding a chevron.
   The caption's bottom gap is trimmed so it matches the top; more room above the rating row keeps a wrapped title off the pill. Sizes come from --pw/--ph/--ps/--pr/--pb/--pd (defaults here; --pd = corner radius, 12px like the card). */
body.pm .card.now .meta{margin:10px 0 0}
body.pm .card.now::after{content:"";position:absolute;right:var(--pr,8px);bottom:var(--pb,8px);width:var(--pw,20px);height:var(--ph,20px);border-radius:var(--pd,10px);pointer-events:none;z-index:1;
  background:var(--chip) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%2314161c' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 4.4 10 8l-3.6 3.6'/%3E%3C/svg%3E") center/var(--ps,16px) var(--ps,16px) no-repeat}
@media (prefers-color-scheme:dark){body.pm .card.now::after{
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%23fff' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.4 4.4 10 8l-3.6 3.6'/%3E%3C/svg%3E")}}
@media (max-width:700px){body.pm .card.now::after{right:var(--pr,5px);bottom:var(--pb,7px)}}   /* same inset as the Showtimes circle (6px from the edge incl. the card's 1px border); on phones that block is tighter */
/* list view: one card with every movie instead of the posters */
body.lm #nowgrid{display:none}
.gcard,.glist{display:none;background:var(--card);border:1px solid var(--cline);border-radius:12px;
  box-shadow:0 0 0 .5px color-mix(in srgb,var(--cline) 50%,transparent);padding:10px 12px 8px;margin:0 0 12px}
body.pm .gcard,body.lm .glist{display:block}
.gcard.inmas{margin:0}
.gleg{display:flex;flex-wrap:wrap;gap:4px 6px;margin:0 0 6px}
/* times as plain text; the theater shows as its letter on a small pastel square (the theater-block color) */
.gk{display:inline-flex;align-items:center;font-size:.74rem;font-weight:500;color:var(--mut);white-space:nowrap;line-height:1.5}
.gk b{display:inline-flex;align-items:center;justify-content:center;width:1.25em;height:1.25em;margin-right:4px;border-radius:4px;
  background:var(--tb);color:color-mix(in srgb,var(--tc) 70%,var(--fg));font-weight:700;font-size:.92em}
.gleg .gk{margin-right:6px}
.gk.px{opacity:.5;text-decoration:line-through}
.gsub{font-size:.74rem;color:var(--mut);margin:0 0 2px}
.gw{display:flex;align-items:center;gap:9px;padding:6px 8px;margin:0 -8px;border-radius:8px;cursor:pointer;
  -webkit-tap-highlight-color:transparent;transition:background .3s ease,transform .3s ease}
.gw:nth-child(even of .gw){background:color-mix(in srgb,var(--fg) 6%,transparent)}
@media (prefers-color-scheme:light){.gw:nth-child(even of .gw){background:color-mix(in srgb,var(--fg) 3.5%,transparent)}}
/* hover (mouse) and tap: bright fill in the badge yellow, dark text, light chips */
@media (hover:hover){
  .gw:hover,.gw:nth-child(even of .gw):hover{background:var(--hl)}
  .gw:hover .gn,.gw:hover .gr{color:#15171c}
  .gw:hover .gi{background:rgba(0,0,0,.10);color:#15171c;border-color:transparent}
  .gw:hover .gk{color:#15171c} .gw:hover .gk b{background:rgba(255,255,255,.75);color:color-mix(in srgb,var(--tc) 45%,#15171c)}}
.gw.tap,.gw:nth-child(even of .gw).tap{background:var(--hl);transform:scale(.985);transition-duration:.05s}
.gw.tap .gn,.gw.tap .gr{color:#15171c}
.gw.tap .gi{background:rgba(0,0,0,.10);color:#15171c;border-color:transparent}
.gw.tap .gk{color:#15171c} .gw.tap .gk b{background:rgba(255,255,255,.75);color:color-mix(in srgb,var(--tc) 45%,#15171c)}
.card.now{-webkit-tap-highlight-color:transparent}
.card.now.tap{transform:scale(var(--tsx,.97),var(--tsy,.97));transition:transform .05s}   /* --tsx/--tsy are set in JS so the press shrinks every card by the same few pixels, tall or short */
.gw:hover .gn{text-decoration:underline;text-underline-offset:3px}
.gw.xtra{display:none} .gcard.all:not(.narrow) .gw.xtra{display:flex} .gcard.narrow .gw.mx{display:none}
.gi{width:26px;height:38px;border-radius:6px;flex:none;background:var(--chip);border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:.78rem;font-weight:600;color:var(--mut)}
img.gi{object-fit:cover;padding:0;display:block}
.gm{min-width:0;flex:1}
.gn{font-size:.95rem;font-weight:700;line-height:1.25;margin:0 0 3px}
.gcs{display:flex;flex-wrap:wrap;gap:3px 10px}
.gr{font-size:.74rem;flex:none;align-self:flex-start;margin-top:2px}
.gmore{display:flex;align-items:center;gap:6px;padding:8px 0 2px;border-top:1px solid var(--cline);font-size:.76rem;color:var(--mut)}
.gmore i{margin-left:auto;font-style:normal}
.gmore.btn{cursor:pointer;color:var(--fg);font-weight:600;background:none;border-left:0;border-right:0;border-bottom:0;width:100%;font-family:inherit;text-align:left}
.gcard.narrow .gmore.btn,.gcard:not(.narrow) .gmore.hint{display:none}
.lcols{column-gap:28px}
.lcols .gw{break-inside:avoid}
@media (min-width:732px){.lcols{column-count:2}}   /* two columns balanced by height (rows differ: 1-2 lines of theaters), same reading order */

/* nearest-first toggle: a pin in the view switcher; off by default, a soft yellow tint when on */
.mode{grid-auto-columns:auto}
.mode .gpin{display:flex;position:relative;width:38px;height:30px;margin-left:5px;border-radius:999px;align-items:center;justify-content:center}
.mode .gpin::before{content:"";position:absolute;left:-4px;top:7px;bottom:7px;width:1px;background:var(--cline)}
.mode .gpin svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:2}
.mode .gpin[aria-pressed="true"]{background:color-mix(in srgb,#f5b301 15%,transparent);color:color-mix(in srgb,#f5b301 65%,var(--mut));box-shadow:none}
@media (prefers-color-scheme:light){.mode .gpin[aria-pressed="true"]{background:color-mix(in srgb,var(--acc) 16%,transparent);color:var(--acc)}}
.gpin.busy svg{animation:gpb .8s ease-in-out infinite alternate}
@keyframes gpb{to{opacity:.35}}
.gtoast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--chip);color:var(--fg);padding:7px 14px;border-radius:999px;font-size:.8rem;box-shadow:0 2px 10px rgba(0,0,0,.3);z-index:60}
/* distances (only while the toggle is on): cards on mouse devices on hover, popup everywhere; kept pale */
.th{position:relative}
.thn .dist{display:none}
.times .dist{display:none;margin-left:auto;align-self:flex-end;font-size:.72rem;font-weight:400;color:var(--fg);white-space:nowrap;padding:0 1px 1px 8px;line-height:1.2;opacity:0;transition:opacity .15s}
@media (hover:hover) and (pointer:fine){
  body.geoOn .times .dist{display:inline}
  body.geoOn .card.now:hover .times .dist{opacity:.45}}
.sdist{margin-left:auto;align-self:flex-end;font-size:.72rem;font-weight:400;color:var(--fg);opacity:.3;white-space:nowrap;padding:0 2px 2px 8px}
@media (hover:hover) and (pointer:fine){.sdist{opacity:.45}}
/* titles: highest contrast; list rows: quieter posters, theaters and times so the title leads */
:root{--ttl:#eceef2;--hl:color-mix(in srgb,#f5b301 90%,var(--card))}
@media (prefers-color-scheme:light){:root{--ttl:#000;--hl:color-mix(in srgb,#f5b301 70%,var(--card))}}
.gn,.card h3,.stitle h3{color:var(--ttl)}
img.gi{opacity:.6;transition:opacity .15s}
.gw .gcs{opacity:.7}
@media (hover:hover){.gw:hover img.gi{opacity:1} .gw:hover .gcs{opacity:1}}
.gw.tap img.gi,.gw.tap .gcs{opacity:1}
.sth[data-th="laquinta"] .sthn i{color:#15171c}
@media (prefers-color-scheme:light){.sth[data-th="laquinta"] .sthn i{color:#fff}}
[hidden]{display:none!important}
"""

JS = """
/* ---- visit statistics: anonymous counts of actions (GoatCounter). Nothing personal, no coordinates, no film titles ---- */
(function(){
  var live=!!document.querySelector('script[data-goatcounter]'), q=[], waiting=false, tries=0, errN=0;
  /* if the statistics script is blocked (ad blocker, offline, private mode) everything here quietly does nothing */
  function stop(){ live=false; q.length=0; waiting=false; }
  var tag=document.querySelector('script[data-goatcounter]'); if(tag) tag.addEventListener('error',stop);
  function flush(){
    try{
      var g=window.goatcounter;
      if(g && typeof g.count==='function'){ while(q.length){ var a=q.shift(); g.count({path:a[0], title:a[1], event:true}); } waiting=false; }
      else if(++tries<40){ waiting=true; setTimeout(flush,500); }
      else stop();
    }catch(e){ stop(); }
  }
  /* once=true: at most once per visit (browser tab session) */
  window.__ev=function(path,title,once){
    try{
      if(!live) return;
      if(once){ try{ var k='ev:'+(typeof once==='string'?once:path); if(sessionStorage.getItem(k)) return; sessionStorage.setItem(k,'1'); }catch(e){} }
      q.push([path,title||path]); if(!waiting) flush();
    }catch(e){}
  };
  var ev=window.__ev, B=document.body;
  /* who: new or returning (the first-visit hint flag already exists for the icon tour), theme, installed app */
  var ret=false; try{ ret=!!localStorage.getItem('viewHint'); }catch(e){}
  ev(ret?'visit/returning':'visit/new', ret?'Returning visitor':'New visitor', 'visit');
  ev(window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'theme/dark':'theme/light', 'Theme', true);
  if((window.matchMedia && matchMedia('(display-mode: standalone)').matches) || navigator.standalone) ev('pwa/standalone','Opened from the home screen',true);
  if(!navigator.geolocation) ev('geo/unsupported','Location not supported',true);
  /* the popup with the full schedule opened */
  var sh=document.getElementById('sheet');
  if(sh && window.MutationObserver) new MutationObserver(function(){ if(!sh.hidden) ev('popup/open','Schedule popup opened'); }).observe(sh,{attributes:true,attributeFilter:['hidden']});
  /* clicks: theater link, ticket link, which showtime and format, and which place in the list (nearest first) */
  var laTime=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',hour:'numeric',minute:'2-digit',hourCycle:'h23'});
  function nowMin(){ var p=laTime.formatToParts(new Date()), h=0, m=0; p.forEach(function(x){ if(x.type==='hour') h=+x.value%24; if(x.type==='minute') m=+x.value; }); return h*60+m; }
  document.addEventListener('click',function(e){
    var t=e.target; if(!t || !t.closest) return;
    var blk=t.closest('a.th, .sth'); if(!blk || !blk.dataset.th) return;
    var th=blk.dataset.th, surface=blk.classList.contains('sth')?'popup':'card';
    var chip=t.closest('.t, .c');
    if(chip && chip.classList.contains('past')){ return; }
    if(chip && chip.dataset.buy) ev('ticket/'+th, 'Ticket link: '+th+' ('+surface+')');
    else ev('theater/open/'+th, 'Theater page: '+th+' ('+surface+')');
    if(B.classList.contains('geoOn')){      /* with nearest-first on: which place in the list did they pick */
      var sib=[].slice.call(blk.parentNode.querySelectorAll(blk.classList.contains('sth')?':scope > .sth':':scope > a.th')).filter(function(x){ return !x.hidden; });
      var rank=sib.indexOf(blk)+1; if(rank>0) ev('geo/rank/'+rank, 'Nearest-first: place in the list picked ('+rank+')');
    }
    if(chip){
      var txt=chip.textContent||'', tm=txt.match(/(\\d{1,2}):(\\d\\d)\\s*([AP])M/);
      var label=txt.replace(/(\\d{1,2}):(\\d\\d)\\s*([AP])M/,'').replace(/[^A-Za-z0-9 -]/g,'').trim().replace(/ +/g,'-').toLowerCase().slice(0,16);
      ev('format/click/'+(label||'regular'), 'Showtime format clicked');
      if(tm){
        var h=(+tm[1])%12+(tm[3]==='P'?12:0), mins=h*60+(+tm[2]);
        var tomorrow=!!chip.closest('.d1, .tm');
        ev('showtime/slot/'+(h<12?'morning':h<17?'afternoon':h<21?'evening':'late'), 'Showtime clicked: time of day');
        var lead=tomorrow?'tomorrow':(mins-nowMin()<60?'under-1h':mins-nowMin()<180?'1-3h':'later-today');
        ev('showtime/lead/'+lead, 'Showtime clicked: how soon');
      }
    }
  },true);
  /* problems on the visitor's side (message only, at most 3 per visit) */
  window.addEventListener('error',function(e){ if(++errN<=3) ev('error/js','JS error: '+String(e.message||'').slice(0,80)+' line '+(e.lineno||0)); });
})();
(function(){
  var B=document.body, d0=B.dataset.d0, d1=B.dataset.d1, ok=(B.dataset.tmrwOk||'').split(',');
  var day=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Los_Angeles'}).format(new Date());
  var now=Date.now()/1000, cards=document.querySelectorAll('.card.now');
  var hide=function(el){ if(el) el.hidden=true; };
  var stale=(day!==d0 && day!==d1);
  if(stale) document.getElementById('stale').hidden=false;
  var rolled=!stale && (day===d1);            /* past midnight: yesterday's "tomorrow" is today now */
  if(rolled){ var s=document.getElementById('daylabel'); if(s) s.textContent=B.dataset.d1nice; }
  var allOk=!rolled && ok.length>=+B.dataset.nth;
  if(!stale) cards.forEach(function(c){
    var upcoming=0, hadToday=0, tomorrow=0;
    c.querySelectorAll('.th').forEach(function(r){
      var a=r.querySelector('.times.d0'), b=r.querySelector('.times.d1'), up=0;
      if(rolled){ hide(a); a=b; b=null; if(a){ a.classList.remove('d1'); hide(a.querySelector('.lab')); } }
      if(a){ a.querySelectorAll('.t').forEach(function(t){ hadToday++; if(+t.dataset.ts<now) t.classList.add('past'); else up++; }); }
      r._today=a; r._tmrw=b;
      if(r.dataset.u1 && (rolled || (!up && b))) r.href=r.dataset.u1;
      if(b){ tomorrow+=b.querySelectorAll('.t').length; if(up) b.hidden=true; }
      upcoming+=up;
      if(!a && !b) r.hidden=true;
      else if(!up) r.classList.add('off');
    });
    var st=c.querySelector('.status'), msg='';
    if(rolled && !hadToday){ c.hidden=true; return; }
    if(upcoming){
      c.style.order=0;
      if(allOk && !tomorrow){ msg='Not scheduled tomorrow*'; c.classList.add('tonly'); }
    } else if(tomorrow){
      c.style.order=1; c.classList.add('later');
      msg=hadToday ? 'No more showings today' : 'Not showing today';
    } else {
      c.style.order=2; c.classList.add('done');
      msg=allOk ? 'No more showings today \u00b7 not scheduled tomorrow*' : 'No more showings today';
    }
    if(msg){ st.textContent=msg; st.hidden=false; }
  });
  if(stale) cards.forEach(function(c){ c.querySelectorAll('.th').forEach(function(r){
    r._today=r.querySelector('.times.d0'); r._tmrw=r.querySelector('.times.d1'); }); });
  if(!document.querySelector('.card.now:not([hidden])')) document.getElementById('none').hidden=false;

  /* masonry: keep the order (showing today -> only tomorrow -> done, each by rating), drop each card
     into the currently shortest column; 2 columns on phones, 3 on tablets, 4 on desktops */
  var grid=document.getElementById('nowgrid'), VIEW={mode:'cards',box:null,layout:null};
  if(grid){
    var list=Array.prototype.slice.call(grid.querySelectorAll(':scope > .card.now'));
    list.forEach(function(c,i){ c._i=i; });
    list.sort(function(a,b){ return ((+a.style.order||0)-(+b.style.order||0)) || (a._i-b._i); });
    var ncol=0;
    var layout=function(force){
      var w=grid.clientWidth; if(!w) return;           /* hidden (list view): lay out when shown again */
      var n=w<700?2:(w<980?3:4);
      if(n===ncol && !force) return; ncol=n;
      var gb=VIEW.box; if(gb && gb.parentNode) gb.parentNode.removeChild(gb);
      grid.classList.add('masonry'); grid.textContent='';
      var cols=[];
      for(var i=0;i<n;i++){ var col=document.createElement('div'); col.className='mcol'; grid.appendChild(col); cols.push(col); }
      if(gb){ gb.classList.toggle('narrow',n===2); gb.classList.toggle('inmas',n>2);
        if(VIEW.mode==='posters'){ if(n>2) cols[0].appendChild(gb); else grid.parentNode.insertBefore(gb,grid); } }
      list.forEach(function(c){
        if(c.hidden) return;
        var k=0; for(var j=1;j<n;j++) if(cols[j].offsetHeight<cols[k].offsetHeight) k=j;
        cols[k].appendChild(c);
      });
    };
    layout(); VIEW.layout=layout; VIEW.list=list;
    var rt; window.addEventListener('resize', function(){ clearTimeout(rt); rt=setTimeout(layout,120); });
  }

  /* a page kept open (or a home-screen app resumed hours later) reloads itself to get fresh times */
  var loaded=Date.now();
  function maybeReload(){ if(document.visibilityState==='visible' && Date.now()-loaded>10*60*1000) location.reload(); }
  document.addEventListener('visibilitychange', maybeReload);
  window.addEventListener('pageshow', function(e){ if(e.persisted) maybeReload(); });

  /* ---- popup with the full schedule, opened from the poster ---- */
  var sheet=document.getElementById('sheet'), sb=sheet.querySelector('.sheet-body'), sbox=sheet.querySelector('.sheet-box');
  function more(){ sbox.classList.toggle('more', sb.scrollHeight-sb.scrollTop-sb.clientHeight>4); }
  sb.addEventListener('scroll', more, {passive:true});
  window.addEventListener('resize', function(){ if(!sheet.hidden) more(); });
  function el(tag,cls,txt){ var e=document.createElement(tag); if(cls) e.className=cls; if(txt!=null) e.textContent=txt; return e; }
  function chips(group,label){
    var w=el('div','sgrp'+(label==='Tomorrow'?' tm':'')); w.appendChild(el('div','slab',label));
    var box=el('div','schips');
    group.querySelectorAll('.t').forEach(function(t){
      var c=el('span','c'+(t.classList.contains('past')?' past':'')+(t.classList.contains('imax')?' imax':'')+(t.classList.contains('fmt')?' fmt':''));
      c.innerHTML=t.innerHTML; if(t.dataset.buy && !t.classList.contains('past')) c.dataset.buy=t.dataset.buy;
      box.appendChild(c);
    });
    w.appendChild(box); return w;
  }
  var tf=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',hour:'numeric',minute:'2-digit'});
  function dur(m){ m=+m; return Math.floor(m/60)+'h '+(m%60<10?'0':'')+(m%60)+'m'; }
  /* large poster over the popup (w780 from TMDB; the small w342 one shows until it loads) */
  var zoom=null;
  function loadBig(pc){
    if(pc._big) return pc._big;
    var src=pc.src.replace('/w342/','/w780/'); if(src===pc.src) return null;
    var bi=new Image(); bi.decoding='async'; bi.src=src; pc._big=bi;
    bi._wait=(bi.decode ? bi.decode() : new Promise(function(r,j){ bi.onload=r; bi.onerror=j; })).then(function(){ bi._ready=true; },function(){});
    return bi;
  }
  function zoomOut(){ if(!zoom) return; var z=zoom; zoom=null; z.classList.remove('on'); setTimeout(function(){ z.remove(); },200); }
  function zoomIn(pc){
    zoomOut(); var z=el('div','zoom'); z.setAttribute('role','dialog'); z.setAttribute('aria-label','Poster');
    var big=loadBig(pc), im;
    if(big && big.complete && big.naturalWidth && big._ready){ im=big; }    /* preloaded and decoded: use it as is */
    else{
      im=el('img'); im.src=pc.src;                                     /* small one now, the sharp one once decoded */
      if(big) (big._wait || Promise.resolve()).then(function(){ if(big.naturalWidth && im.parentNode) im.src=big.src; });
    }
    im.alt='';
    var x=el('button','zx'); x.type='button'; x.setAttribute('aria-label','Close');
    x.innerHTML='<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M2 2l10 10M12 2L2 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>';
    z.appendChild(im); z.appendChild(x); z.addEventListener('click',zoomOut);
    document.body.appendChild(z); zoom=z; void z.offsetWidth;          /* start the transition from the first frame */
    requestAnimationFrame(function(){ z.classList.add('on'); }); x.focus();
  }
  function open(card){
    sb.innerHTML='';
    var head=el('div','shead'), pv=card.querySelector(':scope > img, :scope > .poster');
    if(pv){
      var pc=pv.cloneNode(true); head.appendChild(pc);
      if(pc.tagName==='IMG'){
        /* load and decode the large poster in the background (not with Data Saver on: then it loads on tap) */
        if(!(navigator.connection && navigator.connection.saveData)) loadBig(pc);
        pc.setAttribute('role','button'); pc.tabIndex=0; pc.setAttribute('aria-label','Show poster larger');
        pc.addEventListener('click',function(e){ e.stopPropagation(); zoomIn(pc); });
        pc.addEventListener('keydown',function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); zoomIn(pc); } });
      }
    }
    var ht=el('div','stitle'); ht.appendChild(el('h3',null,card.querySelector('h3').textContent));
    ht.appendChild(card.querySelector('.meta').cloneNode(true));
    var info=[card.dataset.genre, card.dataset.rt ? dur(card.dataset.rt) : ''].filter(Boolean).join(' \u00b7 ');
    if(info) ht.appendChild(el('div','sinfo',info));
    var st=card.querySelector('.status'); if(st && !st.hidden) ht.appendChild(el('div','sstatus',st.textContent));
    /* nearest upcoming showing across theaters */
    var best=null;
    card.querySelectorAll('.th').forEach(function(r){
      [[r._today,''],[r._tmrw,'Tomorrow ']].forEach(function(p){
        if(!p[0]) return;
        p[0].querySelectorAll('.t:not(.past)').forEach(function(t){
          var ts=+t.dataset.ts; if(ts<now) return;
          if(!best || ts<best.ts) best={ts:ts, day:p[1], th:r.querySelector('.full').textContent};
        });
      });
    });
    if(best){
      var nx=el('div','snext'), txt='Next: '+best.day+tf.format(new Date(best.ts*1000))+' \u00b7 '+best.th;
      if(card.dataset.rt) txt+=' \u00b7 ends ~'+tf.format(new Date((best.ts+card.dataset.rt*60)*1000));
      nx.textContent=txt; ht.appendChild(nx);
    }
    head.appendChild(ht); sb.appendChild(head);
    card.querySelectorAll('.th').forEach(function(r){
      if(!r._today && !r._tmrw) return;
      var box=el('a','sth'); box.dataset.th=r.dataset.th; box.href=r.href; box.target='_blank'; box.rel='noopener';
      var hd=el('div','sthn'); hd.appendChild(el('span',null,r.querySelector('.full').textContent));
      hd.appendChild(el('i',null,'Open \u2197')); box.appendChild(hd);
      if(r._today) box.appendChild(chips(r._today,'Today'));
      if(r._tmrw) box.appendChild(chips(r._tmrw,'Tomorrow'));
      if(r.dataset.dist){ var lc=box.querySelectorAll('.schips'); if(lc.length) lc[lc.length-1].appendChild(el('span','sdist',r.dataset.dist)); }
      sb.appendChild(box);
    });
    sb.appendChild(el('p','snote','* Showtimes are collected automatically and can change. Please confirm on the theater\u2019s site before you go.'));
    sheet.hidden=false; document.documentElement.style.overflow='hidden';
    sb.scrollTop=0; more();
    [].forEach.call(sb.querySelectorAll('img'), function(im){ if(!im.complete) im.addEventListener('load', more); });
  }
  function close(){ zoomOut(); sheet.hidden=true; document.documentElement.style.overflow=''; }
  document.addEventListener('click',function(e){
    if(e.target.closest('.sheet-bg, .sheet-x')){ close(); return; }
    var bt=e.target.closest('.t[data-buy], .c[data-buy]');   /* a showtime with its own ticket link */
    if(bt && !bt.classList.contains('past') && /^https:[/][/]/i.test(bt.dataset.buy)){ e.preventDefault(); window.open(bt.dataset.buy,'_blank','noopener'); return; }
    if(e.target.closest('a')) return;            /* theater block: follow the link */
    var c=e.target.closest('.card.now');
    if(c) pressOpen(c,c);
  });
  /* tap feedback: the pressed card or row lights up (not while scrolling), and the popup opens
     a moment later, so even a quick tap is seen */
  var pressT=null, pressEl=null, pressX=0, pressY=0;
  function pressable(t){ return !t.closest('a, button, .t[data-buy], .c[data-buy], #sheet, .zoom') && t.closest('.gw, .card.now'); }
  function unpress(el,wait){ setTimeout(function(){ el.classList.remove('tap'); }, wait||0); }
  /* safety net: a pressed look must never outlive the touch (a 2nd finger, a scroll grab or a cancelled
     touch used to leave a card stuck shrunk with a dark border) */
  function pressSize(w){   /* same absolute inset on every side (3px), not 3% of the card: a tall card used to shrink by tens of pixels */
    if(!w.classList.contains('card')) return;
    var r=w.getBoundingClientRect(); if(r.width<40 || r.height<40) return;
    w.style.setProperty('--tsx',(1-6/r.width).toFixed(4)); w.style.setProperty('--tsy',(1-6/r.height).toFixed(4));
  }
  function clearTaps(){ clearTimeout(pressT); pressEl=null; [].forEach.call(document.querySelectorAll('.card.now.tap, .gw.tap'),function(x){ x.classList.remove('tap'); }); }
  document.addEventListener('pointerdown',function(e){
    clearTaps();
    var tb=e.target.closest && e.target.closest('.th');   /* theater block: fixed 3px press shrink, not 2% of its height */
    if(tb){ var tr=tb.getBoundingClientRect(); if(tr.width>=40 && tr.height>=20){ tb.style.setProperty('--thx',(1-3/tr.width).toFixed(4)); tb.style.setProperty('--thy',(1-3/tr.height).toFixed(4)); } }
    var w=pressable(e.target); if(!w) return;
    pressEl=w; pressX=e.clientX; pressY=e.clientY; w._t=0;
    pressT=setTimeout(function(){ pressSize(w); w.classList.add('tap'); w._t=Date.now(); },70);
  },true);
  document.addEventListener('pointermove',function(e){
    if(pressEl && (Math.abs(e.clientX-pressX)>8 || Math.abs(e.clientY-pressY)>8)){ clearTimeout(pressT); pressEl.classList.remove('tap'); pressEl=null; }
  },true);
  function release(){ clearTimeout(pressT); if(pressEl && pressEl._t) unpress(pressEl, Math.max(0,180-(Date.now()-pressEl._t))); pressEl=null; }
  document.addEventListener('pointerup',release,true);
  document.addEventListener('pointercancel',clearTaps,true);
  document.addEventListener('touchcancel',clearTaps,true);
  window.addEventListener('scroll',function(){ if(!pressOpening) clearTaps(); },{passive:true});
  window.addEventListener('pagehide',clearTaps); window.addEventListener('pageshow',clearTaps);
  document.addEventListener('visibilitychange',function(){ if(document.hidden) clearTaps(); });
  var pressOpening=false;
  function pressOpen(fl,card){
    pressSize(fl); fl.classList.add('tap'); fl._t=fl._t||Date.now(); pressOpening=true;
    setTimeout(function(){ pressOpening=false; open(card); unpress(fl,80); },120);
  }
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape' && zoom){ zoomOut(); return; }
    if(e.key==='Escape' && !sheet.hidden){ close(); return; }
    if((e.key==='Enter'||e.key===' ') && e.target.classList && e.target.classList.contains('now')){ e.preventDefault(); open(e.target); }
  });

  /* ---- theater order: fixed west -> east; theaters with nothing left to show today sink to the bottom of each card;
         optional "nearest first" pin (off by default, remembered; distances appear only while it is on) ---- */
  (function(){
    var GEO={}; try{ GEO=JSON.parse(B.dataset.geo||'{}'); }catch(e){}
    var keys=Object.keys(GEO); if(!keys.length) return;
    var WEST=keys.slice(), pin=document.getElementById('geo'), cap=document.getElementById('modecap'), dist={}, on=false;
    if(pin && !navigator.geolocation) pin.hidden=true;
    function mi(a,b){ var R=3958.8,t=Math.PI/180,dl=(b[0]-a[0])*t,dn=(b[1]-a[1])*t,
      h=Math.sin(dl/2)*Math.sin(dl/2)+Math.cos(a[0]*t)*Math.cos(b[0]*t)*Math.sin(dn/2)*Math.sin(dn/2); return 2*R*Math.asin(Math.sqrt(h)); }
    function label(d){ return (d<10 ? d.toFixed(1).replace(/\\.0$/,'') : Math.round(d))+' mi'; }
    function later(k){ return (k.classList.contains('off') || k.classList.contains('tm') || k.classList.contains('px')) ? 100 : 0; }
    function sortKids(parent, sel, rank){
      var kids=[].slice.call(parent.querySelectorAll(sel)).filter(function(k){ return k.parentNode===parent; });
      kids.sort(function(a,b){ return (rank[a.dataset.th]+later(a))-(rank[b.dataset.th]+later(b)); }).forEach(function(k){ parent.appendChild(k); });
    }
    function capText(){ if(!cap || cap.classList.contains('peek')) return;
      cap.textContent=cap.textContent.split(' · ')[0]+(on?' · nearest first':''); }
    window.__capText=capText;
    function order(){
      var o=on ? keys.slice().sort(function(a,b){ return dist[a]-dist[b]; }) : WEST, rank={};
      o.forEach(function(k,i){ rank[k]=i; });
      document.querySelectorAll('.card.now .body').forEach(function(b){ sortKids(b,'a.th',rank); });
      document.querySelectorAll('.gcs').forEach(function(g){ sortKids(g,'[data-th]',rank); });
      document.querySelectorAll('.gleg').forEach(function(g){
        [].slice.call(g.children).sort(function(a,b){ return rank[a.dataset.th]-rank[b.dataset.th]; }).forEach(function(k){ g.appendChild(k); }); });
      document.querySelectorAll('a.th').forEach(function(r){
        /* the distance sits at the end of the last visible row of showtimes: it shares the row when there is room, and otherwise wraps to its own line, so the card gets taller only when it has to */
        var s=r.querySelector('.dist'); if(!s) s=el('span','dist');
        var gs=[].slice.call(r.querySelectorAll('.times')).filter(function(g){ return !g.hidden; }), lg=gs[gs.length-1];
        if(lg && s.parentNode!==lg) lg.appendChild(s);
        s.textContent=on ? label(dist[r.dataset.th]) : ''; if(on) r.dataset.dist=s.textContent; else delete r.dataset.dist;
      });
      B.classList.toggle('geoOn',on);
      if(pin) pin.setAttribute('aria-pressed',on?'true':'false');
      capText();
      if(VIEW.layout) VIEW.layout(true);
      window.__geoSort={on:on, order:o};   /* for checks */
    }
    window.__geoOrder=order;
    function toast(t){ var d=el('div','gtoast',t); document.body.appendChild(d); setTimeout(function(){ d.remove(); },2600); }
    function off(){ on=false; try{ localStorage.removeItem('geoOn'); }catch(e){} order(); }
    function locate(quiet){
      if(!navigator.geolocation) return;
      if(pin) pin.classList.add('busy');
      navigator.geolocation.getCurrentPosition(function(p){
        if(pin) pin.classList.remove('busy');
        var pos=[p.coords.latitude,p.coords.longitude]; keys.forEach(function(k){ dist[k]=mi(pos,GEO[k]); });
        on=true; try{ localStorage.setItem('geoOn','1'); }catch(e){} order();
        if(window.__ev){
          __ev(quiet?'geo/auto-ok':'geo/granted', quiet?'Location: refreshed on return':'Location allowed', quiet);
          var near=Math.min.apply(null,keys.map(function(k){ return dist[k]; }));
          __ev('geo/zone/'+(near<10?'under-10mi':near<30?'10-30mi':'over-30mi'), 'Distance to the nearest theater (rough)', true);
        }
      }, function(err){
        if(pin) pin.classList.remove('busy');
        if(window.__ev) __ev((quiet?'geo/auto-':'geo/')+(err && err.code===1 ? 'denied':'error'), 'Location not available', quiet);
        off(); if(!quiet) toast(err && err.code===1 ? 'Location is blocked for this site' : 'Could not get your location');
      }, {timeout:8000, maximumAge:600000});
    }
    if(pin) pin.addEventListener('click',function(){ if(on){ if(window.__ev) __ev('geo/off','Nearest-first switched off'); off(); } else { if(window.__ev) __ev('geo/on','Nearest-first switched on'); locate(false); } });
    order();                                                    /* default: west -> east, nothing about distance */
    var want=false; try{ want=localStorage.getItem('geoOn')==='1'; }catch(e){}
    if(want) locate(true);                                      /* switched on earlier: refresh the position on this visit */
  })();

  /* ---- views: Showtimes (cards) / Posters (posters + short list card) / List (one card, every movie) ---- */
  (function(){
    var mode=document.getElementById('mode'), cap=document.getElementById('modecap');
    if(!mode || !grid || !VIEW.layout) return;
    var btns=[].slice.call(mode.querySelectorAll('button[data-m]')), CAPS={cards:'Showtimes',posters:'Posters',list:'List'};
    var cards=VIEW.list.filter(function(c){ return !c.hidden; }), N=7, NP=5;   /* rows in the poster-view card: wide screens / phones */
    if(!cards.length){ mode.hidden=true; cap.hidden=true; return; }
    var L={festival:'F',cinemark:'C',regal:'R',pickford:'M',laquinta:'L'};
    var sf=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',hour:'numeric',minute:'2-digit'});
    function st(ts){ return sf.format(new Date(ts*1000)).replace(' AM','a').replace(' PM','p').replace(':00',''); }
    function legend(){ var g=el('div','gleg');
      THEATERS.forEach(function(x){ var s=el('span','gk'); s.dataset.th=x[0]; s.appendChild(el('b',null,L[x[0]])); s.appendChild(document.createTextNode(x[1])); g.appendChild(s); });
      return g; }
    var THEATERS=[['festival','Festival'],['pickford','Pickford'],['cinemark','Cinemark'],['regal','Regal'],['laquinta','La Quinta']];
    function row(c,xtra){
      var w=el('div','gw'+(xtra?' xtra':'')), ttl=c.querySelector('h3').textContent;
      var pim=c.querySelector(':scope > img');
      if(pim && pim.getAttribute('src')){                     /* tiny poster (w92) instead of the letter; loads when scrolled into view */
        var th=el('img','gi'); th.alt=''; th.loading='lazy'; th.decoding='async'; th.src=pim.getAttribute('src').replace('/w342/','/w92/');
        th.addEventListener('error',function(){ var f=el('div','gi',ttl.charAt(0)); th.replaceWith(f); });
        w.appendChild(th);
      } else w.appendChild(el('div','gi',ttl.charAt(0)));
      var m=el('div','gm'); m.appendChild(el('div','gn',ttl));
      var cs=el('div','gcs');
      c.querySelectorAll('.th').forEach(function(r){
        if(r.hidden) return;
        var a=r._today?[].slice.call(r._today.querySelectorAll('.t')):[], b=r._tmrw?[].slice.call(r._tmrw.querySelectorAll('.t')):[];
        var nx=a.filter(function(t){ return !t.classList.contains('past') && +t.dataset.ts>=now; })[0], cls='gk', t=nx, pre='';
        if(!t && b.length){ t=b[0]; cls+=' tm'; pre='tomorrow '; }
        else if(!t && a.length){ t=a[a.length-1]; cls+=' px'; }
        if(!t) return;
        var s=el('span',cls); s.dataset.th=r.dataset.th; s.appendChild(el('b',null,L[r.dataset.th]||'?'));
        s.appendChild(document.createTextNode(pre+st(+t.dataset.ts))); cs.appendChild(s);
      });
      m.appendChild(cs); w.appendChild(m);
      var rt=c.querySelector('.rating');
      w.appendChild(el('div','gr rating'+(rt && rt.classList.contains('few')?' few':''), rt && !rt.classList.contains('none') ? rt.textContent : ''));
      w.addEventListener('click',function(){ pressOpen(w,c); });
      return w;
    }
    var SUB='Next showing at each theater';
    /* poster view card: 7 movies; on wider screens a button shows the rest */
    var box=el('div','gcard'); box.appendChild(legend()); box.appendChild(el('div','gsub',SUB));
    cards.forEach(function(c,k){ var w=row(c,k>=N); if(k>=NP) w.classList.add('mx'); box.appendChild(w); });
    if(cards.length>N){
      var bt=el('button','gmore btn'); bt.type='button';
      var setBt=function(on){ bt.innerHTML=''; bt.appendChild(el('span',null,on?'Show less':'Show all '+cards.length)); bt.appendChild(el('i',null,on?'↑':'↓')); };
      setBt(false);
      bt.addEventListener('click',function(){ setBt(box.classList.toggle('all')); VIEW.layout(true); });
      box.appendChild(bt);
    }
    if(cards.length>NP){
      var hint=el('div','gmore hint'); hint.appendChild(el('span',null,'+ '+(cards.length-NP)+' more · tap a poster below')); hint.appendChild(el('i',null,'↓'));
      box.appendChild(hint);
    }
    VIEW.box=box; setTimeout(function(){ if(window.__geoOrder) window.__geoOrder(); },0);   /* lists exist now: apply the theater order to them too */
    /* list view: one card, every movie (two columns inside it on wide screens) */
    var lv=el('div','glist'); lv.appendChild(legend()); lv.appendChild(el('div','gsub',SUB));
    var lc=el('div','lcols');
    cards.forEach(function(c){ lc.appendChild(row(c,false)); });
    lv.appendChild(lc); grid.parentNode.insertBefore(lv,grid);

    var timers=[];
    function stopTour(){ timers.forEach(clearTimeout); timers=[]; btns.forEach(function(b){ b.classList.remove('tour'); }); }
    function apply(m,save){
      VIEW.mode=m; B.classList.toggle('pm',m==='posters'); B.classList.toggle('lm',m==='list');
      btns.forEach(function(b){ b.setAttribute('aria-pressed',String(b.dataset.m===m)); });
      cap.textContent=CAPS[m]; if(window.__capText) window.__capText();
      if(save){ try{ localStorage.setItem('view',m); }catch(e){} }
      if(m!=='list') VIEW.layout(true);
    }
    btns.forEach(function(b){ b.addEventListener('click',function(){ stopTour(); apply(b.dataset.m,true); cap.classList.remove('peek'); if(window.__ev) __ev('view/switch/'+b.dataset.m,'View switched to '+b.dataset.m); }); });
    /* mouse hover: the caption shows the name of the icon under the pointer */
    if(window.matchMedia && matchMedia('(hover: hover)').matches) btns.forEach(function(b){
      b.addEventListener('mouseenter',function(){ if(b.getAttribute('aria-pressed')!=='true'){ cap.textContent=CAPS[b.dataset.m]; cap.classList.add('peek'); } });
      b.addEventListener('mouseleave',function(){ cap.textContent=CAPS[VIEW.mode]; cap.classList.remove('peek'); if(window.__capText) window.__capText(); });
    });
    var saved=null; try{ saved=localStorage.getItem('view'); }catch(e){}
    var h=location.hash.slice(1);
    apply(CAPS[h]?h:(CAPS[saved]?saved:'cards'),false);
    if(window.__ev) __ev('view/load/'+VIEW.mode,'View on load: '+VIEW.mode,true);

    /* first visit only: light up the inactive icons one by one (no caption change) */
    var seen=true; try{ seen=!!localStorage.getItem('viewHint'); localStorage.setItem('viewHint','1'); }catch(e){}
    var calm=window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    if(!seen && !calm){
      var seq=btns.filter(function(b){ return b.getAttribute('aria-pressed')!=='true'; });
      seq.forEach(function(b,i){ timers.push(setTimeout(function(){
        btns.forEach(function(x){ x.classList.toggle('tour',x===b); }); },1500+i*600)); });
      timers.push(setTimeout(stopTour,1500+seq.length*600));
    }
  })();

  /* tap hint, until the visitor opens a card (popup, theater or ticket link): a short line under "Now playing" and a wave
     of light. Every view (Showtimes / Posters / List) has its own timer and its own state:
       Showtimes: a wave over the theater blocks on screen (left to right), the first one after a short start delay, then
                  more with growing pauses (15 s, 20 s, 25 s; the 2nd visit stops after 20 s) so it never turns into a pattern;
       Posters / List: one wave over the movie rows, first to last, 80 ms apart (once per view).
     Waves wait for something to show (blocks below the fold, scrolling, popup open) and look again every 1.5 s.
     Waves: 1st visit 4 in Showtimes, 2nd visit 3, then none; the first click ends them for good.
     The line under the title disappears with the first click.
     window.__hintCfg (preview only) overrides: start, pauses [s], speed, str, bri, sat, flow (ms), step (ms), lift, line, wave */
  (function(){
    var T=window.__hintCfg, cfg=T||{speed:.7,str:1.025,bri:1.06,sat:1,flow:600,step:80,lift:0,start:5,line:true,wave:true};
    var calm=window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    var visit=1, off=false;     /* waves: 1st visit 4 in Showtimes, 2nd visit 3, then none; a click ends them for good */
    if(!T) try{
      if(localStorage.getItem('tapHint')==='1') off=true;
      else { var n=+localStorage.getItem('tapHintN')||0; if(n>=2) off=true; else { visit=n+1; localStorage.setItem('tapHintN',String(n+1)); } }
    }catch(e){}
    var sheet=document.getElementById('sheet'), line=document.getElementById('taphint'), B=document.body, lastScroll=0, timer=null, cur='', modes={}, gone=false;
    if(!off && window.__ev) __ev('hint/shown','Tap hint shown',true);
    function modeKey(){ return B.classList.contains('lm')?'list':B.classList.contains('pm')?'posters':'cards'; }
    function state(k){ return modes[k]||(modes[k]={waves:0,done:false}); }
    function lineText(){ return modeKey()!=='cards' ? 'Tap a movie to see showtimes and buy tickets' : 'Tap a showtime to buy tickets'; }
    function showLine(on){ if(line){ line.hidden=!on; if(on) line.textContent=lineText(); } }
    function clear(){ [].forEach.call(document.querySelectorAll('.nudge'),function(x){ x.classList.remove('nudge'); }); }
    function understood(){ if(T) return; try{ localStorage.setItem('tapHint','1'); }catch(e){} clearTimeout(timer); timer=null; for(var k in modes) modes[k].done=true; gone=true; clear(); showLine(false); }
    function blocks(){
      var vh=window.innerHeight, vw=window.innerWidth, out=[];
      [].forEach.call(document.querySelectorAll('.card.now a.th, .gw'),function(e){
        if(e.offsetParent===null) return;
        var r=e.getBoundingClientRect(), cy=r.top+r.height/2;
        if(r.height>0 && cy>60 && cy<vh-60 && r.right>0 && r.left<vw) out.push([e,r,cy]);
      });
      return out;
    }
    function wave(){
      var c=blocks(); if(!c.length) return 0;
      var k=cfg.speed||1, fl=cfg.flow==null?600:cfg.flow, step=cfg.step==null?80:cfg.step;
      var th=c.filter(function(x){ return !x[0].classList.contains('gw'); }), rows=c.filter(function(x){ return x[0].classList.contains('gw'); });
      clear();
      function go(x,d){
        var st=x[0].style;
        st.setProperty('--nd',Math.round(d)+'ms'); st.setProperty('--nt',Math.round(900/k)+'ms'); st.setProperty('--ns',String(cfg.str||1.025));
        st.setProperty('--nsa',String(cfg.sat||1)); st.setProperty('--nb',String(cfg.bri||1.06)); st.setProperty('--nl',String(cfg.lift==null?0:cfg.lift));
        void x[0].offsetWidth; x[0].classList.add('nudge');
      }
      /* theater blocks: left to right first (each movie card starts a little after its left neighbour), then top to bottom inside the card */
      if(th.length){
        var xl=Infinity, xr=-Infinity, top=Infinity, bot=-Infinity;
        th.forEach(function(x){ var cx=x[1].left+x[1].width/2; x.push(cx); xl=Math.min(xl,cx); xr=Math.max(xr,cx); top=Math.min(top,x[2]); bot=Math.max(bot,x[2]); });
        var sx=Math.max(1,xr-xl), sy=Math.max(1,bot-top);
        th.forEach(function(x){ go(x,((x[3]-xl)/sx*fl + (x[2]-top)/sy*350)/k); });
      }
      /* movie rows: strictly one after another, first to last (page order) */
      rows.forEach(function(x,i){ go(x,i*step/k); });
      return c.length;
    }
    function pauses(){ return cfg.pauses||(visit===1?[15,20,25]:[15,20]); }
    function tick(){
      timer=null;
      var m=state(cur), ok=false;
      if(!(cfg.wave===false || document.hidden || (sheet && !sheet.hidden) || Date.now()-lastScroll<800)) ok=wave()>0;
      if(ok) m.waves++;
      if(cur!=='cards'){ if(ok){ m.done=true; return; } }                       /* Posters / List: one wave */
      else if(ok && m.waves>pauses().length){ m.done=true; setTimeout(clear,2200); return; }   /* Showtimes: first wave + one per pause */
      /* next wave after the pause that follows this one; nothing to show yet: look again in 1.5 s */
      timer=setTimeout(tick, ok ? pauses()[m.waves-1]*1000 : 1500);
    }
    function startView(first){
      clearTimeout(timer); timer=null; clear();
      cur=modeKey(); var m=state(cur); showLine(!off && !gone && cfg.line!==false);
      if(m.done || calm || off) return;
      var wait = first ? (cfg.start==null?5:cfg.start) : (m.waves ? pauses()[Math.min(m.waves,pauses().length)-1] : 2.5);
      timer=setTimeout(tick, wait*1000);
    }
    if(T){ T.now=function(){ wave(); }; T.line=function(on){ showLine(on); }; }
    if(window.MutationObserver) new MutationObserver(function(){ if(modeKey()!==cur) startView(false); else if(line && !line.hidden) line.textContent=lineText(); }).observe(B,{attributes:true,attributeFilter:['class']});
    if(off){ showLine(false); return; }
    document.addEventListener('click',function(e){ if(e.target.closest && e.target.closest('.card.now, .gw, #sheet')) understood(); },true);
    window.addEventListener('scroll',function(){ lastScroll=Date.now(); },{passive:true});
    document.addEventListener('animationend',function(e){ if(String(e.animationName).indexOf('nudge')===0) e.target.classList.remove('nudge'); });
    startView(true);
  })();
})();
"""


def seo_head(today):
    """Title/description/canonical/social tags + JSON-LD list of theaters. Counts and names come from THEATERS."""
    cities = []
    for k, _ in THEATERS:
        c = THEATER_ADDR[k].rsplit(",", 1)[-1].strip()
        if c not in cities:
            cities.append(c)
    city_txt = ", ".join(cities[:-1]) + " and " + cities[-1] if len(cities) > 1 else cities[0]
    title = "Movie Showtimes Today - Palm Springs & Coachella Valley"
    desc = (f"Today's movie showtimes at {len(THEATERS)} theaters in {city_txt}. "
            "Updated several times a day, with ratings and what's coming soon.")
    a = html.escape
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "name": "In Theaters - Coachella Valley", "url": SITE_URL},
        {"@type": "ItemList", "name": "Movie theaters in the Coachella Valley", "itemListElement": [
            {"@type": "ListItem", "position": i, "item": {
                "@type": "MovieTheater", "name": n, "address": THEATER_ADDR[k] + ", CA", "url": THEATER_URLS[k]}}
            for i, (k, n) in enumerate(THEATERS, 1)]}]}
    ld_json = json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")
    verify = ""
    if GSC_VERIFY:
        verify += f'<meta name="google-site-verification" content="{a(GSC_VERIFY)}">\n'
    if BING_VERIFY:
        verify += f'<meta name="msvalidate.01" content="{a(BING_VERIFY)}">\n'
    return (f'<title>{a(title)}</title>\n'
            f'<meta name="description" content="{a(desc)}">\n'
            f'<link rel="canonical" href="{SITE_URL}">\n'
            f'<meta property="og:type" content="website">\n'
            f'<meta property="og:site_name" content="In Theaters - Coachella Valley">\n'
            f'<meta property="og:title" content="{a(title)}">\n'
            f'<meta property="og:description" content="{a(desc)}">\n'
            f'<meta property="og:url" content="{SITE_URL}">\n'
            f'<meta name="twitter:card" content="summary">\n'
            f'{verify}<script type="application/ld+json">{ld_json}</script>')


def sitemap_xml(today):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f'<url><loc>{SITE_URL}</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq></url>\n'
            + "".join(f'<url><loc>{SITE_URL}{THEATER_SLUG[k]}/</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq></url>\n'
                      for k, _ in THEATERS)
            + '</urlset>\n')


def sitemap_clean_xml():
    """Minimal sitemap for Google: only <loc>, exactly the basic example from Google's docs (no lastmod / changefreq)."""
    urls = [SITE_URL] + [f"{SITE_URL}{THEATER_SLUG[k]}/" for k, _ in THEATERS]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <url>\n    <loc>{u}</loc>\n  </url>\n" for u in urls)
            + '</urlset>\n')


def theater_page(key, movies, today, tomorrow, tmrw_ok):
    """Static, crawlable page for one theater: today's (and tomorrow's) showtimes as plain text, address, links."""
    name, addr = dict(THEATERS)[key], THEATER_ADDR[key]
    city = addr.rsplit(",", 1)[-1].strip()
    a = html.escape
    nice_today = datetime.strptime(today, "%Y-%m-%d").strftime("%A, %b %-d")
    nice_tmrw = datetime.strptime(tomorrow, "%Y-%m-%d").strftime("%A, %b %-d")
    updated = datetime.now(TZ).strftime("%b %-d, %-I:%M %p %Z")

    def block(day):
        out, titles = "", []
        for g in movies:
            shows = g["by_theater"].get(key) or {}
            items = sorted((ts, fmt) for (ts, fmt) in shows if ts.strftime("%Y-%m-%d") == day)
            if not items:
                continue
            titles.append(g["title"])
            chips = ""
            for ts, fmt in items:
                tag = f"<b>{a(fmt)}</b> " if fmt else ""
                cls = {"IMAX": "t imax", "D-BOX": "t fmt"}.get(fmt, "t")
                buy = g["buy"].get((key, ts, fmt))
                href = buy or shows[(ts, fmt)]
                chips += (f'<a class="{cls}" data-ts="{int(ts.timestamp())}" href="{a(href)}" target="_blank" '
                          f'rel="noopener">{tag}{fmt_time(ts)}</a>')
            out += f'<div class="trow"><div class="ttl">{a(g["title"])}</div><div class="times">{chips}</div></div>'
        return out, titles

    today_html, today_titles = block(today)
    tmrw_html, _ = block(tomorrow) if key in tmrw_ok else ("", [])
    if not today_html:
        today_html = ('<p class="empty">No showtimes are listed for today. Please check the theater&rsquo;s own '
                      f'site: <a href="{a(THEATER_URLS[key])}" target="_blank" rel="noopener">{a(name)} &#8599;</a></p>')
    title = f"{name} Showtimes Today - {city}"
    desc = f"Showtimes today at {name} in {city}, CA ({addr})."
    if today_titles:
        desc += " Now showing: " + ", ".join(today_titles[:4]) + (" and more." if len(today_titles) > 4 else ".")
    canon = f"{SITE_URL}{THEATER_SLUG[key]}/"
    lat, lon = THEATER_GEO[key]
    ld = {"@context": "https://schema.org", "@type": "MovieTheater", "name": name, "url": canon,
          "address": addr + ", CA", "sameAs": THEATER_URLS[key],
          "geo": {"@type": "GeoCoordinates", "latitude": lat, "longitude": lon}}
    ld_json = json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")
    gc = (f'<script data-goatcounter="https://{GOATCOUNTER_CODE}.goatcounter.com/count" async '
          f'src="//gc.zgo.at/count.js"></script>\n') if GOATCOUNTER_CODE else ""
    others = "".join(f'<a href="../{THEATER_SLUG[k]}/">{a(n)} &rsaquo;</a>' for k, n in THEATERS if k != key)
    maps = "https://maps.google.com/?q=" + urllib.parse.quote_plus(name + ", " + addr)
    tm = (f'<h2>Tomorrow <span class="sub">&middot; {nice_tmrw}</span></h2><div class="tlist d1">{tmrw_html}</div>'
          if tmrw_html else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="base-uri 'none'; object-src 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{a(title)}</title>
<meta name="description" content="{a(desc)}">
<link rel="canonical" href="{canon}">
<meta property="og:type" content="website"><meta property="og:site_name" content="In Theaters - Coachella Valley">
<meta property="og:title" content="{a(title)}"><meta property="og:description" content="{a(desc)}"><meta property="og:url" content="{canon}">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">{ld_json}</script>
<link rel="icon" type="image/png" sizes="32x32" href="../icon-32.png">
<link rel="apple-touch-icon" href="../apple-touch-icon.png">
<meta name="theme-color" content="#111318">
<style>{CSS}
.tp{{max-width:640px;margin:0 auto}} .tp .addr2{{display:block;text-align:center;color:var(--mut);margin-top:4px}}
.tp .btns{{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin:14px 0 4px}}
.tp .btns a{{padding:8px 13px;border-radius:10px;background:var(--chip);color:var(--fg);text-decoration:none;font-size:.85rem}}
.tp .trow{{padding:12px 0;border-top:1px solid var(--line)}} .tp .ttl{{font-weight:700}}
.tp .tlist .t{{--tc:var(--cline);font-size:.85rem;padding:5px 10px}}
.tp .others a{{display:block;padding:9px 0;color:var(--fg);text-decoration:none;border-top:1px solid var(--line)}}
.tp .note{{color:var(--mut);font-size:.8rem;margin-top:22px}} .tp .empty{{color:var(--mut)}}
</style></head>
<body data-th="{key}">
<header><h1>{a(name)}</h1><div class="sub">Showtimes today, {nice_today} &middot; updated {updated}</div></header>
<main class="tp">
<span class="addr2">{a(addr)}, CA</span>
<div class="btns"><a href="{a(maps)}" target="_blank" rel="noopener">Open in Maps</a><a href="{a(THEATER_URLS[key])}" target="_blank" rel="noopener">Theater website &#8599;</a><a href="../">&larr; All theaters</a></div>
<h2>Today <span class="sub">&middot; {nice_today}</span></h2><div class="tlist">{today_html}</div>
{tm}
<h2>Other theaters</h2><div class="others">{others}</div>
<p class="note">Showtimes are collected automatically from the theater&rsquo;s website and can change or be incomplete. Please confirm on the theater&rsquo;s site before you go. Last updated {updated}.</p>
</main>
{gc}<script>document.querySelectorAll(".t[data-ts]").forEach(function(e){{if(+e.dataset.ts*1000<Date.now())e.classList.add("past")}})</script>
</body></html>"""


def render(movies, soon, today, warnings, tomorrow, tmrw_ok):
    gc_tag = (f'<script data-goatcounter="https://{GOATCOUNTER_CODE}.goatcounter.com/count" async '
              f'src="//gc.zgo.at/count.js"></script>\n') if GOATCOUNTER_CODE else ""
    others = {k: [norm(g["title"]) for g in movies if k in g["by_theater"]] for k, _ in THEATERS}
    now_html = "\n".join(now_card(g, i, today, others) for i, g in enumerate(movies))
    soon_html = "\n".join(soon_card(m, i) for i, m in enumerate(soon))
    nice_today = datetime.strptime(today, "%Y-%m-%d").strftime("%A, %b %-d")
    nice_tmrw = "Today, " + datetime.strptime(tomorrow, "%Y-%m-%d").strftime("%A, %b %-d")
    updated = datetime.now(TZ).strftime("%b %-d, %-I:%M %p %Z")
    warn = "".join(
        f'<a class="fallback" href="{html.escape(THEATER_URLS[k])}" target="_blank" rel="noopener" '
        f'title="{html.escape(r[:300])}"><span><b>{html.escape(THEATER_NAMES[k])}</b>'
        f'<small>Showtimes could not be loaded here right now</small></span>'
        f'<em>Open showtimes &#8599;</em></a>' for k, r in warnings)
    notes = "".join(f" {html.escape(n)}" for n in NOTES)
    theaters = "".join(
        f'<div class="tinfo" data-th="{k}"><b>{html.escape(n)}</b>'
        f'<a class="addr" href="https://maps.google.com/?q={urllib.parse.quote_plus(n + ", " + THEATER_ADDR[k])}" '
        f'target="_blank" rel="noopener">{html.escape(THEATER_ADDR[k])}</a>'
        f'<a href="{html.escape(THEATER_URLS[k])}" target="_blank" rel="noopener">All showtimes &#8599;</a>'
        f'<a class="tpage" href="{THEATER_SLUG[k]}/">Showtimes today &rsaquo;</a></div>'
        for k, n in THEATERS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="base-uri 'none'; object-src 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
{seo_head(today)}
<link rel="icon" type="image/png" sizes="32x32" href="icon-32.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="manifest.webmanifest">
<meta name="apple-mobile-web-app-title" content="Showtimes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#111318">
<link rel="preconnect" href="https://image.tmdb.org">
<style>{CSS}</style></head>
<body class="sig-label" data-geo='{json.dumps(THEATER_GEO)}' data-d0="{today}" data-d1="{tomorrow}" data-d1nice="{nice_tmrw}" data-tmrw-ok="{','.join(tmrw_ok)}" data-nth="{len(THEATERS)}">
<header><h1>In Theaters<span class="reg">Coachella Valley</span></h1><div class="sub"><span id="daylabel">Today, {nice_today}</span> &middot; updated {updated}</div><div class="mode" id="mode" role="group" aria-label="View"><button type="button" data-m="cards" aria-pressed="true" aria-label="Showtimes" title="Showtimes"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></button><button type="button" data-m="posters" aria-pressed="false" aria-label="Posters" title="Posters"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2"/><circle cx="9" cy="9" r="1.5"/><path d="M4 17l5-5 4 4 3-3 4 4"/></svg></button><button type="button" data-m="list" aria-pressed="false" aria-label="List" title="List"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></svg></button><button type="button" class="gpin" id="geo" aria-pressed="false" aria-label="Nearest theaters first" title="Nearest theaters first"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg></button></div><div class="modecap" id="modecap">Showtimes</div></header>
{warn}
<div class="notice" id="stale" hidden>These showtimes may be out of date (last updated {updated}). The page refreshes several times a day.</div>
<main>
<h2>Now playing <span class="sub">&middot; still showing today first, then by rating</span></h2>
<p class="taphint" id="taphint" hidden></p>
<p class="empty" id="none" hidden>Nothing scheduled.</p>
<section class="grid" id="nowgrid">
{now_html}
</section>
<h2>Coming soon <span class="sub">&middot; next {UPCOMING_DAYS // 7} weeks, most anticipated first</span></h2>
<section class="grid">
{soon_html}
</section>
</main>
<footer>
<h2>Theaters</h2>
<div class="theaters">{theaters}</div>
<div class="foot">
<p>* Showtimes are collected automatically from the theaters&rsquo; websites and can change or be incomplete. Please confirm on the theater&rsquo;s site before you go. Last updated {updated}.</p>
<p>Tap a movie for its full schedule. Tap a showtime to open that showing on the theater&rsquo;s site (where the theater allows it), or the colored theater block to open the theater&rsquo;s page.{notes}</p>
<p>This product uses the TMDB API but is not endorsed or certified by TMDB. Ratings are TMDB user scores.</p>
</div>
<div class="credit"><div class="c1">Made in Palm Springs by <b>Sergei</b></div>
<div class="c2">Need a website or tech help? <a href="mailto:genius.gorilla.it@gmail.com">genius.gorilla.it@gmail.com</a></div>
<a class="cbtn" href="mailto:genius.gorilla.it@gmail.com?subject=Website%20%2F%20IT%20help"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>Email me</a></div>
</footer>
<div id="sheet" hidden><div class="sheet-bg"></div><div class="sheet-box" role="dialog" aria-modal="true">
<button class="sheet-x" aria-label="Close"><svg viewBox="0 0 14 14" aria-hidden="true"><path d="M2 2l10 10M12 2L2 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg></button><div class="sheet-body"></div></div></div>
{gc_tag}<script>{JS}</script>
</body></html>"""


# ------------------------------------------------------------------------ icons
# Home-screen / tab icons (PNG, base64) so nothing extra has to be uploaded to the repo.
ICONS = {
    512: "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAYAAAD0eNT6AABVKklEQVR42u3dd7zlVX3v/9da3+93973PmTNnZpihiFgogoQRFARpGhtNjQ2vGtSbmESJ9d78rok3udcbY2IiRqNJTBALlqgxiC2x0WEQUBCkKUUYpp85Zff9/X7X+v3x3WdmgGGYcso+Z97Px+MklsE5a5e13t9VPssc/NRjPSIiIrJfsXoJREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQAREREFABEREREAUBEREQUAEREREQBQERERBasELxeBREREc0AiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIiIAoCIiIgwe6WAVQlYREREMwAiIiKiACAiIiIKACIiIqIAICIiIgtSqD2AIiIimgEQERERBQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBucyIN0GJCIiohkAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREQUAERERUQAQERERBQARERFZJEK8XgQRERHNAIiIiMh+MAOgKQARERHNAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiMybUGWARERENAMgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiIgoAIiIiogAgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiIgoAIiIiogAgIiIieygE3QcoIiKiGQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABARERHmrRCQ6gCJiIhoBkBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBERETYw9sAdR2giIiIZgBERERkP5gB0PO/iIiIZgBEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBE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ACKyMM7vR4EnDDy+v7ZPAXoNw7qtAZfdVObq2wtcf1eR8YallPcU+iXU9dSvALBfBQHns/oBky1LrZRywJKU157cpFZ0vP7UBvkwOxC7bNRpbUBEBn/0D6Bbh7F6SLHouOGXBW5+IM/tD+S45pdFxuqWKIBi3hEFWR+ogV8BYL9eGghtdmIgTg1T7eyCy4NGUyCbFnvDqQ3ykW6+EpFBnvY35PKeG+/Kc91dRSpFx9a6ZbweUCo4CjlPLty+AVAUAOQxYWB67X962t/7LBR4r5uvRWSwpwC8NxRynmLe4Vx/7T/Mrkr3TkV8Bkmol2DwNs5M3x8QBtu/VMuHFJdFZOHMBDgHQejx/WVOUQCQvaiMBbpUSEQW6J4AQZcBiYiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiLCvxwB1TkNEREQzACIiIqIAICIiIizCJQCtAIiIiGgGQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBEREQBQERERPYLoddrICIiohkAERER2Q9mAEBzACIiIpoBEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREfbsNkBdBigiIqIZABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREeavEJAqAYmIiGgGQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERPZMqLsARURENAMgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiIgoAIiIiogAgIiIiCgAiIiIyf0JQKSARERHNAIiIiMh+MAOgCQARERHNAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIgoAIiIiIgCgIiIiCgAiIiIyCIRgterICIiohkAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREQUAERERGQyhqgCIiIhoBkBEREQUAEREREQBQERERBQAREREhAV6GZB2AYqIiGgGQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABERESE2SwFrFrAIiIimgEQERERBQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBERUQAQERERBQARERFRABAREREFABEREVEAEBERkQXj/weQc1hjEBFtYwAAAABJRU5ErkJggg==",
    192: "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",
    180: "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",
    32: "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAERklEQVR42u2XzWucVRTGf+e+X5OZaWrTJmli048EbUlLbC1KoSqIX3+A6EoqVNCFGzeCGzG6cyNuFFwo2bjxDyhFWoorC0qxlNbapsSPNjFtJzGf77xf97i4M5NJUrSFQgR7h3dgZu4959znee5zz8jAnseUDRw+bGh+fN3Y/P8BBNCNpgD+9wisL8CIe+7nsOqeO4iwbSgYo8SpIU4NRlx5/1ZLc44qKNJaR+N7q0IpsJQji7WyKuAqCozAct0wPFDn2LOzdIQWVZC7QEMVPANRoCwnggittfXM8PV3D3H+1w4qkV2FhPQN7FNwE7Nc6Nmc8+0HE/T35ZDdxfZb1UMWw9WpkOGhFIo2eAK4edPjxQ/3cKMWEAba2rffxMqIEieGxwdj+rfmzNYMvnd3uVXB95S5JY/rtYDB3ow0lxZyeQE9WwqeGIoZnwwpBZZCZbUIFcWqEvoWQqcFzzQFJKgajBSNhG6xGMWq49Qz7nNeuGJyuyJkqwIBhIHFquJeazSgFgLP8vstn0vXQvb2p9QzF6EaWRBLlgsK+JFFC0hTQyWwoJAh+Aa6qgV5IfgeFNZRUC1ZLk+ETEz7hJ5FbQM2wKt2bh1tUeVZJmc8vji5hW2dBUf2JmCVUz9v55vzR9jTOcFiKpw8McRMXenvjhm7sJMzk92MdM6Q43FlMmT/YMpUzacjVCoVZez0Zl75+GGmZj2iUNE2YXmVTV2jK2qBwHPn9aeJEsefn+PqZMhHV07xZ997nL1cJ/q+xkvTLzM/0cXns8t8efQtzux4Gi7+xjPbprkdB8wtGI592s9i3TC8I+HNz/qYjw0dYdMLtF27ytoLqXmEtGEeKj7GQK4+WME3gochtwY1BjyhUIMAka+UI8ti7ARULTldrDc2l9erVLeMNi3DGEhzoZ4K779a46n9dboqOd3pCezUad45PMbAgducnp0mPHSB147eoOPCNQ7+cY63H71EHZ/xSZ+RXSmvPzfHyK6ETWVL6MHJc1UHudek321aevqGtGVCqXB4KOGTN25yYGdCmjnRVUI3OStcoRIqYiFLDSXPut8Q/lo0XJ0KefKRmKwQjEBuoRQoF69HvPtVN2d/KVGOtGVGpom1oCQZ7O5JObgvYakuLStdTAwLiU+aC2kOyZIhjg2FKguZYT7zKApH10LsQhbWJRdgsS6M7E0Y3J6SZCBoi+M2H3DvaS6QgrVCIdrGV4FdxZ87ujSCWYHcCnkBhQrWrli4tQJpIzbrfKA5CToCy7nxkOkZj97e4t6sWCDyc4YHhFJZKdl2K1Zqtw0/XgkphYq1K7qXbb27tf0yWkqEQ4Mpx1+YpxzqPbULRpTQp2VgzRNVz4SxU5v4YTyi0sa/K6Bn16oUxsBy4k6CZ+69Y1x7ewpOD6VQnfjsP/UDjcnl0FKJ7m9D4oQp6xi9Y0dUrPem+1XGurAPuuKNR+DBf8ONRuBvC6QgLFTJyGUAAAAASUVORK5CYII=",
}

# Android "maskable" version: same ticket, smaller, so round/squircle masks never cut it.
ICON_MASKABLE = "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"

def write_icons(folder):
    names = {512: "icon-512.png", 192: "icon-192.png", 180: "apple-touch-icon.png", 32: "icon-32.png"}
    for n, fname in names.items():
        with open(os.path.join(folder, fname), "wb") as f:
            f.write(base64.b64decode(ICONS[n]))
    with open(os.path.join(folder, "icon-maskable-512.png"), "wb") as f:
        f.write(base64.b64decode(ICON_MASKABLE))
    manifest = {"id": "./", "name": "In Theaters - Coachella Valley", "short_name": "Showtimes",
                "description": "Today's showtimes at Festival, Cinemark and Regal, plus upcoming releases.",
                "start_url": "./", "scope": "./", "display": "standalone",
                "background_color": "#111318", "theme_color": "#111318",
                "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                          {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                          {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
                           "purpose": "maskable"}]}
    with open(os.path.join(folder, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)


# ------------------------------------------------------------------------ main
def us_theatrical_date(movie_id, lo, hi):
    """US theatrical date of a movie inside [lo, hi] (wide release preferred), or None."""
    try:
        data = tmdb(f"/movie/{movie_id}/release_dates")
    except Exception:  # noqa: BLE001
        return None
    best = None
    for c in data.get("results", []):
        if c.get("iso_3166_1") != "US":
            continue
        for r in c.get("release_dates", []):
            d = (r.get("release_date") or "")[:10]
            if r.get("type") in (2, 3) and lo <= d <= hi:
                rank = (0 if r["type"] == 3 else 1, d)
                if best is None or rank < best:
                    best = rank
    return best[1] if best else None


def get_soon(now_ids, today):
    """Next few weeks of US theatrical releases, most anticipated (TMDB popularity) first."""
    t0 = datetime.strptime(today, "%Y-%m-%d")
    lo = (t0 + timedelta(days=1)).strftime("%Y-%m-%d")
    hi = (t0 + timedelta(days=UPCOMING_DAYS)).strftime("%Y-%m-%d")
    results = []
    try:
        for p in (1, 2):
            d = tmdb("/discover/movie", region="US", with_release_type="3", sort_by="popularity.desc",
                     include_adult="false", page=p, **{"release_date.gte": lo, "release_date.lte": hi})
            results += d.get("results", [])
            if p >= d.get("total_pages", 1):
                break
    except Exception as e:  # noqa: BLE001
        print(f"TMDB discover failed ({e}); using /movie/upcoming")
    if not results:
        for p in (1, 2, 3):
            d = tmdb("/movie/upcoming", region="US", page=p)
            results += [m for m in d.get("results", []) if lo <= (m.get("release_date") or "") <= hi]
            if p >= d.get("total_pages", 1):
                break
    seen, cands = set(), []
    for m in sorted(results, key=lambda m: -m.get("popularity", 0)):
        if m["id"] in seen or m["id"] in now_ids or not m.get("poster_path"):
            continue
        seen.add(m["id"])
        cands.append(m)
    cands = cands[:MAX_UPCOMING * 3]
    dates = pmap(lambda m: us_theatrical_date(m["id"], lo, hi), cands)
    soon = []
    for m, date in zip(cands, dates):
        if date:  # only films with a confirmed US theatrical date in the window
            m["_date"] = date
            soon.append(m)
    return soon[:MAX_UPCOMING]


def mock_showings(now):
    """Sample data placed around the current time so every card state can be seen."""
    t = lambda h: (now + timedelta(hours=h)).replace(second=0, microsecond=0)
    tm = lambda hh, mm=0: (now + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    RT = {"Playing Now And Tomorrow": (118, "Comedy"), "Mixed Theaters": (135, "Sci-Fi"), "Last Day Today": (102, "Horror"),
          "Done Today Back Tomorrow": (96, "Drama"), "Starts Tomorrow": (128, "Action"), "Missed It": (90, "Documentary"),
          "The Metropolitan Opera: Carmen": (215, "Opera")}
    S = lambda th, title, ts, imax=False: dict(theater=th, title=title, ts=ts, fmt="IMAX" if imax else "",
                                              url=THEATER_URLS[th] + "#" + ts.strftime("%Y-%m-%d"), tmdb_id=None, poster=None,
                                              rt=RT[title][0], genre=RT[title][1])
    out = []
    for th in ("festival", "cinemark", "regal"):
        out += [S(th, "Playing Now And Tomorrow", t(h)) for h in (-4, -1.5, 1, 3)]
        out += [S(th, "Playing Now And Tomorrow", tm(h)) for h in (13, 16, 19)]
    out += [S("cinemark", "Mixed Theaters", t(-3)), S("cinemark", "Mixed Theaters", tm(14)), S("cinemark", "Mixed Theaters", tm(18, 30)),
            S("regal", "Mixed Theaters", t(-2)), S("regal", "Mixed Theaters", t(1.5)), S("regal", "Mixed Theaters", t(2.5), True)]
    out += [S("regal", "Last Day Today", t(-2)), S("regal", "Last Day Today", t(2))]
    out += [S("festival", "Done Today Back Tomorrow", t(-5)), S("festival", "Done Today Back Tomorrow", t(-2)),
            S("festival", "Done Today Back Tomorrow", tm(12)), S("festival", "Done Today Back Tomorrow", tm(15, 15)),
            S("festival", "Done Today Back Tomorrow", tm(18, 30))]
    out += [S("cinemark", "Starts Tomorrow", tm(19)), S("regal", "Starts Tomorrow", tm(19, 30), True)]
    out += [S("cinemark", "Missed It", t(-6)), S("cinemark", "Missed It", t(-3)), S("regal", "Missed It", t(-2.5))]
    out += [S("pickford", "Playing Now And Tomorrow", t(h)) for h in (-3, 0.5, 2.5)]
    out += [S("pickford", "Playing Now And Tomorrow", tm(17)), S("pickford", "Mixed Theaters", t(-1)),
            S("pickford", "Mixed Theaters", t(4)), S("pickford", "Starts Tomorrow", tm(20))]
    out += [S("laquinta", "Playing Now And Tomorrow", t(h)) for h in (-2, 2)] + [S("laquinta", "Playing Now And Tomorrow", tm(18))]
    out += [S("laquinta", "Mixed Theaters", t(0.75)), S("laquinta", "Starts Tomorrow", tm(21))]
    for x in out[-11:-9] + out[-7:-6]:
        x["fmt"] = "D-BOX"
    out += [S("pickford", "The Metropolitan Opera: Carmen", t(h)) for h in (-1, 3)]
    for x in out:
        if x["title"] == "Done Today Back Tomorrow":
            x["year"] = "1984"
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return [s for s in out if s["ts"].strftime("%Y-%m-%d") in (today, tomorrow)]


def step_summary(line):
    """A line in the GitHub Actions run summary (information only, does not fail the run)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- {line}\n")


# Outgoing links: only https links to the theaters' own sites may reach the page. A tampered or compromised source
# page must not be able to send visitors to a look-alike ticket page or slip in a javascript: link.
LINK_HOSTS = {urllib.parse.urlsplit(u).hostname
              for u in (CINEMARK_BASE, REGAL_BASE, FESTIVAL_BASE, DPLACE_BASE, *THEATER_URLS.values())}


def link_ok(url):
    """True for a plain https link to one of LINK_HOSTS (no user-info, no odd port, no spaces or backslashes)."""
    try:
        if not isinstance(url, str) or re.search(r"[\s\\\x00-\x1f\x7f]", url):
            return False
        p = urllib.parse.urlsplit(url)
        return p.scheme == "https" and p.hostname in LINK_HOSTS and "@" not in p.netloc and p.port in (None, 443)
    except ValueError:
        return False


def image_ok(url):
    """Posters only need to be plain https images (they come from TMDB or the theater's own artwork)."""
    try:
        return (isinstance(url, str) and not re.search(r"[\s\\\x00-\x1f\x7f]", url)
                and urllib.parse.urlsplit(url).scheme == "https" and bool(urllib.parse.urlsplit(url).hostname))
    except ValueError:
        return False


def _safe_host(url):
    """A short, log-safe description of where a rejected link pointed."""
    try:
        h = urllib.parse.urlsplit(str(url)).hostname or str(url)[:30]
    except ValueError:
        h = str(url)[:30]
    return re.sub(r"[^A-Za-z0-9.:-]", "?", h)[:60]


def sanitize_showings(showings, problems):
    """Drop any ticket link or poster that fails the checks above. A dropped ticket link falls back to the theater's own
    page; every drop is reported in problems.txt, which turns the run red (the owner gets GitHub's failure e-mail)."""
    bad = Counter()
    for s in showings:
        th = s.get("theater")
        if s.get("url") and not link_ok(s["url"]):
            bad[(th, "link", _safe_host(s["url"]))] += 1
            s["url"] = THEATER_URLS.get(th, SITE_URL)
        if s.get("buy") and not link_ok(s["buy"]):
            bad[(th, "ticket link", _safe_host(s["buy"]))] += 1
            s["buy"] = None
        if s.get("poster") and not image_ok(s["poster"]):
            bad[(th, "poster", _safe_host(s["poster"]))] += 1
            s["poster"] = None
    for (th, kind, host), n in sorted(bad.items(), key=str):
        problems.append(f"SECURITY: {n} {kind}(s) at {THEATER_NAMES.get(th, th)} pointed to an unexpected address "
                        f"({host}) and were dropped - check that theater's page for tampering")
    return showings


def write_problems(problems):
    """problems.txt is empty when everything is fine; the workflow fails the run otherwise."""
    with open(PROBLEMS_FILE, "w", encoding="utf-8") as f:
        f.write("".join(re.sub(r"[\x00-\x1f\x7f]+", " ", p) + "\n" for p in problems))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and problems:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Problems\n" + "".join(f"- {p}\n" for p in problems))
    for p in problems:
        print("PROBLEM:", p)


ANOMALY_TAIL = re.compile(r"(\b\d+(st|nd|rd|th)\s*$)|\b(anniversary|reissue|re-issue|early access|remastered|restored|encore|4k|imax|dolby)\b", re.I)
ANOMALY_EDITION = re.compile(r"^\s*(\d+(st|nd|rd|th)|anniversary|reissue|re-issue|remastered|restored|4k|imax|dolby|early access|encore)\b", re.I)


def find_anomalies(movies, use_tmdb):
    """Things that look wrong in the finished card list (never fails the run; printed as 'ANOMALY:' lines, the weekly report reads them).
    Heuristics only: a duplicate card, a leftover edition tag in a title, a title TMDB could not match."""
    out = []
    for g in movies:
        t = g["title"]
        if ANOMALY_TAIL.search(t):
            out.append(f'"{t}": the title still has an edition tag (25th / Anniversary / 4K ...) - clean_title may need a new rule')
    norms = {}
    for g in movies:
        norms.setdefault(norm(g["title"]), []).append(g)
    keys = sorted(norms, key=len)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if len(a) >= 6 and b.startswith(a) and ANOMALY_EDITION.match(re.sub(r"^[^0-9a-z]+", "", b[len(a):])):
                out.append(f'possible duplicate: "{norms[a][0]["title"]}" and "{norms[b][0]["title"]}" (the same film with an edition tag?)')
    seen = {}
    for g in movies:
        if g.get("poster"):
            seen.setdefault(g["poster"], []).append(g["title"])
    for url, names in seen.items():
        if len(names) > 1:
            out.append(f'possible duplicate: {len(names)} cards share one poster ({" / ".join(names[:3])})')
    if use_tmdb and movies:
        unm = [g["title"] for g in movies if not g["tmdb_id"] and not g.get("label")]
        if unm:
            out.append(f'{len(unm)} card(s) without a TMDB match and without an Event/Re-release label (no rating): ' + "; ".join(unm[:6]))
        if len([g for g in movies if not g["tmdb_id"]]) > 0.35 * len(movies):
            out.append("TMDB matching looks degraded: more than 35% of cards have no TMDB match")
    return out


def write_anomalies(anomalies):
    for a in anomalies:
        print("ANOMALY:", a)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and anomalies:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Anomalies (heuristics, the run stays green)\n" + "".join(f"- {a}\n" for a in anomalies))


def main():
    started = time.time()
    problems = []
    mock = "--mock" in sys.argv
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    showings, warnings, tmrw_ok = [], [], []

    if mock:
        showings = mock_showings(now)
        tmrw_ok = [k for k, _ in THEATERS]
        soon = [{"id": 100 + i, "title": f"Upcoming Sample {c}", "release_date": (now + timedelta(days=d)).strftime("%Y-%m-%d"),
                 "poster_path": None} for i, (c, d) in enumerate([("A", 6), ("B", 11), ("C", 13), ("D", 20)])]
        use_tmdb = False
    else:
        if not os.environ.get("TMDB_API_KEY"):
            sys.exit("TMDB_API_KEY is not set")
        use_tmdb = True
        fetchers = {"festival": fetch_festival, "cinemark": fetch_cinemark,
                    "regal": lambda d: fetch_regal(d, official=(d == today)),
                    "pickford": fetch_pickford, "laquinta": fetch_laquinta, "dplace": fetch_dplace}

        def job(kd):
            key, day = kd
            try:
                return key, day, fetchers[key](day), None
            except Exception as e:  # noqa: BLE001
                return key, day, None, e

        jobs = [(k, d) for k, _ in THEATERS for d in (today, tomorrow)] + [("dplace", None)]
        results = {(k, d): (got, err) for k, d, got, err in pmap(job, jobs, workers=9)}
        dp, dp_err = results.pop(("dplace", None))
        apply_dplace(results, dp, dp_err, (today, tomorrow), problems)
        for key, _ in THEATERS:
            name = THEATER_NAMES[key]
            got, err = results[(key, today)]
            if err:
                print(f"{name}: FAILED - {err}")
                warnings.append((key, str(err)))
                problems.append(f"{name}: today's showtimes could not be loaded ({str(err)[:300]})")
                continue
            print(f"{name}: {len(got)} showtimes today")
            if len(got) < MIN_SHOWTIMES:
                problems.append(f"{name}: only {len(got)} showtimes found for today - the site may have changed")
            showings += got
            got, err = results[(key, tomorrow)]  # tomorrow is a bonus: no data -> no claims about tomorrow
            if err:
                print(f"{name}: tomorrow not available - {err}")
                problems.append(f"{name}: tomorrow's showtimes could not be loaded ({str(err)[:300]})")
            else:
                print(f"{name}: {len(got)} showtimes tomorrow")
                if len(got) < MIN_SHOWTIMES:
                    problems.append(f"{name}: only {len(got)} showtimes found for tomorrow")
                showings += got
                if got:
                    tmrw_ok.append(key)
        if len(warnings) == len(THEATERS):
            problems.append("All theaters failed - the page was NOT updated (the previous version stays online)")
            write_problems(problems)
            sys.exit("All theaters failed; keeping the previously published page.")

    showings = sanitize_showings(showings, problems)
    movies = build_movies(showings, use_tmdb)
    if use_tmdb:
        try:
            soon = get_soon({g["tmdb_id"] for g in movies if g["tmdb_id"]}, today)
        except Exception as e:  # noqa: BLE001
            print(f"TMDB upcoming failed: {e}")
            soon = []
        if TMDB_STATS["fail"] and TMDB_STATS["fail"] >= TMDB_STATS["ok"]:
            problems.append(f"TMDB: {TMDB_STATS['fail']} of {TMDB_STATS['fail'] + TMDB_STATS['ok']} requests failed "
                            "(posters/ratings missing) - check the TMDB_API_KEY secret")
        if not soon:
            problems.append("TMDB: the 'Coming soon' list is empty")

    write_anomalies(find_anomalies(movies, use_tmdb))
    write_problems(problems)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    write_icons(os.path.dirname(OUT))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(render(movies, soon, today, warnings, tomorrow, tmrw_ok))
    with open(os.path.join(os.path.dirname(OUT), "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap_xml(today))
    with open(os.path.join(os.path.dirname(OUT), "sitemap-google.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap_clean_xml())
    for k, _ in THEATERS:
        d = os.path.join(os.path.dirname(OUT), THEATER_SLUG[k])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(theater_page(k, movies, today, tomorrow, tmrw_ok))
    if GSC_VERIFY_FILE:
        with open(os.path.join(os.path.dirname(OUT), GSC_VERIFY_FILE), "w", encoding="utf-8") as f:
            f.write(f"google-site-verification: {GSC_VERIFY_FILE}")
    print(f"OK: {len(movies)} movies, {len(soon)} upcoming -> {OUT} ({time.time() - started:.1f}s)")


if __name__ == "__main__":
    main()
