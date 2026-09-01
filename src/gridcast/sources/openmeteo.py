"""Open-Meteo: free, keyless, and the only weather source that closes the loop.

Three of its APIs map exactly onto gridcast's three time horizons:

* **Archive (ERA5)** -- what actually happened, back to 1940. Roughly a 5-day
  publication lag, so it owns everything older than a week.
* **Forecast** -- the next 16 days, plus ``past_days`` to bridge the archive lag.
* **Previous Runs** -- for each valid hour, what the model predicted 1..7 days
  *earlier*. This is the piece that makes "how accurate has forecasting been"
  answerable for weather the same way CAISO's own day-ahead column makes it
  answerable for load, and it is what lets the two error series be compared.

Everything is requested in SI with ``timezone=UTC``; imperial conversion happens
in the browser. Storing SI and presenting local keeps the DST handling in one
place (``timeutil``) instead of smeared across the pipeline.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..config import (
    OPEN_METEO_ARCHIVE,
    OPEN_METEO_FORECAST,
    OPEN_METEO_PREVIOUS_RUNS,
    Location,
)
from ..http import Fetcher
from ..timeutil import now_utc

log = logging.getLogger("gridcast.openmeteo")

#: Not just temperature. ``shortwave_radiation`` and ``wind_speed_100m`` are the
#: variables that actually drive the solar and wind columns of CAISO's fuel mix,
#: and hub-height wind (100m) is far more predictive than the 10m surface value.
HOURLY_VARIABLES = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "dew_point_2m",
    "cloud_cover",
    "shortwave_radiation",
    "direct_normal_irradiance",
    "wind_speed_10m",
    "wind_speed_100m",
    "precipitation",
]

BASE_PARAMS = {
    "timezone": "UTC",
    "temperature_unit": "celsius",
    "wind_speed_unit": "ms",
    "precipitation_unit": "mm",
}

MAX_LEAD_DAYS = 7


def _to_frame(payload: dict, location: Location, columns: list[str]) -> pd.DataFrame:
    hourly = payload.get("hourly") or {}
    if not hourly.get("time"):
        return pd.DataFrame(columns=["location", "ts_utc", *columns])

    frame = pd.DataFrame({"ts_utc": pd.to_datetime(hourly["time"], utc=True)})
    for column in columns:
        values = hourly.get(column)
        frame[column] = pd.to_numeric(pd.Series(values), errors="coerce") if values else pd.NA
    frame.insert(0, "location", location.key)
    return frame


def fetch_archive(fetcher: Fetcher, location: Location, start: date, end: date) -> pd.DataFrame:
    """ERA5 reanalysis actuals. Immutable once published, so safe to disk-cache."""
    payload = fetcher.get_json(
        OPEN_METEO_ARCHIVE,
        {
            **BASE_PARAMS,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(HOURLY_VARIABLES),
        },
        cache=end < now_utc().date(),
    )
    frame = _to_frame(payload, location, HOURLY_VARIABLES)
    frame["source"] = "era5"
    return frame


def fetch_forecast(
    fetcher: Fetcher, location: Location, past_days: int = 7, forecast_days: int = 16
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(recent_actual, forecast)`` split at the current hour.

    The ``past_days`` window bridges the ERA5 publication lag. It is model
    analysis rather than reanalysis, which is a small discontinuity against the
    archive -- ``source`` records which is which so a chart can say so.
    """
    payload = fetcher.get_json(
        OPEN_METEO_FORECAST,
        {
            **BASE_PARAMS,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "past_days": past_days,
            "forecast_days": forecast_days,
            "hourly": ",".join(HOURLY_VARIABLES),
        },
    )
    frame = _to_frame(payload, location, HOURLY_VARIABLES)
    if frame.empty:
        return frame, frame

    boundary = now_utc().replace(minute=0, second=0, microsecond=0)
    recent = frame[frame["ts_utc"] < boundary].copy()
    ahead = frame[frame["ts_utc"] >= boundary].copy()
    recent["source"] = "forecast_analysis"
    ahead["run_time_utc"] = boundary
    return recent, ahead


def fetch_previous_runs(
    fetcher: Fetcher,
    location: Location,
    past_days: int = 60,
    variables: tuple[str, ...] = ("temperature_2m", "shortwave_radiation"),
) -> pd.DataFrame:
    """What the model said N days ahead, for N in 1..7.

    Returns long-form ``(location, ts_utc, lead_days, variable, predicted)`` so
    that adding a variable or a lead time does not change the schema.
    """
    requested: list[str] = []
    for variable in variables:
        requested.append(variable)
        requested.extend(f"{variable}_previous_day{lead}" for lead in range(1, MAX_LEAD_DAYS + 1))

    payload = fetcher.get_json(
        OPEN_METEO_PREVIOUS_RUNS,
        {
            **BASE_PARAMS,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "past_days": past_days,
            "forecast_days": 1,
            "hourly": ",".join(requested),
        },
    )
    hourly = payload.get("hourly") or {}
    if not hourly.get("time"):
        return pd.DataFrame(
            columns=["location", "ts_utc", "lead_days", "variable", "predicted", "actual"]
        )

    times = pd.to_datetime(hourly["time"], utc=True)
    records: list[pd.DataFrame] = []
    for variable in variables:
        actual = pd.to_numeric(pd.Series(hourly.get(variable)), errors="coerce")
        for lead in range(1, MAX_LEAD_DAYS + 1):
            values = hourly.get(f"{variable}_previous_day{lead}")
            if not values:
                continue
            block = pd.DataFrame(
                {
                    "location": location.key,
                    "ts_utc": times,
                    "lead_days": lead,
                    "variable": variable,
                    "predicted": pd.to_numeric(pd.Series(values), errors="coerce"),
                    "actual": actual,
                }
            )
            records.append(block.dropna(subset=["predicted", "actual"], how="any"))

    if not records:
        return pd.DataFrame(
            columns=["location", "ts_utc", "lead_days", "variable", "predicted", "actual"]
        )
    return pd.concat(records, ignore_index=True)
