"""FastAPI read API plus the static frontend.

This is the *dynamic* deployment path: it can answer any ``[start, end)`` and
re-bucket on the fly, so zooming the range slider fetches genuinely finer data.
The alternative path -- ``gridcast export`` writing static JSON -- needs no
server at all. See DEPLOY.md for which to pick.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import queries, ranges, store
from .config import DEFAULT_LOCATION, LOCATIONS, WEB_DIR
from .timeutil import now_utc, parse_utc

log = logging.getLogger("gridcast.api")

app = FastAPI(title="gridcast", version="0.1.0", docs_url="/api/docs")

# Wide open on purpose: everything served is public grid and weather data, and
# it lets you run the frontend from a different origin during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _window(
    start: str | None, end: str | None, preset: str | None
) -> tuple[datetime, datetime]:
    with store.session(read_only=True) as connection:
        earliest, latest = queries.data_range(connection)
    if start and end:
        return parse_utc(start), parse_utc(end)
    try:
        return ranges.resolve(preset or ranges.DEFAULT_PRESET, earliest, latest)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _check_location(location: str) -> str:
    if location not in LOCATIONS:
        raise HTTPException(status_code=400, detail=f"unknown location {location!r}")
    return location


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    with store.session(read_only=True) as connection:
        earliest, latest = queries.data_range(connection)
        tables = store.table_stats(connection)
        available = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT location FROM weather_actual ORDER BY 1"
            ).fetchall()
        ]
    return {
        "generated_at": now_utc().isoformat(),
        "range": {
            "start": earliest.isoformat() if earliest else None,
            "end": latest.isoformat() if latest else None,
        },
        "locations": [
            {"key": k, "name": v.name, "latitude": v.latitude, "longitude": v.longitude}
            for k, v in LOCATIONS.items()
            if not available or k in available
        ],
        "default_location": DEFAULT_LOCATION,
        "presets": {k: label for k, (label, _) in ranges.PRESETS.items()},
        "default_preset": ranges.DEFAULT_PRESET,
        "tables": tables,
        "mode": "api",
    }


def _endpoint(fn):
    def handler(
        start: str | None = Query(None, description="ISO-8601 UTC"),
        end: str | None = Query(None, description="ISO-8601 UTC"),
        range: str | None = Query(None, description="preset key, e.g. 14d"),
        location: str = Query(DEFAULT_LOCATION),
    ) -> dict[str, Any]:
        _check_location(location)
        window_start, window_end = _window(start, end, range)
        with store.session(read_only=True) as connection:
            return fn(connection, window_start, window_end, location)

    return handler


app.get("/api/series")(_endpoint(queries.series))
app.get("/api/response-curve")(_endpoint(queries.response_curve))
app.get("/api/forecast-error")(_endpoint(queries.forecast_error))
app.get("/api/fuel-mix")(_endpoint(queries.fuel_mix))


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Mounted last so /api/* wins. html=True serves index.html at /.
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
