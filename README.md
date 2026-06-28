# MAS Financial Institutions Directory

A small internal web app (Privé design system) that automates the weekly
[MAS Financial Institutions Directory](https://eservices.mas.gov.sg/fid/institution)
workflow.

Instead of manually downloading the export and re-running a notebook, staff
open the app and click **Refresh**. The app fetches the latest directory from
MAS, stores it, and shows the **week-over-week delta** — new and removed
institutions — as a chart and tables. A second tab provides the full directory
with smart filters, and a Config tab manages stored files (with manual upload
as a fallback when auto-fetch isn't possible).

![delta tab](static/assets/prive-logo.svg)

## What it does

- **Refresh from MAS** — fetches the directory on demand, validates it, and
  stores it as `FID_<today>.xls`. Because `/print` is a JavaScript SPA, the
  fetch is layered (see [How the fetch works](#how-the-fetch-works)):
  a plain HTTP GET first, then a headless Chromium that runs the page's JS.
- **Week-over-week delta** — compares the **two most recent** snapshots
  (newest vs. previous), regardless of how many days apart they are.
  - Net change, new count, removed count KPIs.
  - Grouped bar chart of movements by sector (New vs. Removed).
  - Tables of new and removed institutions, with filters and a "copy summary".
- **No-update highlighting** — if the fetched file is identical to the last
  stored snapshot, a banner says so and no duplicate is stored. If the two
  snapshots differ but no institutions changed, a "no week-over-week changes"
  banner is shown.
- **Directory** — full latest snapshot with sector / licence multi-select
  filters, free-text search over name **and** address, sortable columns and
  pagination.
- **Config** — list of stored snapshots (date, size, delete), a drag-&-drop /
  click upload zone (manual fallback), and the storage path.
- **Auto-cleanup** — keeps the newest `MAS_MAX_FILES` (default **10**); older
  snapshots are deleted automatically on every refresh/upload.

## Run with Docker (recommended)

```bash
docker compose up --build
# open http://localhost:8000
```

Snapshots persist in the named volume `mas_data` (mounted at `/data`).

Or plain Docker:

```bash
docker build -t mas-fid .
docker run -p 8000:8000 -v mas_fid_data:/data mas-fid
```

## Run locally (dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py            # http://localhost:8000  (PORT env to override)
```

Locally the data directory defaults to `./mas_data` (where the sample
`FID_2026-06-22.xls` lives, so the Directory tab works immediately). The
delta needs **two** snapshots — click Refresh or upload a second file.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `MAS_DATA_DIR` | `./mas_data` (local) / `/data` (Docker) | Where snapshots are stored |
| `MAS_MAX_FILES` | `10` | Auto-cleanup threshold |
| `MAS_FETCH_METHOD` | `auto` | `auto` (HTTP then headless browser), `requests`, or `playwright` |
| `MAS_FID_PRINT_URL` | `https://eservices.mas.gov.sg/fid/institution/print` | Export endpoint |
| `MAS_FID_BASE_URL` | `https://eservices.mas.gov.sg/fid/institution` | Used to prime cookies / referer |
| `MAS_FETCH_TIMEOUT` | `120` | HTTP timeout (seconds) |
| `MAS_PLAYWRIGHT_TIMEOUT` | `60000` | Headless-browser navigation timeout (ms) |
| `MAS_CHROMIUM_PATH` | _(auto)_ | Explicit Chromium binary; only needed if Playwright can't auto-locate one |
| `PORT` | `8000` | Listen port |

## How the fetch works

`/fid/institution/print` is an Angular SPA, so a plain HTTP GET only returns the
page shell. Refresh therefore tries, in order (controlled by `MAS_FETCH_METHOD`):

1. **HTTP GET** (`requests`) — fast; used if MAS ever streams the file directly.
   The response is validated, and rejected if it's just the SPA shell.
2. **Headless Chromium** (`playwright`) — loads the page, runs its JS, then:
   1. uses the underlying **XHR/JSON** the SPA fetches (cleanest), else
   2. captures a **JS-triggered file download**, else
   3. **scrapes the rendered HTML table**.

Whatever the source, the data is normalised to the canonical 9-column
tab-separated layout (column headers are alias-matched, e.g. `Telephone` →
`Phone Number`), validated for a plausible row count, and saved. The headless
browser and all its OS dependencies ship in the Docker image, so no extra setup
is needed there.

> Running outside Docker? Install the browser once with
> `playwright install chromium` (or `--with-deps` on Linux).

## Network requirement / manual fallback

Auto-refresh needs outbound HTTPS access to `eservices.mas.gov.sg` **from the
container**. If the host is blocked (egress policy, captcha, or a MAS layout
change), Refresh fails gracefully with a clear message (it reports what each
method tried) and the **Config tab** lets you upload the export by hand:

1. Open <https://eservices.mas.gov.sg/fid/institution/print> in a browser.
2. Save the file.
3. Drag it onto the Config upload zone.

Uploaded files are validated as real FID exports before being stored. The date
is taken from the filename (`FID_YYYY-MM-DD.xls` or `FID_YYYYMMDD.xls`);
otherwise today's date is used.

## How the delta is computed

Each institution is identified by `(Organisation Name, Sector, Licence
Type/Status)`. Multi-row companies (one row per activity) are collapsed to a
single row with activities joined by ` | `. The delta is the set difference of
these identity keys between the two most recent snapshots. Administrative
"LODGED NOTICE OF CESSATION" rows are excluded from the *new* list.

## Project layout

```
app.py             Flask app + REST API
fetcher.py         Layered MAS download (HTTP + headless browser) + normalisation
mas_scrapper.py    Core analysis (load, dedupe, delta, filter) — notebook-compatible
mas_scrapper.ipynb Original notebook workflow (still works against mas_scrapper.py)
static/            Frontend (Privé internal design system, no build step)
  index.html, app.js, styles.css, theme.css
  assets/prive-logo-dark.svg, assets/prive-logo-light.svg
Dockerfile, docker-compose.yml
mas_data/          Local snapshots (sample FID_2026-06-22.xls included)
```

> **Note on the logo:** the two SVGs in `static/assets/` are fallback wordmarks.
> The header automatically prefers official PNGs if present — just drop
> `prive-logo-dark.png` and `prive-logo-light.png` into `static/assets/`
> (no code change needed). The dark variant is used in dark theme, the light
> variant in light theme.

## API (for reference)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/state` | File list + whether a delta is available |
| `POST` | `/api/refresh` | Fetch latest from MAS, store, return delta |
| `GET` | `/api/delta` | Delta between the two latest snapshots (filterable) |
| `GET` | `/api/directory` | Paginated full directory (filterable, sortable) |
| `GET` | `/api/files` | List stored snapshots |
| `POST` | `/api/upload` | Manual snapshot upload |
| `DELETE` | `/api/files/<name>` | Delete a snapshot |
| `GET` | `/healthz` | Health check |

Filter query params: repeated `sector=`, repeated `licence=`, `q=` (name/address),
plus `page`, `page_size`, `sort`, `dir` for the directory.
