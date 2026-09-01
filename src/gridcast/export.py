"""Render the API's responses to static JSON.

This is what makes zero-backend hosting work. The scheduled job runs
``update`` then ``export``, commits ``web/data/``, and the push triggers a
redeploy -- so a purely static host (Vercel, GitHub Pages, Netlify, an S3
bucket) serves a site that is never more than one cron interval stale, with no
server, no cold starts and no running costs.

The tradeoff versus the FastAPI path is that only the preset windows in
``ranges.PRESETS`` exist, so zooming pans within an already-loaded window
instead of fetching finer data. For a personal project that is almost always
the right trade.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import duckdb

from . import queries, ranges, store
from .config import DEFAULT_LOCATION, LOCATIONS, WEB_DATA_DIR, ensure_dirs
from .timeutil import now_utc

log = logging.getLogger("gridcast.export")

VIEWS: dict[str, Callable[..., dict[str, Any]]] = {
    "series": queries.series,
    "response-curve": queries.response_curve,
    "forecast-error": queries.forecast_error,
    "fuel-mix": queries.fuel_mix,
}


def _write(name: str, payload: dict[str, Any]) -> int:
    path = WEB_DATA_DIR / f"{name}.json"
    # separators=(",", ":") drops ~15% of the bytes for columnar numeric data.
    text = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    path.write_text(text)
    return len(text)


def export_all(connection: duckdb.DuckDBPyConnection) -> dict[str, int]:
    ensure_dirs()
    earliest, latest = queries.data_range(connection)

    available = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT location FROM weather_actual ORDER BY 1"
        ).fetchall()
    ] or [DEFAULT_LOCATION]

    written: dict[str, int] = {}
    written["meta"] = _write(
        "meta",
        {
            "generated_at": now_utc().isoformat(),
            "range": {
                "start": earliest.isoformat() if earliest else None,
                "end": latest.isoformat() if latest else None,
            },
            "locations": [
                {
                    "key": key,
                    "name": LOCATIONS[key].name,
                    "latitude": LOCATIONS[key].latitude,
                    "longitude": LOCATIONS[key].longitude,
                }
                for key in available
                if key in LOCATIONS
            ],
            "default_location": DEFAULT_LOCATION if DEFAULT_LOCATION in available else available[0],
            "presets": {key: label for key, (label, _) in ranges.PRESETS.items()},
            "default_preset": ranges.DEFAULT_PRESET,
            "tables": store.table_stats(connection),
            "mode": "static",
        },
    )

    for location_key in available:
        for preset in ranges.PRESETS:
            start, end = ranges.resolve(preset, earliest, latest)
            for view_name, view in VIEWS.items():
                name = f"{view_name}-{location_key}-{preset}"
                written[name] = _write(name, view(connection, start, end, location_key))

    total = sum(written.values())
    log.info("exported %s files, %.1f KB total", len(written), total / 1024)
    return written
