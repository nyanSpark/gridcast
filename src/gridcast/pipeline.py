"""Ingestion orchestration: one full backfill, and one cheap incremental update.

``backfill`` is the slow path you run once. ``update`` is what the scheduled job
calls every few hours; it re-reads only the last couple of days (CAISO revises
recent intervals), refreshes the forward forecasts, and re-scores the recent
forecast archive. Both are idempotent, so an interrupted or double-fired run
costs nothing.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import duckdb

from . import store
from .config import (
    CAISO_OUTLOOK_EARLIEST,
    DEFAULT_LOCATION,
    EIA_API_KEY,
    LOCATIONS,
)
from .config import (
    location as get_location,
)
from .http import Fetcher, NotAvailable
from .sources import caiso_oasis, caiso_outlook, eia, openmeteo
from .timeutil import daterange, parse_date, today_pacific

log = logging.getLogger("gridcast.pipeline")

#: ERA5 reanalysis publishes with a few days' lag; the Forecast API's
#: ``past_days`` window covers the gap.
ERA5_LAG_DAYS = 6
ARCHIVE_CHUNK_DAYS = 366
#: Open-Meteo caps ``past_days`` at 92.
MAX_PREVIOUS_RUN_DAYS = 92


def _existing_days(connection: duckdb.DuckDBPyConnection, table: str) -> set[date]:
    """Pacific days already holding a plausible full complement of intervals."""
    rows = connection.execute(
        f"""
        SELECT (ts_utc AT TIME ZONE 'America/Los_Angeles')::DATE AS day_pt, count(*) AS n
        FROM {table} GROUP BY 1 HAVING count(*) >= 270
        """
    ).fetchall()
    return {row[0] for row in rows}


def ingest_caiso_outlook(
    connection: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    *,
    skip_existing: bool = True,
) -> int:
    """Pull every Today's Outlook series for each Pacific day in ``[start, end]``."""
    fetcher = Fetcher("caiso-outlook", min_interval=0.35)
    written = 0
    try:
        done = _existing_days(connection, "caiso_demand") if skip_existing else set()
        today = today_pacific()
        for day in daterange(start, end):
            if day in done and day != today:
                continue
            for slug, series in caiso_outlook.SERIES.items():
                try:
                    frame = caiso_outlook.fetch_day(fetcher, day, slug)
                except NotAvailable as exc:
                    log.warning("%s", exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad day must not stop a backfill
                    log.warning("caiso %s %s failed: %s", slug, day, exc)
                    continue
                written += store.upsert(connection, series.table, frame)
            log.info("caiso outlook %s done", day)
    finally:
        fetcher.close()
    return written


def ingest_weather(
    connection: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    location_keys: list[str],
    *,
    include_previous_runs: bool = True,
) -> int:
    """Archive actuals, the live forecast, and the archived past forecasts."""
    fetcher = Fetcher("open-meteo", min_interval=0.6)
    written = 0
    try:
        for key in location_keys:
            place = get_location(key)

            archive_end = min(end, today_pacific() - timedelta(days=ERA5_LAG_DAYS))
            cursor = start
            while cursor <= archive_end:
                stop = min(cursor + timedelta(days=ARCHIVE_CHUNK_DAYS - 1), archive_end)
                frame = openmeteo.fetch_archive(fetcher, place, cursor, stop)
                written += store.upsert(connection, "weather_actual", frame)
                log.info("weather archive %s %s -> %s", key, cursor, stop)
                cursor = stop + timedelta(days=1)

            recent, ahead = openmeteo.fetch_forecast(fetcher, place)
            written += store.upsert(connection, "weather_actual", recent)
            written += store.upsert(connection, "weather_forecast", ahead)

            if include_previous_runs:
                span = min((end - start).days or 1, MAX_PREVIOUS_RUN_DAYS)
                errors = openmeteo.fetch_previous_runs(fetcher, place, past_days=span)
                written += store.upsert(connection, "weather_forecast_error", errors)
            log.info("weather %s done", key)
    finally:
        fetcher.close()
    return written


def ingest_oasis(connection: duckdb.DuckDBPyConnection, days_ahead: int = 7) -> int:
    """The forward load forecast -- the one thing Today's Outlook cannot give us."""
    fetcher = Fetcher("caiso-oasis", min_interval=5.0)
    try:
        today = today_pacific()
        frame = caiso_oasis.fetch_load_forecast(
            fetcher, today, today + timedelta(days=days_ahead), market_run_id="7DA"
        )
        return store.upsert(connection, "caiso_load_forecast", frame)
    except Exception as exc:  # noqa: BLE001 - OASIS is flaky; never fail the run over it
        log.warning("oasis forecast unavailable: %s", exc)
        return 0
    finally:
        fetcher.close()


def ingest_eia(connection: duckdb.DuckDBPyConnection, start: date, end: date) -> int:
    if not EIA_API_KEY:
        log.info("no EIA_API_KEY set; skipping deep-history backbone")
        return 0
    fetcher = Fetcher("eia", min_interval=0.4)
    try:
        frame = eia.fetch_region_data(fetcher, EIA_API_KEY, start, end)
        return store.upsert(connection, "eia_demand", frame)
    except Exception as exc:  # noqa: BLE001
        log.warning("eia ingest failed: %s", exc)
        return 0
    finally:
        fetcher.close()


def backfill(
    connection: duckdb.DuckDBPyConnection,
    days: int = 730,
    location_keys: list[str] | None = None,
    *,
    skip_existing: bool = True,
    with_oasis: bool = True,
) -> dict[str, int]:
    """One-time historical load. Safe to re-run; the disk cache makes it cheap."""
    location_keys = location_keys or [DEFAULT_LOCATION]
    today = today_pacific()
    start = max(today - timedelta(days=days), parse_date(CAISO_OUTLOOK_EARLIEST))

    log.info("backfill %s -> %s for %s", start, today, ", ".join(location_keys))
    written = {
        "caiso_outlook": ingest_caiso_outlook(
            connection, start, today, skip_existing=skip_existing
        ),
        "weather": ingest_weather(connection, start, today, location_keys),
        "eia": ingest_eia(connection, start, today),
    }
    if with_oasis:
        written["oasis"] = ingest_oasis(connection)
    return written


def update(
    connection: duckdb.DuckDBPyConnection,
    location_keys: list[str] | None = None,
    lookback_days: int = 2,
) -> dict[str, int]:
    """The scheduled path: recent actuals plus a fresh forward view."""
    location_keys = location_keys or list(LOCATIONS)
    today = today_pacific()
    start = today - timedelta(days=lookback_days)

    written = {
        # skip_existing=False: CAISO revises recent intervals after publication.
        "caiso_outlook": ingest_caiso_outlook(connection, start, today, skip_existing=False),
        "weather": ingest_weather(
            connection, start, today, location_keys, include_previous_runs=True
        ),
        "oasis": ingest_oasis(connection),
        "eia": ingest_eia(connection, start - timedelta(days=5), today),
    }
    return written
