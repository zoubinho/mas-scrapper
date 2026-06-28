"""
Fetch the latest MAS Financial Institutions Directory export.

The export button on https://eservices.mas.gov.sg/fid/institution links to
https://eservices.mas.gov.sg/fid/institution/print which returns the full
directory as a tab-separated file (served with a .xls extension).

This module downloads that file, validates that it really is a FID export, and
writes it into the data directory as ``FID_<today>.xls``.

If the download is blocked (network policy, MAS layout change, captcha, ...)
``fetch_latest`` raises ``FetchError`` with an explanatory message so the web
app can fall back to the manual upload flow.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import requests

import mas_scrapper as core


PRINT_URL = os.environ.get(
    "MAS_FID_PRINT_URL", "https://eservices.mas.gov.sg/fid/institution/print"
)
BASE_URL = os.environ.get(
    "MAS_FID_BASE_URL", "https://eservices.mas.gov.sg/fid/institution"
)
TIMEOUT = int(os.environ.get("MAS_FETCH_TIMEOUT", "120"))

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


def _looks_like_fid(text: str) -> bool:
    """Cheap validation that the payload is a FID export, not an HTML shell."""
    head = text[:4000]
    if "<html" in head.lower() or "<!doctype" in head.lower():
        return False
    return REQUIRED_TOKEN in head and "Sector" in head


def download_raw(session: requests.Session | None = None) -> str:
    """Return the raw text of the MAS export, or raise FetchError."""
    sess = session or requests.Session()
    try:
        # Prime cookies by hitting the directory page first (some WAFs require it).
        try:
            sess.get(BASE_URL, headers=_HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            pass

        resp = sess.get(PRINT_URL, headers=_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(
            f"Could not reach MAS ({PRINT_URL}): {exc}. "
            "Auto-refresh is unavailable here — upload the file manually."
        ) from exc

    text = resp.content.decode("utf-8-sig", errors="replace")
    if not _looks_like_fid(text):
        snippet = text[:200].replace("\n", " ")
        raise FetchError(
            "MAS responded but the payload is not a recognisable FID export "
            f"(got: {snippet!r}). The site layout may have changed, or a "
            "login/captcha is required. Use manual upload instead."
        )
    return text


def fetch_latest(target_dir: Path | None = None, today: str | None = None) -> dict:
    """Download today's directory and save it.

    Returns a dict describing what happened::

        {
            "saved": bool,        # False when content was unchanged
            "path": "<file>",     # path to today's file
            "date": "YYYY-MM-DD",
            "unchanged": bool,    # True when identical to previous newest file
            "previous_date": "YYYY-MM-DD" | None,
        }
    """
    target = Path(target_dir) if target_dir else core.data_dir()
    target.mkdir(parents=True, exist_ok=True)
    date_str = today or _dt.date.today().isoformat()

    text = download_raw()
    data = text.encode("utf-8")

    # Compare against the current newest file to detect "no update".
    existing = core.list_files()
    prev_date = existing[-1][0] if existing else None
    prev_path = existing[-1][1] if existing else None

    out_path = target / f"FID_{date_str}.xls"

    if prev_path is not None and prev_path.exists():
        import hashlib

        new_hash = hashlib.sha256(data).hexdigest()
        prev_hash = core.file_hash(prev_path)
        if new_hash == prev_hash and prev_path.name != out_path.name:
            # Directory is identical to last stored snapshot: don't duplicate.
            return {
                "saved": False,
                "path": str(prev_path),
                "date": prev_date,
                "unchanged": True,
                "previous_date": prev_date,
            }

    out_path.write_bytes(data)
    return {
        "saved": True,
        "path": str(out_path),
        "date": date_str,
        "unchanged": False,
        "previous_date": prev_date if prev_date != date_str else (
            existing[-2][0] if len(existing) >= 2 else None
        ),
    }
