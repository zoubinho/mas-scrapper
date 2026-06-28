"""
MAS Financial Institutions Directory - web app.

A small Flask service that:
  * fetches the latest directory from MAS on demand ("Refresh"),
  * computes the week-over-week delta between the two most recent snapshots,
  * serves the full directory with smart filters,
  * lets staff manage / upload snapshot files,
  * auto-cleans old snapshots (keeps the newest MAX_FILES).

The UI (Prive internal design system) is served from ./static.
"""

from __future__ import annotations

import datetime as _dt
import math
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import mas_scrapper as core
from fetcher import FetchError, fetch_latest

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB upload cap

DELTA_COLS = [
    "Organisation Name", "Sector", "Licence Type/Status",
    "Activity/Business Type", "Address", "Phone Number", "Website",
]
DIRECTORY_COLS = [
    "Organisation Name", "Sector", "Licence Type/Status",
    "Activity/Business Type", "Sub-Activity/Product",
    "Address", "Phone Number", "Website",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _records(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    present = [c for c in cols if c in df.columns]
    if df.empty:
        return []
    out = df[present].fillna("").astype(str)
    return out.to_dict(orient="records")


def _request_filters() -> tuple[list[str], list[str], str]:
    sectors = [s for s in request.args.getlist("sector") if s]
    licences = [s for s in request.args.getlist("licence") if s]
    query = request.args.get("q", "", type=str).strip()
    return sectors, licences, query


def _cleanup(max_files: int = core.MAX_FILES) -> list[str]:
    """Delete oldest snapshots beyond max_files. Returns deleted dates."""
    files = core.list_files()
    deleted: list[str] = []
    if len(files) <= max_files:
        return deleted
    for date_str, path in files[:-max_files]:
        try:
            path.unlink()
            deleted.append(date_str)
        except OSError:
            pass
    return deleted


def _file_info() -> list[dict]:
    info = []
    for date_str, path in core.list_files():
        st = path.stat()
        info.append({
            "date": date_str,
            "name": path.name,
            "size_kb": round(st.st_size / 1024, 1),
            "modified": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    # newest first for display
    return list(reversed(info))


def _delta_payload(drop_cessation: bool = True) -> dict:
    files = core.list_files()
    base = {
        "files": _file_info(),
        "max_files": core.MAX_FILES,
        "data_dir": str(core.data_dir()),
    }
    if len(files) < 2:
        base.update({
            "available": False,
            "message": (
                "Need at least two snapshots to compute a delta. "
                "Click Refresh to fetch the latest, or upload files in Config."
            ),
        })
        return base

    sectors, licences, query = _request_filters()
    result = core.run_analysis(
        sectors=sectors or None,
        licence_types=licences or None,
        name_query=query,
        drop_cessation=drop_cessation,
    )
    n_new = len(result["new_filtered"])
    n_removed = len(result["removed_filtered"])
    total_new = len(result["new_entries"])
    total_removed = len(result["removed_entries"])

    chart = [
        {"sector": r["Sector"], "new": int(r["New"]), "removed": int(r["Removed"])}
        for _, r in result["chart_data"].iterrows()
    ]

    base.update({
        "available": True,
        "old_date": result["old_date"],
        "new_date": result["new_date"],
        "old_count": result["old_count"],
        "new_count": result["new_count"],
        "net_change": result["new_count"] - result["old_count"],
        "total_new": total_new,
        "total_removed": total_removed,
        "shown_new": n_new,
        "shown_removed": n_removed,
        "no_changes": total_new == 0 and total_removed == 0,
        "chart": chart,
        "new_entries": _records(result["new_filtered"], DELTA_COLS),
        "removed_entries": _records(result["removed_filtered"], DELTA_COLS),
        "columns": DELTA_COLS,
        "all_sectors": result["all_sectors"],
        "all_licence_types": result["all_licence_types"],
    })
    return base


# ---------------------------------------------------------------------------
# Static / SPA
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "files": len(core.list_files())})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    """Lightweight state for first paint: file list + whether delta is possible."""
    files = core.list_files()
    return jsonify({
        "files": _file_info(),
        "data_dir": str(core.data_dir()),
        "max_files": core.MAX_FILES,
        "has_delta": len(files) >= 2,
        "latest_date": files[-1][0] if files else None,
        "today": _dt.date.today().isoformat(),
    })


@app.post("/api/refresh")
def api_refresh():
    """Fetch the latest export from MAS, store it, clean up, return the delta."""
    try:
        result = fetch_latest()
    except FetchError as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
            "fallback": "manual_upload",
        }), 502

    deleted = _cleanup()
    # Clear the dataframe cache so the new file is picked up.
    core._load_dedup_cached.cache_clear()

    payload = _delta_payload()
    payload.update({
        "ok": True,
        "refresh": {
            "saved": result["saved"],
            "unchanged": result["unchanged"],
            "date": result["date"],
            "previous_date": result.get("previous_date"),
            "deleted": deleted,
        },
    })
    return jsonify(payload)


@app.get("/api/delta")
def api_delta():
    return jsonify(_delta_payload())


@app.get("/api/directory")
def api_directory():
    """Paginated full directory (latest snapshot) with smart filters."""
    files = core.list_files()
    if not files:
        return jsonify({"available": False, "message": "No snapshots available yet."})

    date_str, path = files[-1]
    df = core.load_dedup(path)

    sectors, licences, query = _request_filters()
    filtered = core.apply_filters(df, sectors or None, licences or None, query)

    sort_col = request.args.get("sort", "Organisation Name")
    if sort_col in filtered.columns:
        ascending = request.args.get("dir", "asc") != "desc"
        filtered = filtered.sort_values(
            sort_col, key=lambda s: s.str.lower(), ascending=ascending
        ).reset_index(drop=True)

    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(500, max(10, request.args.get("page_size", 50, type=int)))
    total = len(filtered)
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    start = (page - 1) * page_size
    window = filtered.iloc[start:start + page_size]

    return jsonify({
        "available": True,
        "date": date_str,
        "total": total,
        "grand_total": len(df),
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "rows": _records(window, DIRECTORY_COLS),
        "columns": DIRECTORY_COLS,
        "all_sectors": sorted(set(df["Sector"]) - {""}),
        "all_licence_types": sorted(set(df["Licence Type/Status"]) - {""}),
    })


@app.get("/api/files")
def api_files():
    return jsonify({
        "files": _file_info(),
        "data_dir": str(core.data_dir()),
        "max_files": core.MAX_FILES,
    })


@app.post("/api/upload")
def api_upload():
    """Manual upload fallback for when auto-refresh is not possible."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part in request."}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "No file selected."}), 400

    safe = secure_filename(f.filename)
    date_str = core._date_from_name(safe) or request.form.get("date") \
        or _dt.date.today().isoformat()
    try:
        _dt.date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"ok": False, "error": f"Bad date: {date_str}"}), 400

    target = core.data_dir() / f"FID_{date_str}.xls"
    tmp = target.with_suffix(".tmp")
    f.save(tmp)

    # Validate it really parses as a FID export before committing.
    try:
        df = core.load_file(tmp)
        if "Organisation Name" not in df.columns or len(df) == 0:
            raise ValueError("empty or missing 'Organisation Name' column")
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return jsonify({
            "ok": False,
            "error": f"Could not parse '{safe}' as a MAS FID export: {exc}",
        }), 400

    tmp.replace(target)
    deleted = _cleanup()
    core._load_dedup_cached.cache_clear()
    return jsonify({
        "ok": True,
        "saved": target.name,
        "date": date_str,
        "rows": len(df),
        "deleted": deleted,
        "files": _file_info(),
    })


@app.delete("/api/files/<name>")
def api_delete(name: str):
    safe = secure_filename(name)
    path = core.data_dir() / safe
    if core._date_from_name(safe) is None or not path.exists():
        return jsonify({"ok": False, "error": "Unknown file."}), 404
    path.unlink()
    core._load_dedup_cached.cache_clear()
    return jsonify({"ok": True, "deleted": safe, "files": _file_info()})


if __name__ == "__main__":
    core.data_dir()  # ensure it exists
    app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", "5570")))
