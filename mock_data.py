"""Sample data shaped like the real responses (used only by `python build.py --mock`)."""
import json

TODAY = "2026-11-14"

def z(h, m):  # PST (UTC-8) local time -> UTC ISO string for 2026-11-14
    hh = h + 8
    day = 14
    if hh >= 24:
        hh -= 24
        day = 15
    return f"2026-11-{day:02d}T{hh:02d}:{m:02d}:00.000Z"

def fs(movie, slug, tid, times):
    return [{"time": z(h, m), "published": True, "private": False,
             "movie": {"name": movie, "urlSlug": slug, "tmdbId": str(tid)}} for h, m in times]

FESTIVAL = {"data": {"showingsForDate": {"data":
    fs("Ebenezer", "ebenezer", 1567605, [(16, 0), (19, 15)])
    + fs("Wild Horse Nine", "wild-horse-nine", 891621, [(16, 45), (19, 30)])
    + fs("You Can See Everything", "you-can-see-everything", 1, [(20, 0)])
    + fs("Being Heumann", "being-heuman", 2, [(15, 45), (18, 45)])
}}}

def cm(title, slug, groups):
    return {"movieTitle": title, "movieSlug": slug,
            "showtimesByPrintType": [{"printTypeName": n, "showtimes": [{"startTime": z(h, m)} for h, m in ts]}
                                     for n, ts in groups]}

CINEMARK = [
    cm("Ebenezer", "ebenezer", [("Luxury Lounger XD", [(16, 0), (19, 0), (22, 0)]), ("Standard", [(18, 30), (21, 30)])]),
    cm("The Cat in the Hat", "the-cat-in-the-hat", [("Standard", [(15, 15), (19, 15)])]),
    cm("Godzilla Minus Zero (Japanese with English Subtitles)", "godzilla-minus-zero", [("Luxury Lounger", [(15, 55), (19, 5)])]),
    cm("Wild Horse Nine", "wild-horse-nine", [("Standard", [(16, 40), (19, 35)])]),
    cm("Clayface", "clayface", [("Standard", [(17, 25), (20, 10)]), ("Standard 3D", [(16, 5)])]),
    cm("Festive Fun with Peppa Cinema Experience", "festive-fun-with-peppa-cinema-experience", [("Standard", [(10, 0)])]),
]

def perf(h, m, attrs):
    return {"PerformanceAttributes": attrs, "CalendarShowTime": f"2026-11-14T{h:02d}:{m:02d}:00", "StopSales": False}

REGAL_DATA = {"props": {"pageProps": {"showtimes": [{
    "TheatreCode": "1801", "AdvertiseShowDate": "2026-11-14T00:00:00",
    "Film": [
        {"Title": "Ebenezer", "MasterMovieCode": "HO00022936",
         "Performances": [perf(15, 30, ["2D", "Laser"]), perf(19, 15, ["2D", "Laser"]), perf(22, 10, ["2D"])]},
        {"Title": "The Cat in the Hat", "MasterMovieCode": "HO00021861",
         "Performances": [perf(15, 5, ["2D"]), perf(18, 10, ["2D"]), perf(20, 0, ["3D"])]},
        {"Title": "IMAX: Dune: Part Two (Reissue 2026)", "MasterMovieCode": "HO00099002",
         "Performances": [perf(18, 5, ["IMAX", "2D"])]},
        {"Title": "Wildwood", "MasterMovieCode": "HO00021657",
         "Performances": [perf(14, 50, ["2D"]), perf(17, 55, ["2D"]), perf(21, 15, ["2D"])]},
        {"Title": "UFC 334: Gane vs Hokit", "MasterMovieCode": "HO00099003",
         "Performances": [perf(18, 0, ["2D"])]},
    ]}]}}}

REGAL_HTML = ('<html><body><a href="/movies/ebenezer-ho00022936">x</a><a href="/movies/the-cat-in-the-hat-ho00021861">x</a>'
              '<a href="/movies/imax-dune-part-two-reissue-2026-ho00099002">x</a>'
              '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(REGAL_DATA) + '</script></body></html>')

SOON = [
    {"id": 100 + i, "title": t, "release_date": d, "poster_path": None}
    for i, (t, d) in enumerate([("Upcoming Sample A", "2026-11-21"), ("Upcoming Sample B", "2026-11-26"),
                                ("Upcoming Sample C", "2026-12-05"), ("Upcoming Sample D", "2026-12-19")])
]
