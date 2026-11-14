#!/usr/bin/env python3
"""Personal aggregator: what is playing TODAY in three Coachella Valley theaters
(+ upcoming US releases). Data: the theaters' own websites, posters/ratings: TMDB.

  TMDB_API_KEY=... python build.py     -> site/index.html
  python build.py --mock               -> same, from built-in sample data (no network)

Standard library only. Each theater is fetched independently: if one fails, the
page is still built and shows a notice. If all three fail, the script exits with
an error so the previously published page stays online.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
UA = "Mozilla/5.0 (compatible; PersonalShowtimesPage/1.0)"
TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w342"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "index.html")

HIDE_NON_IMAX_3D = True   # you only watch 3D in IMAX, so other 3D showtimes are dropped
MAX_UPCOMING = 12        # how many "coming soon" cards
UPCOMING_DAYS = 28       # only releases within this many days from today

# Order = display order on the cards.
THEATERS = [
    ("festival", "Festival Theaters"),
    ("cinemark", "Cinemark Century at the River + XD"),
    ("regal", "Regal Rancho Mirage"),
]
THEATER_NAMES = dict(THEATERS)
# Where the "open showtimes" link goes when a theater could not be read.
THEATER_URLS = {
    "festival": "https://festivaltheaters.com/buy-tickets/",
    "cinemark": "https://www.cinemark.com/theatres/ca-rancho-mirage/cinemark-century-at-the-river-and-xd",
    "regal": "https://www.regmovies.com/theatres/regal-rancho-mirage-1801",
}
NOTES = []  # extra footer lines (e.g. which backup source was used)

CINEMARK_ID = 479
CINEMARK_BASE = "https://www.cinemark.com"
REGAL_BASE = "https://www.regmovies.com"
REGAL_THEATRE = f"{REGAL_BASE}/theatres/regal-rancho-mirage-1801"
BIGSCREEN_REGAL = "https://www.bigscreen.com/Marquee.php?theater=6355&view=sched&showdate={date}"
FESTIVAL_BASE = "https://festivaltheaters.com"
FESTIVAL_SITE_ID = "177"
FESTIVAL_CIRCUIT_ID = "106"


# ---------------------------------------------------------------- http helpers
def http(url, data=None, headers=None, tries=3):
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def la(dt_utc):
    return dt_utc.astimezone(TZ)


def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# ------------------------------------------------------------ theater parsers
# Each parser returns a list of dicts:
#   {theater, title, ts (aware datetime), imax (bool), url, tmdb_id (int|None)}

def parse_cinemark(data, today):
    out = []
    for m in data:
        slug = m.get("movieSlug") or ""
        url = f"{CINEMARK_BASE}/movies/{slug}?showDate={today}"
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
                    out.append(dict(theater="cinemark", title=m.get("movieTitle", ""), ts=ts,
                                    imax=imax, url=url, tmdb_id=None, poster=poster))
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
                out.append(dict(theater="regal", title=title, ts=ts, imax=imax,
                                url=REGAL_BASE + path, tmdb_id=None))
    return out


def parse_bigscreen(page_html, today, theater="regal", url=None):
    """BigScreen Cinema Guide theater schedule (plain HTML). Used as a backup for Regal."""
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
                out.append(dict(theater=theater, title=title, ts=ts, imax=imax, url=url, tmdb_id=None, poster=poster))
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
        out.append(dict(theater="festival", title=mv.get("name", ""), ts=ts, imax=False,
                        url=f"{FESTIVAL_BASE}/movie/{mv.get('urlSlug', '')}/",
                        tmdb_id=int(tid) if tid and str(tid).isdigit() else None, poster=poster))
    return out


# --------------------------------------------------------------- live fetchers
def fetch_cinemark(today):
    q = urllib.parse.urlencode({"date": today, "timeZone": "America/Los_Angeles"})
    return parse_cinemark(json.loads(http(f"{CINEMARK_BASE}/papi/theaters/{CINEMARK_ID}/showtimes?{q}")), today)


def fetch_regal(today):
    """Regal's own site first; if it refuses this server, use BigScreen's listing for the same theater."""
    try:
        return parse_regal(http(REGAL_THEATRE, tries=2), today)
    except Exception as e1:  # noqa: BLE001
        print(f"  Regal official site failed ({e1}); trying BigScreen")
        try:
            got = parse_bigscreen(http(BIGSCREEN_REGAL.format(date=today), tries=2), today)
        except Exception as e2:  # noqa: BLE001
            raise RuntimeError(f"regmovies.com: {e1} | bigscreen.com: {e2}") from None
        if not got:
            raise RuntimeError(f"regmovies.com: {e1} | bigscreen.com: page loaded but no showtimes for {today}")
        NOTES.append("Regal showtimes via BigScreen Cinema Guide (bigscreen.com); times link to Regal's theater page.")
        return got


def fetch_festival(today):
    query = ("query($date:String,$siteIds:[ID]){ showingsForDate(date:$date, siteIds:$siteIds)"
             "{ data { time published private movie { name urlSlug tmdbId posterImage } } } }")
    raw = http(f"{FESTIVAL_BASE}/graphql",
               data={"query": query, "variables": {"date": today, "siteIds": [FESTIVAL_SITE_ID]}},
               headers={"Content-Type": "application/json", "site-id": FESTIVAL_SITE_ID,
                        "circuit-id": FESTIVAL_CIRCUIT_ID, "client-type": "consumer"})
    data = json.loads(raw)
    if data.get("errors"):
        raise RuntimeError(f"Festival GraphQL: {data['errors'][0].get('message')}")
    return parse_festival(data, today)


# ------------------------------------------------------------------------ TMDB
def tmdb(path, **params):
    key = os.environ["TMDB_API_KEY"].strip()
    params.setdefault("language", "en-US")
    headers = {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    return json.loads(http(f"{TMDB}{path}?{urllib.parse.urlencode(params)}", headers=headers))


def clean_title(t):
    t = re.sub(r"^(IMAX|CUT\d+|DOLBY CINEMA|FATHOM|TCM)\s*:\s*", "", t.strip(), flags=re.I)
    t = re.sub(r"\([^)]*\)", " ", t)
    suffix = (r"\s+-\s+(the imax experience|sensory friendly|open caption\w*|(\d{4}\s+)?re-?issue(\s+\d{4})?"
              r"|\d+(st|nd|rd|th)\s+anniversary.*|early access.*|fan event.*)\s*$")
    for _ in range(3):
        t = re.sub(suffix, "", t, flags=re.I)
    t = re.sub(r"\s+in 3d\s*$", "", t, flags=re.I)
    t = re.sub(r"\b(early access|reissue|encore|anniversary.*)$", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def norm(t):
    return re.sub(r"[^a-z0-9]+", "", clean_title(t).lower())


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


def tmdb_by_title(title):
    k = ("t", norm(title))
    if k not in _cache:
        found = None
        try:
            queries = [clean_title(title)]
            alt = re.sub(r":?\s+part (one|1)$", "", queries[0], flags=re.I)
            if alt != queries[0]:
                queries.append(alt)
            for q in queries:
                res = tmdb("/search/movie", query=q, include_adult="false")["results"]
                exact = [r for r in res if norm(q) in (norm(r.get("title", "")), norm(r.get("original_title", "")))]
                loose = [r for r in res if titles_match(q, r.get("title", "")) or
                         titles_match(q, r.get("original_title", ""))]
                pick = exact or loose
                if pick:
                    found = max(pick, key=lambda r: r.get("popularity", 0))
                    break
        except Exception as e:  # noqa: BLE001
            print(f"  TMDB search '{title}': {e}")
        _cache[k] = found
    return _cache[k]


# ----------------------------------------------------------------- aggregation
def sort_key(g):
    """Best rated first. Ratings with few votes are pulled toward an average so that
    '9.5 from 12 votes' does not outrank '8.2 from 3000 votes'. Unrated titles go last,
    most popular first."""
    v, r = g["votes"], g["rating"]
    if v >= 10 and r > 0:
        return (0, -((v * r + 50 * 6.5) / (v + 50)), g["title"])
    return (1 if g["tmdb_id"] else 2, -g["pop"], g["title"])



def build_movies(showings, use_tmdb):
    groups = {}
    for s in showings:
        info = None
        if use_tmdb:
            info = tmdb_by_id(s["tmdb_id"]) if s["tmdb_id"] else tmdb_by_title(s["title"])
            if info is None and s["tmdb_id"]:
                info = tmdb_by_title(s["title"])
        key = ("tmdb", info["id"]) if info else ("t", norm(s["title"]))
        g = groups.setdefault(key, dict(title=(info or {}).get("title") or clean_title(s["title"]) or s["title"],
                                        poster=(IMG + info["poster_path"]) if (info or {}).get("poster_path") else None,
                                        alt_posters={},
                                        votes=(info or {}).get("vote_count", 0),
                                        rating=(info or {}).get("vote_average", 0),
                                        pop=(info or {}).get("popularity", 0),
                                        tmdb_id=(info or {}).get("id"),
                                        by_theater={}))
        g["by_theater"].setdefault(s["theater"], {})[(s["ts"], s["imax"])] = s["url"]
        if s.get("poster"):
            g["alt_posters"].setdefault(s["theater"], s["poster"])
    movies = list(groups.values())
    for g in movies:  # no TMDB poster -> use the theater's own artwork
        if not g["poster"]:
            g["poster"] = next((g["alt_posters"][k] for k in ("festival", "cinemark", "regal")
                                if k in g["alt_posters"]), None)
    movies.sort(key=sort_key)
    return movies


# ------------------------------------------------------------------- rendering
def fmt_time(ts):
    return ts.strftime("%-I:%M %p")


def poster_html(g, i):
    if g["poster"]:
        return (f'<img src="{html.escape(g["poster"])}" alt="{html.escape(g["title"])}" loading="lazy" '
                f'onerror="var d=document.createElement(\'div\');d.className=\'poster\';'
                f'd.textContent=this.alt;this.replaceWith(d)">')
    h = (i * 47) % 360
    return (f'<div class="poster" style="background:linear-gradient(160deg,hsl({h} 45% 30%),'
            f'hsl({h + 60} 45% 16%))"><span>{html.escape(g["title"])}</span></div>')


def now_card(g, i):
    if g["votes"] >= 10 and g["rating"] > 0:
        rating = f'<span class="rating">&#9733; {g["rating"]:.1f}</span>'
    else:
        rating = '<span class="rating none">No ratings yet</span>'
    rows = ""
    for key, name in THEATERS:
        shows = g["by_theater"].get(key)
        if not shows:
            continue
        items = sorted(shows.items())
        first_url = items[0][1]
        chips = ""
        for (ts, imax), url in items:
            tag = "<b>IMAX</b> " if imax else ""
            cls = "t imax" if imax else "t"
            chips += (f'<a class="{cls}" data-ts="{int(ts.timestamp())}" href="{html.escape(url)}" '
                      f'target="_blank" rel="noopener">{tag}{fmt_time(ts)}</a>')
        rows += (f'<div class="th"><a class="thn" href="{html.escape(first_url)}" target="_blank" rel="noopener">'
                 f'{html.escape(name)}<i>&#8599;</i></a><div class="times">{chips}</div></div>')
    return (f'<article class="card now">{poster_html(g, i)}<div class="body"><h3>{html.escape(g["title"])}</h3>'
            f'<div class="meta">{rating}</div>{rows}</div></article>')


def soon_card(m, i):
    title = m.get("title") or "Untitled"
    try:
        d = datetime.strptime(m.get("_date") or m["release_date"], "%Y-%m-%d").strftime("%b %-d")
    except (KeyError, ValueError):
        d = "TBA"
    g = dict(title=title, poster=(IMG + m["poster_path"]) if m.get("poster_path") else None)
    return (f'<article class="card soon">{poster_html(g, i + 30)}<span class="badge">{d}</span>'
            f'<div class="body"><h3>{html.escape(title)}</h3></div></article>')


CSS = """
:root{--bg:#111318;--fg:#eceef2;--mut:#9aa1ad;--card:#1b1e26;--line:#2b303b;--acc:#f5b301;--chip:#262b36}
@media (prefers-color-scheme:light){:root{--bg:#f5f6f8;--fg:#15171c;--mut:#616977;--card:#fff;--line:#e3e6eb;--acc:#b97d00;--chip:#eef0f4}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header,main,footer{max-width:1100px;margin:0 auto;padding:16px}
h1{margin:8px 0 0;font-size:1.6rem} h2{margin:26px 0 12px;font-size:1.2rem} h2 .sub{font-weight:400;font-size:.8rem}
.sub{color:var(--mut);font-size:.9rem}
.notice{max-width:1100px;margin:8px auto;padding:8px 14px;border-radius:8px;background:var(--chip);color:var(--fg);font-size:.85rem;border-left:4px solid var(--acc)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;align-items:start}
@media (max-width:700px){.grid{grid-template-columns:repeat(2,1fr);gap:10px}}
.card{background:var(--card);border-radius:12px;overflow:hidden;position:relative;border:1px solid var(--line)}
.card img,.poster{width:100%;aspect-ratio:2/3;object-fit:cover;display:block}
.poster{background:linear-gradient(160deg,#3a4a66,#1c2233);display:flex;align-items:center;justify-content:center;text-align:center;padding:14px;color:#fff;font-weight:600}
.body{padding:10px}
h3{margin:0;font-size:.95rem;line-height:1.25}
.meta{margin:4px 0 8px;font-size:.85rem}
.rating{color:var(--acc);font-weight:600} .rating.none{color:var(--mut);font-weight:400}
.th{padding:8px 0;border-top:1px solid var(--line)}
.thn{display:flex;justify-content:space-between;gap:6px;font-size:.78rem;font-weight:600;color:var(--fg);text-decoration:none;line-height:1.2}
.thn i{font-style:normal;color:var(--mut)} .thn:hover{color:var(--acc)}
.times{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.t{background:var(--chip);color:var(--fg);text-decoration:none;font-size:.78rem;padding:3px 8px;border-radius:999px;white-space:nowrap}
.t:hover{background:var(--acc);color:#111}
.t.imax{border:1px solid var(--acc)} .t.imax b{color:var(--acc);font-size:.68rem;letter-spacing:.03em}
.t.imax:hover b{color:#111}
.soon .body{padding:8px 10px 10px}
.badge{position:absolute;top:8px;left:8px;background:var(--acc);color:#111;font-weight:700;font-size:.78rem;padding:3px 9px;border-radius:999px}
.fallback{max-width:1068px;margin:8px auto;padding:10px 14px;border-radius:10px;background:var(--card);border:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;text-decoration:none;color:var(--fg)}
@media (max-width:1100px){.fallback{margin:8px 16px}}
.fallback small{display:block;color:var(--mut);font-size:.78rem}
.fallback em{font-style:normal;white-space:nowrap;background:var(--acc);color:#111;font-weight:600;font-size:.85rem;padding:6px 12px;border-radius:999px}
.empty{color:var(--mut)}
footer{color:var(--mut);font-size:.8rem;padding-bottom:32px}
[hidden]{display:none!important}
"""

JS = """
(function(){
  if(document.body.dataset.preview) return;
  var fmt=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Los_Angeles'});
  var stale=fmt.format(new Date())!==document.body.dataset.date;
  if(stale){document.getElementById('stale').hidden=false;}
  else{
    var now=Date.now()/1000;
    document.querySelectorAll('.t').forEach(function(a){ if(+a.dataset.ts<now) a.hidden=true; });
    document.querySelectorAll('.th').forEach(function(r){ if(!r.querySelector('.t:not([hidden])')) r.hidden=true; });
    document.querySelectorAll('.card.now').forEach(function(c){ if(!c.querySelector('.th:not([hidden])')) c.hidden=true; });
  }
  if(!document.querySelector('.card.now:not([hidden])')) document.getElementById('none').hidden=false;
})();
"""


def render(movies, soon, today, warnings, preview=False):
    now_html = "\n".join(now_card(g, i) for i, g in enumerate(movies))
    soon_html = "\n".join(soon_card(m, i) for i, m in enumerate(soon))
    nice_today = datetime.strptime(today, "%Y-%m-%d").strftime("%A, %b %-d")
    updated = datetime.now(TZ).strftime("%b %-d, %-I:%M %p %Z")
    warn = "".join(
        f'<a class="fallback" href="{html.escape(THEATER_URLS[k])}" target="_blank" rel="noopener" '
        f'title="{html.escape(r[:300])}"><span><b>{html.escape(THEATER_NAMES[k])}</b>'
        f'<small>Showtimes could not be loaded here right now</small></span>'
        f'<em>Open showtimes &#8599;</em></a>' for k, r in warnings)
    notes = "".join(f" {html.escape(n)}" for n in NOTES)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>In Theaters - Coachella Valley</title>
<style>{CSS}</style></head>
<body data-date="{today}"{' data-preview="1"' if preview else ''}>
<header><h1>In Theaters</h1><div class="sub">Today, {nice_today} &middot; updated {updated}</div></header>
{warn}
<div class="notice" id="stale" hidden>These showtimes may be out of date (last updated {updated}). The page refreshes several times a day.</div>
<main>
<h2>Now playing <span class="sub">&middot; best rated first</span></h2>
<p class="empty" id="none" hidden>No more showings today.</p>
<section class="grid">
{now_html}
</section>
<h2>Coming soon <span class="sub">&middot; next {UPCOMING_DAYS // 7} weeks, most anticipated first</span></h2>
<section class="grid">
{soon_html}
</section>
</main>
<footer>Showtimes come from the theaters' own websites and can change; tap a time to open that movie on the theater's site.{notes} This product uses the TMDB API but is not endorsed or certified by TMDB. Ratings are TMDB user scores.</footer>
<script>{JS}</script>
</body></html>"""


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
    seen, soon = set(), []
    for m in sorted(results, key=lambda m: -m.get("popularity", 0)):
        if m["id"] in seen or m["id"] in now_ids or not m.get("poster_path"):
            continue
        seen.add(m["id"])
        date = us_theatrical_date(m["id"], lo, hi)
        if not date:
            continue  # no confirmed US theatrical date in the window
        m["_date"] = date
        soon.append(m)
        if len(soon) >= MAX_UPCOMING:
            break
    return soon


def main():
    mock = "--mock" in sys.argv
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    showings, warnings = [], []

    if mock:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import mock_data
        today = mock_data.TODAY
        showings = (parse_festival(mock_data.FESTIVAL, today) + parse_cinemark(mock_data.CINEMARK, today)
                    + parse_regal(mock_data.REGAL_HTML, today))
        soon = mock_data.SOON
        use_tmdb = False
    else:
        if not os.environ.get("TMDB_API_KEY"):
            sys.exit("TMDB_API_KEY is not set")
        use_tmdb = True
        for key, fn in (("festival", fetch_festival), ("cinemark", fetch_cinemark), ("regal", fetch_regal)):
            try:
                got = fn(today)
                print(f"{THEATER_NAMES[key]}: {len(got)} showtimes today")
                showings += got
            except Exception as e:  # noqa: BLE001
                print(f"{THEATER_NAMES[key]}: FAILED - {e}")
                warnings.append((key, str(e)))
        if len(warnings) == len(THEATERS):
            sys.exit("All theaters failed; keeping the previously published page.")

    movies = build_movies(showings, use_tmdb)
    if use_tmdb:
        try:
            soon = get_soon({g["tmdb_id"] for g in movies if g["tmdb_id"]}, today)
        except Exception as e:  # noqa: BLE001
            print(f"TMDB upcoming failed: {e}")
            soon = []

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and warnings:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Theaters that failed\n" + "".join(f"- **{THEATER_NAMES[k]}**: `{r[:400]}`\n" for k, r in warnings))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(render(movies, soon, today, warnings, preview=mock))
    print(f"OK: {len(movies)} movies today, {len(soon)} upcoming -> {OUT}")


if __name__ == "__main__":
    main()
