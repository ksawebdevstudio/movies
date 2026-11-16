# In Theaters - Coachella Valley

Personal page: what is playing TODAY at Festival Theaters (Palm Springs), Cinemark Century at the
River + XD and Regal Rancho Mirage (showtimes taken from the theaters' own sites; tap a time to open
that movie there), plus upcoming US releases (TMDB). IMAX showtimes are tagged; other 3D showtimes
are hidden (`HIDE_NON_IMAX_3D` in build.py).

## Setup (one time)
1. Create a **public** GitHub repo, upload all files from this folder (keep `.github/workflows/update.yml`). Default branch `main`.
2. Settings -> Secrets and variables -> Actions -> New repository secret: `TMDB_API_KEY` = your TMDB key (v3 key or v4 token).
3. Settings -> Pages -> Source: **GitHub Actions**.
4. Actions -> "Update movie page" -> Run workflow. Open the run log: it prints one line per theater
   ("Cinemark ...: N showtimes today" or "FAILED - reason"). Page: `https://<user>.github.io/<repo>/`.

## Notes
- Runs 3 times a day (5am / 11am / 5pm Pacific in winter). Past showtimes are hidden in the browser by the viewer's clock.
- If a theater fails, the page still builds and shows a notice; if all three fail, the old page stays online.
- Scheduled runs pause after 60 days without repo activity; re-enable in the Actions tab or push any commit.
- Three views, switched by the icons under the title: Showtimes (cards with times), Posters (posters only plus a short
  list card; tap a poster for the schedule) and List (one card with every movie). The choice is remembered in the browser;
  on the first visit the inactive icons light up once as a hint. `#posters` / `#list` in the URL open a view directly.
- Local test without network: `python build.py --mock` (writes `site/index.html`).
- If GitHub's servers get blocked by a theater site, run `build.py` on the NAS instead (see chat notes).
