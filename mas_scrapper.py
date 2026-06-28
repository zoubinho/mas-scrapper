"""
MAS Financial Institutions Directory - core analysis.

Compares two weekly MAS FID exports and surfaces new/removed institutions,
and exposes helpers used by the web app (file discovery, directory query,
content hashing).

The data directory is configurable through the ``MAS_DATA_DIR`` environment
variable so the same code runs both locally (Jupyter notebook) and inside the
Docker container (mounted volume).
"""

from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _default_data_dir() -> Path:
    env = os.environ.get("MAS_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).parent / "mas_data"


DATA_DIR = _default_data_dir()

# Accept both the MAS native export name (FID_YYYYMMDD.xls) and the
# dash-separated convention used by the original notebook (FID_YYYY-MM-DD.xls).
FILE_PATTERN = re.compile(r"FID[_-]?(\d{4})-?(\d{2})-?(\d{2})\.xls$", re.IGNORECASE)

MAX_FILES = int(os.environ.get("MAS_MAX_FILES", "10"))

IDENTITY_COLS = ["Organisation Name", "Sector", "Licence Type/Status"]
ALL_COLS = [
    "No.", "Organisation Name", "Address", "Phone Number", "Website",
    "Sector", "Licence Type/Status", "Activity/Business Type", "Sub-Activity/Product",
]


def data_dir() -> Path:
    """Return the (re-resolved) data directory, creating it if needed."""
    d = _default_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _date_from_name(name: str) -> str | None:
    m = FILE_PATTERN.search(name)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def list_files() -> list[tuple[str, Path]]:
    """Return list of (date_str, path) sorted oldest -> newest."""
    d = data_dir()
    results = []
    for f in d.iterdir():
        if not f.is_file():
            continue
        date_str = _date_from_name(f.name)
        if date_str:
            results.append((date_str, f))
    # Sort by date, then by name so duplicates of the same date are stable.
    return sorted(results, key=lambda x: (x[0], x[1].name))


def get_last_two_files() -> tuple[tuple[str, Path], tuple[str, Path]]:
    """Return the two most recent (date, path) tuples."""
    files = list_files()
    if len(files) < 2:
        raise ValueError(
            f"Need at least 2 FID files in {data_dir()}, found {len(files)}."
        )
    return files[-2], files[-1]


def file_hash(path: Path) -> str:
    """SHA-256 of a file's bytes (used to detect 'no update' on refresh)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_file(path: Path) -> pd.DataFrame:
    """Load a MAS FID export and return a cleaned DataFrame.

    The native MAS export is tab-separated despite the .xls extension. We try
    that first, then fall back to comma-separated and finally to a real Excel
    workbook so manual uploads in any of those shapes still work.
    """
    df = None
    for sep in ("\t", ","):
        try:
            candidate = pd.read_csv(path, sep=sep, dtype=str).fillna("")
        except Exception:
            continue
        cols = [c.strip() for c in candidate.columns]
        if "Organisation Name" in cols:
            candidate.columns = cols
            df = candidate
            break
    if df is None:
        # Last resort: a genuine Excel workbook.
        df = pd.read_excel(path, dtype=str).fillna("")
        df.columns = [c.strip() for c in df.columns]

    if "Organisation Name" not in df.columns:
        raise ValueError(
            "File does not look like a MAS FID export "
            "(missing 'Organisation Name' column)."
        )

    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    # Drop rows that are not real institutions: a header row that leaked into
    # the body (e.g. when a scraped table has no separate <thead>) or rows with
    # a blank organisation name. These otherwise surface as a bogus "new" entry
    # and pollute the deduplicated directory.
    name = df["Organisation Name"].astype(str).str.strip()
    df = df[(name != "") & (name.str.lower() != "organisation name")]
    return df.reset_index(drop=True)


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse multi-row companies into one row per identity key.

    Activities/products are aggregated with ' | '.
    """
    work = df.copy()
    for col in ALL_COLS:
        if col not in work.columns:
            work[col] = ""

    agg = (
        work.groupby(IDENTITY_COLS, sort=False)
        .agg(
            {
                "No.": "first",
                "Address": "first",
                "Phone Number": "first",
                "Website": "first",
                "Activity/Business Type": lambda v: " | ".join(
                    dict.fromkeys(x for x in v if x)
                ),
                "Sub-Activity/Product": lambda v: " | ".join(
                    dict.fromkeys(x for x in v if x)
                ),
            }
        )
        .reset_index()
    )
    return agg[ALL_COLS]


@lru_cache(maxsize=32)
def _load_dedup_cached(path_str: str, mtime: float, size: int) -> pd.DataFrame:
    return deduplicate(load_file(Path(path_str)))


def load_dedup(path: Path) -> pd.DataFrame:
    """Cached load+deduplicate keyed on file path, mtime and size."""
    st = path.stat()
    return _load_dedup_cached(str(path), st.st_mtime, st.st_size).copy()


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

def _keys(df: pd.DataFrame) -> set:
    return set(
        zip(df["Organisation Name"], df["Sector"], df["Licence Type/Status"])
    )


def compute_delta(
    old_df: pd.DataFrame, new_df: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (new_entries, removed_entries) DataFrames.

    Identity key = (Organisation Name, Sector, Licence Type/Status).
    """
    old_keys = _keys(old_df)
    new_keys = _keys(new_df)
    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys

    def key_col(df: pd.DataFrame) -> pd.Series:
        return pd.Series(
            list(zip(df["Organisation Name"], df["Sector"], df["Licence Type/Status"])),
            index=df.index,
        )

    new_entries = new_df[key_col(new_df).isin(added_keys)].reset_index(drop=True)
    removed_entries = old_df[key_col(old_df).isin(removed_keys)].reset_index(drop=True)
    return new_entries, removed_entries


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def apply_filters(
    df: pd.DataFrame,
    sectors: list[str] | None = None,
    licence_types: list[str] | None = None,
    name_query: str = "",
) -> pd.DataFrame:
    """Filter a DataFrame by sector, licence type and free-text search.

    The free-text search matches against organisation name AND address.
    """
    result = df
    if sectors:
        result = result[result["Sector"].isin(sectors)]
    if licence_types:
        result = result[result["Licence Type/Status"].isin(licence_types)]
    if name_query:
        q = name_query.strip()
        if q:
            mask = (
                result["Organisation Name"].str.contains(q, case=False, na=False, regex=False)
                | result["Address"].str.contains(q, case=False, na=False, regex=False)
            )
            result = result[mask]
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Chart data helpers
# ---------------------------------------------------------------------------

def sector_counts(new_entries: pd.DataFrame, removed_entries: pd.DataFrame) -> pd.DataFrame:
    """Build a summary DataFrame [Sector, New, Removed]."""
    new_counts = new_entries["Sector"].value_counts().rename("New")
    removed_counts = removed_entries["Sector"].value_counts().rename("Removed")
    summary = (
        pd.concat([new_counts, removed_counts], axis=1)
        .fillna(0)
        .astype(int)
        .reset_index()
    )
    summary = summary.rename(columns={summary.columns[0]: "Sector"})
    summary = summary.sort_values("Sector").reset_index(drop=True)
    return summary


# Rows that are administrative notices rather than real new institutions.
_CESSATION_RE = re.compile("LODGED NOTICE OF CESSATION", re.IGNORECASE)


def _drop_cessation(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df["Organisation Name"].str.contains(_CESSATION_RE, na=False)
    return df[~mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Top-level entry point (kept compatible with the original notebook)
# ---------------------------------------------------------------------------

def run_analysis(
    sectors: list[str] | None = None,
    licence_types: list[str] | None = None,
    name_query: str = "",
    drop_cessation: bool = False,
) -> dict:
    """Full pipeline: discover files -> load -> dedupe -> delta -> filter."""
    (old_date, old_path), (new_date, new_path) = get_last_two_files()

    old_df = load_dedup(old_path)
    new_df = load_dedup(new_path)

    new_entries, removed_entries = compute_delta(old_df, new_df)
    if drop_cessation:
        new_entries = _drop_cessation(new_entries)

    chart_data = sector_counts(new_entries, removed_entries)

    new_filtered = apply_filters(new_entries, sectors, licence_types, name_query)
    removed_filtered = apply_filters(removed_entries, sectors, licence_types, name_query)

    all_sectors = sorted((set(old_df["Sector"]) | set(new_df["Sector"])) - {""})
    all_licence = sorted(
        (set(old_df["Licence Type/Status"]) | set(new_df["Licence Type/Status"])) - {""}
    )

    return {
        "old_date": old_date,
        "new_date": new_date,
        "old_path": str(old_path),
        "new_path": str(new_path),
        "old_count": len(old_df),
        "new_count": len(new_df),
        "new_entries": new_entries,
        "removed_entries": removed_entries,
        "new_filtered": new_filtered,
        "removed_filtered": removed_filtered,
        "chart_data": chart_data,
        "all_sectors": all_sectors,
        "all_licence_types": all_licence,
    }
