"""Merge titles-today.json (written by build.py) into titles.json (the running history on the 'data' branch).
Usage: python3 merge_titles.py titles-today.json titles.json
One entry per card title: first/last day seen, number of days seen, raw names used by theaters, theaters, TMDB match,
label, anomalies. Safe to run twice on the same day (days are counted once per date)."""
import json
import os
import sys


def main(today_path, hist_path):
    with open(today_path, encoding="utf-8") as f:
        today = json.load(f)
    day = today["date"]
    hist = {"titles": {}}
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as f:
            hist = json.load(f)
    db = hist.setdefault("titles", {})
    for r in today["titles"]:
        e = db.setdefault(r["title"], {"first": day, "last": day, "days": 0, "raws": [], "theaters": [],
                                       "tmdb": r["tmdb"], "label": r["label"], "anomalies": []})
        if e["last"] != day or e["days"] == 0:
            e["days"] += 1
        e["last"] = max(e["last"], day)
        for key in ("raws", "theaters", "anomalies"):
            e[key] = sorted(set(e[key]) | set(r[key]))
        e["tmdb"] = e["tmdb"] or r["tmdb"]
        e["label"] = r["label"] or e["label"]
    hist["updated"] = day
    hist["note"] = "Built by merge_titles.py from the daily build; each key is a card title."
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"{len(today['titles'])} titles today, {len(db)} in history")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
