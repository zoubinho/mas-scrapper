"""
Fetch the latest MAS Financial Institutions Directory export.

The directory at https://eservices.mas.gov.sg/fid/institution is an Angular SPA,
and the "export" button links to .../fid/institution/print. Depending on how MAS
serves it, that endpoint is either a direct tab-separated download or a
JS-rendered HTML table. We therefore try, in order:

  1. requests   - a plain HTTP GET (works if /print streams the file directly).
  2. playwright - a headless Chromium that runs the SPA's JS and then:
       a. uses the underlying XHR/JSON the page loads (cleanest), else
       b. captures a JS-triggered file download, else
       c. scrapes the rendered HTML table.

Whatever the source, the result is normalised to the canonical 9-column
tab-separated layout the rest of the app expects and saved as FID_<date>.xls.

If every method fails (network policy, captcha, layout change, ...) ``fetch_latest``
raises ``FetchError`` so the web app can fall back to the manual upload flow.
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import os
import re
import tempfile
from pathlib import Path

import pandas as pd
import requests

import mas_scrapper as core

PRINT_URL = os.environ.get(
    "MAS_FID_PRINT_URL", "https://eservices.mas.gov.sg/fid/institution/print"
)
BASE_URL = os.environ.get(
    "MAS_FID_BASE_URL", "https://eservices.mas.gov.sg/fid/institution"
)
TIMEOUT = int(os.environ.get("MAS_FETCH_TIMEOUT", "120"))

# auto (requests then playwright) | requests | playwright
FETCH_METHOD = os.environ.get("MAS_FETCH_METHOD", "auto").lower()
PLAYWRIGHT_TIMEOUT_MS = int(os.environ.get("MAS_PLAYWRIGHT_TIMEOUT", "60000"))
# Explicit Chromium binary; if unset Playwright auto-locates (works in the
# official mcr.microsoft.com/playwright image).
CHROMIUM_PATH = os.environ.get("MAS_CHROMIUM_PATH")

CANONICAL_COLS = core.ALL_COLS  # 9 columns

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
}

REQUIRED_TOKEN = "Organisation Name"


class FetchError(RuntimeError):
    """Raised when the live download cannot be completed/validated."""


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

# Map a normalised header/key to a canonical column name.
_HEADER_ALIASES = {
    "no": "No.", "sno": "No.", "sn": "No.", "serialno": "No.", "number": "No.",
    "organisationname": "Organisation Name", "organizationname": "Organisation Name",
    "institutionname": "Organisation Name", "nameofinstitution": "Organisation Name",
    "entityname": "Organisation Name", "name": "Organisation Name",
    "address": "Address",
    "phonenumber": "Phone Number", "phone": "Phone Number",
    "telephone": "Phone Number", "contactnumber": "Phone Number", "tel": "Phone Number",
    "website": "Website", "url": "Website", "web": "Website",
    "sector": "Sector",
    "licencetypestatus": "Licence Type/Status", "licensetypestatus": "Licence Type/Status",
    "licencetype": "Licence Type/Status", "licensetype": "Licence Type/Status",
    "licence": "Licence Type/Status", "license": "Licence Type/Status",
    "licencestatus": "Licence Type/Status", "status": "Licence Type/Status",
    "activitybusinesstype": "Activity/Business Type", "activitytype": "Activity/Business Type",
    "businesstype": "Activity/Business Type", "activity": "Activity/Business Type",
    "subactivityproduct": "Sub-Activity/Product", "subactivity": "Sub-Activity/Product",
    "product": "Sub-Activity/Product", "subactivitytype": "Sub-Activity/Product",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _canon(header: str) -> str | None:
    return _HEADER_ALIASES.get(_norm(header))


def _frame_to_tsv(df: pd.DataFrame) -> str:
    """Order to the canonical columns (fill missing with '') and emit TSV."""
    out = pd.DataFrame()
    for col in CANONICAL_COLS:
        out[col] = df[col] if col in df.columns else ""
    out = out.fillna("").astype(str)
    return out.to_csv(sep="\t", index=False)


def _rows_to_frame(header: list[str], rows: list[list[str]]) -> pd.DataFrame | None:
    """Build a canonical DataFrame from a scraped HTML table."""
    if header:
        mapping = {i: _canon(h) for i, h in enumerate(header)}
        if "Organisation Name" not in mapping.values():
            mapping = None
    else:
        mapping = None

    if mapping is None:
        # No usable header: only safe if every row has exactly 9 cells.
        widths = {len(r) for r in rows}
        if widths != {len(CANONICAL_COLS)}:
            return None
        mapping = {i: CANONICAL_COLS[i] for i in range(len(CANONICAL_COLS))}

    records = []
    for r in rows:
        rec = {c: "" for c in CANONICAL_COLS}
        for i, val in enumerate(r):
            col = mapping.get(i)
            if col:
                rec[col] = (val or "").strip()
        records.append(rec)
    df = pd.DataFrame(records)
    return df if not df.empty and df["Organisation Name"].astype(bool).any() else None


def _find_records(obj, depth: int = 0):
    """Recursively find the first list-of-dicts that looks like institutions."""
    if depth > 6:
        return None
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and any(_canon(k) == "Organisation Name" for k in dicts[0]):
            return dicts
        for x in obj:
            found = _find_records(x, depth + 1)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            found = _find_records(v, depth + 1)
            if found:
                return found
    return None


def _records_to_frame(records: list[dict]) -> pd.DataFrame | None:
    """Build a canonical DataFrame from captured JSON records."""
    rows = []
    for rec in records:
        out = {c: "" for c in CANONICAL_COLS}
        for k, v in rec.items():
            col = _canon(k)
            if col and v is not None and not isinstance(v, (dict, list)):
                out[col] = str(v).strip()
        rows.append(out)
    df = pd.DataFrame(rows)
    return df if not df.empty and df["Organisation Name"].astype(bool).any() else None


def _looks_like_fid(text: str) -> bool:
    head = text[:4000]
    if "<html" in head.lower() or "<!doctype" in head.lower():
        return False
    return REQUIRED_TOKEN in head and "Sector" in head


def _validate_text(text: str, min_rows: int = 50) -> str:
    """Ensure the TSV parses to a plausible FID export; return it unchanged."""
    if not _looks_like_fid(text):
        raise FetchError("payload is not a recognisable FID export.")
    n = max(0, text.strip().count("\n"))  # rows excluding header
    if n < min_rows:
        raise FetchError(f"only {n} data rows parsed; the export looks truncated.")
    return text


# ---------------------------------------------------------------------------
# Method 1: plain HTTP
# ---------------------------------------------------------------------------

def _download_requests() -> str:
    sess = requests.Session()
    try:
        try:
            sess.get(BASE_URL, headers=_HEADERS, timeout=TIMEOUT)  # prime cookies
        except requests.RequestException:
            pass
        resp = sess.get(PRINT_URL, headers=_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"HTTP GET failed: {exc}") from exc
    return _validate_text(resp.content.decode("utf-8-sig", errors="replace"))


# ---------------------------------------------------------------------------
# Method 2: headless Chromium
# ---------------------------------------------------------------------------

def _chromium_executable() -> str | None:
    if CHROMIUM_PATH:
        return CHROMIUM_PATH
    for pat in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium-*/chrome-linux64/chrome",
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None  # let Playwright auto-locate (official image)


def _download_playwright() -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise FetchError(
            "Playwright is not installed; cannot render the SPA. "
            "Install it (or use the official Playwright image)."
        ) from exc

    captured_json: list = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=True,
                executable_path=_chromium_executable(),
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:  # pragma: no cover
            raise FetchError(f"could not launch Chromium: {exc}") from exc

        page = browser.new_page(user_agent=_HEADERS["User-Agent"])
        page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)

        def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" in ct and resp.request.resource_type in ("xhr", "fetch"):
                    captured_json.append(resp.json())
            except Exception:
                pass

        page.on("response", on_response)

        downloaded_text = None
        try:
            with page.expect_download(timeout=4000) as dl:
                _safe_goto(page)
            downloaded_text = Path(dl.value.path()).read_text(
                encoding="utf-8-sig", errors="replace"
            )
        except Exception:
            # No download fired — normal SPA render path.
            try:
                page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
            except Exception:
                pass

        # (b) JS-triggered file download
        if downloaded_text and _looks_like_fid(downloaded_text):
            browser.close()
            return _validate_text(downloaded_text)

        # (a) underlying XHR JSON
        for payload in captured_json:
            records = _find_records(payload)
            if records:
                df = _records_to_frame(records)
                if df is not None and len(df) >= 50:
                    browser.close()
                    return _validate_text(_frame_to_tsv(df))

        # (c) scrape the rendered table
        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
        except Exception:
            browser.close()
            raise FetchError("no data table rendered on the print page.")

        header = page.eval_on_selector_all(
            "table thead th, table thead td",
            "els => els.map(e => e.innerText.trim())",
        )
        rows = page.eval_on_selector_all(
            "table tbody tr",
            "rs => rs.map(r => Array.from(r.cells).map(c => c.innerText.trim()))",
        )
        browser.close()

    df = _rows_to_frame(header, rows)
    if df is None:
        raise FetchError(
            "rendered table did not match the expected FID columns "
            f"(headers seen: {header[:12]})."
        )
    return _validate_text(_frame_to_tsv(df))


# helper so the goto inside expect_download doesn't hard-fail on abort
def _safe_goto(page):
    try:
        page.goto(PRINT_URL, wait_until="commit", timeout=PLAYWRIGHT_TIMEOUT_MS)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _methods() -> list:
    if FETCH_METHOD == "requests":
        return [("requests", _download_requests)]
    if FETCH_METHOD == "playwright":
        return [("playwright", _download_playwright)]
    return [("requests", _download_requests), ("playwright", _download_playwright)]


def download_raw() -> str:
    """Return the FID export as canonical TSV text, trying each method in turn."""
    errors = []
    for name, fn in _methods():
        try:
            return fn()
        except FetchError as exc:
            errors.append(f"{name}: {exc}")
    raise FetchError(
        "Auto-refresh failed. " + "  |  ".join(errors)
        + "  Upload the file manually in Config instead."
    )


# ---------------------------------------------------------------------------
# Content signature for method-independent "no update" detection
# ---------------------------------------------------------------------------

def _signature(df: pd.DataFrame) -> str:
    cols = [c for c in CANONICAL_COLS if c in df.columns]
    lines = sorted("\t".join(str(r[c]) for c in cols) for _, r in df[cols].iterrows())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def fetch_latest(target_dir: Path | None = None, today: str | None = None) -> dict:
    """Download today's directory and save it.

    Returns a dict::

        {"saved": bool, "path": str, "date": "YYYY-MM-DD",
         "unchanged": bool, "previous_date": str|None, "method": str}
    """
    target = Path(target_dir) if target_dir else core.data_dir()
    target.mkdir(parents=True, exist_ok=True)
    date_str = today or _dt.date.today().isoformat()

    text = download_raw()

    # Parse once for the no-update comparison.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xls", dir=target, delete=False, encoding="utf-8"
    ) as tf:
        tf.write(text)
        tmp_path = Path(tf.name)

    try:
        new_df = core.load_file(tmp_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise FetchError(f"downloaded data could not be parsed: {exc}") from exc

    existing = core.list_files()
    prev_date = existing[-1][0] if existing else None
    prev_path = existing[-1][1] if existing else None
    out_path = target / f"FID_{date_str}.xls"

    if prev_path is not None and prev_path.exists() and prev_path.name != out_path.name:
        try:
            if _signature(new_df) == _signature(core.load_file(prev_path)):
                tmp_path.unlink(missing_ok=True)
                return {
                    "saved": False, "path": str(prev_path), "date": prev_date,
                    "unchanged": True, "previous_date": prev_date,
                }
        except Exception:
            pass  # fall through to saving

    tmp_path.replace(out_path)
    return {
        "saved": True,
        "path": str(out_path),
        "date": date_str,
        "unchanged": False,
        "previous_date": prev_date if prev_date != date_str
        else (existing[-2][0] if len(existing) >= 2 else None),
    }
