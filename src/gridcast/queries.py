"""Read-side queries: adaptive downsampling and the four chart payloads.

The single most important thing here is ``pick_bucket``. The store keeps
5-minute resolution, but shipping 5-minute data for a multi-year window means
~840k points per series to the browser, which no plotting library survives.
Instead the bucket is chosen from the requested span, so the payload stays a
few thousand points whether the user is looking at one day or eight years, and
zooming in genuinely fetches finer data rather than re-rendering the same
points. That is what makes the range slider feel like it is doing something.

Payloads are **columnar** -- ``{"t": [...], "demand": [...]}`` rather than a
list of row objects. For a time series this is roughly a third of the bytes and
is the shape Plotly wants anyway.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import duckdb
import pandas as pd

from .config import DEFAULT_LOCATION

PACIFIC_TZ = "America/Los_Angeles"

#: (maximum span, DuckDB interval). First match wins.
#: Thresholds are chosen so every bucket yields roughly 1-2.5k points per
#: series -- dense enough to look continuous, light enough to stay interactive.
BUCKETS: list[tuple[timedelta | None, str]] = [
    (timedelta(days=8), "5 minutes"),    # <= ~2300 points
    (timedelta(days=45), "1 hour"),      # <= ~1080 points
    (timedelta(days=400), "6 hours"),    # <= ~1600 points
    (None, "1 day"),
]


def pick_bucket(start: datetime, end: datetime) -> str:
    span = end - start
    for limit, interval in BUCKETS:
        if limit is None or span <= limit:
            return interval
    return "1 day"


#: Two decimals is below the measurement precision of every series here
#: (megawatts, degrees, W/m2, percent) and roughly halves the JSON, which
#: matters because the static export is committed on every scheduled run.
ROUND_DP = 2


def columnar(frame: pd.DataFrame) -> dict[str, list[Any]]:
    """DataFrame -> JSON-safe columnar dict. NaN/NaT become null."""
    payload: dict[str, list[Any]] = {}
    for name in frame.columns:
        column = frame[name]
        if pd.api.types.is_datetime64_any_dtype(column):
            values = column.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            payload[name] = [None if pd.isna(v) else str(v) for v in values]
        elif pd.api.types.is_float_dtype(column):
            payload[name] = [None if pd.isna(v) else round(float(v), ROUND_DP) for v in column]
        else:
            payload[name] = [
                None if pd.isna(v) else v.item() if hasattr(v, "item") else v for v in column
            ]
    return payload


def _bucketed(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    bucket: str,
    start: datetime,
    end: datetime,
    where: str = "",
    params: list[Any] | None = None,
) -> pd.DataFrame:
    aggregates = ", ".join(f"avg({name}) AS {name}" for name in columns)
    sql = f"""
        SELECT time_bucket(INTERVAL '{bucket}', ts_utc) AS t, {aggregates}
        FROM {table}
        WHERE ts_utc >= ? AND ts_utc < ? {where}
        GROUP BY 1 ORDER BY 1
    """
    return connection.execute(sql, [start, end, *(params or [])]).fetch_df()


# -- meta ---------------------------------------------------------------------
def data_range(connection: duckdb.DuckDBPyConnection) -> tuple[datetime | None, datetime | None]:
    row = connection.execute(
        """
        SELECT min(t), max(t) FROM (
            SELECT min(ts_utc) AS t FROM caiso_demand
            UNION ALL SELECT max(ts_utc) FROM caiso_demand
            UNION ALL SELECT min(ts_utc) FROM eia_demand
            UNION ALL SELECT max(ts_utc) FROM eia_demand
        )
        """
    ).fetchone()
    return row[0], row[1]


# -- 1. the timeline ----------------------------------------------------------
def series(
    connection: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    location: str = DEFAULT_LOCATION,
) -> dict[str, Any]:
    """Demand and weather on a shared time axis, plus the forward forecasts.

    Demand and weather are returned as *separate* arrays with their own time
    axes rather than joined onto one grid. Demand is 5-minute and weather is
    hourly; joining them would mean either inventing interpolated weather or
    throwing away demand resolution. Plotly is happy to draw traces with
    different x arrays, so neither compromise is necessary.
    """
    bucket = pick_bucket(start, end)

    grid = _bucketed(
        connection,
        "caiso_demand",
        ["demand", "day_ahead_forecast", "hour_ahead_forecast"],
        bucket,
        start,
        end,
    )
    net = _bucketed(connection, "caiso_netdemand", ["net_demand"], bucket, start, end)
    if not net.empty:
        grid = grid.merge(net, on="t", how="outer").sort_values("t")

    weather = _bucketed(
        connection,
        "weather_actual",
        ["temperature_2m", "shortwave_radiation", "wind_speed_100m", "cloud_cover"],
        bucket if bucket != "5 minutes" else "1 hour",
        start,
        end,
        where="AND location = ?",
        params=[location],
    )

    # Forward halves: the most recent forecast run only.
    ahead = connection.execute(
        """
        SELECT ts_utc AS t, temperature_2m
        FROM weather_forecast
        WHERE location = ? AND ts_utc >= ? AND ts_utc < ?
          AND run_time_utc = (SELECT max(run_time_utc) FROM weather_forecast WHERE location = ?)
        ORDER BY 1
        """,
        [location, start, end, location],
    ).fetch_df()

    load_ahead = connection.execute(
        """
        SELECT ts_utc AS t, forecast_mw
        FROM caiso_load_forecast
        WHERE ts_utc >= ? AND ts_utc < ? AND market_run_id = '7DA'
        ORDER BY 1
        """,
        [start, end],
    ).fetch_df()

    return {
        "bucket": bucket,
        "location": location,
        "grid": columnar(grid),
        "weather": columnar(weather),
        "weather_forecast": columnar(ahead),
        "load_forecast": columnar(load_ahead),
    }


# -- 2. the response curve ----------------------------------------------------
def response_curve(
    connection: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    location: str = DEFAULT_LOCATION,
) -> dict[str, Any]:
    """Hourly (temperature, demand) pairs -- the U-shaped load response.

    Aggregated to the hour because that is the weather resolution; carrying
    5-minute demand into a scatter would just be 12 copies of each weather
    point.
    """
    frame = connection.execute(
        f"""
        WITH d AS (
            SELECT time_bucket(INTERVAL '1 hour', ts_utc) AS t, avg(demand) AS demand
            FROM caiso_demand WHERE ts_utc >= ? AND ts_utc < ? GROUP BY 1
        ),
        w AS (
            SELECT time_bucket(INTERVAL '1 hour', ts_utc) AS t,
                   avg(temperature_2m) AS temperature_2m
            FROM weather_actual
            WHERE location = ? AND ts_utc >= ? AND ts_utc < ? GROUP BY 1
        )
        SELECT d.t, d.demand, w.temperature_2m,
               hour(d.t AT TIME ZONE '{PACIFIC_TZ}') AS hour_pt,
               month(d.t AT TIME ZONE '{PACIFIC_TZ}') AS month_pt
        FROM d JOIN w USING (t)
        WHERE d.demand IS NOT NULL AND w.temperature_2m IS NOT NULL
        ORDER BY d.t
        """,
        [start, end, location, start, end],
    ).fetch_df()
    return {"location": location, "points": columnar(frame)}


# -- 3. forecast accuracy -----------------------------------------------------
def forecast_error(
    connection: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    location: str = DEFAULT_LOCATION,
) -> dict[str, Any]:
    """CAISO load-forecast error, weather-forecast error, and the coupling."""
    bucket = pick_bucket(start, end)

    caiso = connection.execute(
        f"""
        SELECT time_bucket(INTERVAL '{bucket}', ts_utc) AS t,
               avg(day_ahead_forecast - demand)                     AS error_mw,
               avg(100.0 * (day_ahead_forecast - demand) / nullif(demand, 0)) AS error_pct
        FROM caiso_demand
        WHERE ts_utc >= ? AND ts_utc < ? AND demand IS NOT NULL AND day_ahead_forecast IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        [start, end],
    ).fetch_df()

    by_hour = connection.execute(
        f"""
        SELECT hour(ts_utc AT TIME ZONE '{PACIFIC_TZ}') AS hour_pt,
               avg(abs(100.0 * (day_ahead_forecast - demand) / nullif(demand, 0))) AS mape,
               avg(100.0 * (day_ahead_forecast - demand) / nullif(demand, 0))      AS bias,
               count(*) AS n
        FROM caiso_demand
        WHERE ts_utc >= ? AND ts_utc < ? AND demand IS NOT NULL AND day_ahead_forecast IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        [start, end],
    ).fetch_df()

    weather_by_lead = connection.execute(
        """
        SELECT lead_days,
               avg(abs(predicted - actual)) AS mae,
               avg(predicted - actual)      AS bias,
               count(*)                     AS n
        FROM weather_forecast_error
        WHERE location = ? AND variable = 'temperature_2m' AND ts_utc >= ? AND ts_utc < ?
        GROUP BY 1 ORDER BY 1
        """,
        [location, start, end],
    ).fetch_df()

    coupling = connection.execute(
        f"""
        WITH load_err AS (
            SELECT date_trunc('day', ts_utc AT TIME ZONE '{PACIFIC_TZ}') AS day_pt,
                   avg(abs(100.0 * (day_ahead_forecast - demand) / nullif(demand, 0))) AS load_mape,
                   max(demand) AS peak_mw
            FROM caiso_demand
            WHERE ts_utc >= ? AND ts_utc < ?
              AND demand IS NOT NULL AND day_ahead_forecast IS NOT NULL
            GROUP BY 1
        ),
        temp_err AS (
            SELECT date_trunc('day', ts_utc AT TIME ZONE '{PACIFIC_TZ}') AS day_pt,
                   avg(abs(predicted - actual)) AS temp_mae_c
            FROM weather_forecast_error
            WHERE location = ? AND variable = 'temperature_2m' AND lead_days = 1
              AND ts_utc >= ? AND ts_utc < ?
            GROUP BY 1
        )
        SELECT load_err.day_pt AS day, load_mape, peak_mw, temp_mae_c
        FROM load_err JOIN temp_err USING (day_pt)
        ORDER BY 1
        """,
        [start, end, location, start, end],
    ).fetch_df()

    return {
        "bucket": bucket,
        "location": location,
        "caiso": columnar(caiso),
        "by_hour": columnar(by_hour),
        "weather_by_lead": columnar(weather_by_lead),
        "coupling": columnar(coupling),
    }


# -- 4. fuel mix --------------------------------------------------------------
FUEL_COLUMNS = [
    "solar", "wind", "natural_gas", "large_hydro", "nuclear",
    "imports", "batteries", "geothermal", "small_hydro", "biomass", "biogas", "coal",
]


def fuel_mix(
    connection: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    location: str = DEFAULT_LOCATION,
) -> dict[str, Any]:
    """Generation by source, with the irradiance that should explain solar."""
    bucket = pick_bucket(start, end)
    mix = _bucketed(connection, "caiso_fuelmix", FUEL_COLUMNS, bucket, start, end)
    weather = _bucketed(
        connection,
        "weather_actual",
        ["shortwave_radiation", "wind_speed_100m"],
        bucket if bucket != "5 minutes" else "1 hour",
        start,
        end,
        where="AND location = ?",
        params=[location],
    )
    return {
        "bucket": bucket,
        "location": location,
        "fuels": FUEL_COLUMNS,
        "mix": columnar(mix),
        "weather": columnar(weather),
    }
