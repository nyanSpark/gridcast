"""CAISO "Today's Outlook" CSV feeds.

These are the undocumented endpoints the caiso.com/todays-outlook page itself
fetches. They are the single richest free source for this project because a
demand row already contains the actual **and** both forecasts:

    Time,Day ahead forecast,Hour ahead forecast,Current demand,Demand response
    00:00,32655,31647,31446,

5-minute resolution, and ``history/YYYYMMDD/`` works back to mid-2018.

Three upstream quirks this module absorbs, all verified against live responses:

1. **289 rows, always.** The file runs 00:00 -> 23:55 plus a trailing 00:00 that
   belongs to the *next* Pacific day. We drop it; the next day's file supplies it.
2. **Spring forward:** the grid stays a fixed 288 wall-clock slots, so 02:00-02:55
   are present but blank. ``pacific_wall_to_utc`` returns ``None`` for them.
3. **Fall back:** the repeated 01:00-01:59 hour is *collapsed* upstream -- CAISO
   publishes one set of values, not two. So one real hour is missing from the
   feed on that day each year. We resolve the label to the first (PDT) pass and
   record nothing for the PST repeat, rather than inventing data.

Being undocumented, the schema also drifts: ``Demand response`` only appears
from around 2023. Missing columns are backfilled as null rather than raising.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date

import httpx
import pandas as pd

from ..config import CAISO_OUTLOOK_BASE
from ..http import Fetcher, NotAvailable
from ..timeutil import pacific_wall_to_utc, today_pacific

log = logging.getLogger("gridcast.caiso.outlook")


@dataclass(frozen=True)
class OutlookSeries:
    slug: str
    table: str
    columns: dict[str, str]


SERIES: dict[str, OutlookSeries] = {
    "demand": OutlookSeries(
        slug="demand",
        table="caiso_demand",
        columns={
            "Day ahead forecast": "day_ahead_forecast",
            "Hour ahead forecast": "hour_ahead_forecast",
            "Current demand": "demand",
            "Demand response": "demand_response",
        },
    ),
    "netdemand": OutlookSeries(
        slug="netdemand",
        table="caiso_netdemand",
        columns={
            "Net demand": "net_demand",
            "Net demand forecast": "net_demand_forecast",
        },
    ),
    "fuelsource": OutlookSeries(
        slug="fuelsource",
        table="caiso_fuelmix",
        columns={
            "Solar": "solar",
            "Wind": "wind",
            "Geothermal": "geothermal",
            "Biomass": "biomass",
            "Biogas": "biogas",
            "Small hydro": "small_hydro",
            "Coal": "coal",
            "Nuclear": "nuclear",
            "Natural Gas": "natural_gas",
            "Large Hydro": "large_hydro",
            "Batteries": "batteries",
            "Imports": "imports",
            "Other": "other",
        },
    ),
    "co2": OutlookSeries(
        slug="co2",
        table="caiso_co2",
        columns={
            "Biogas CO2": "biogas_co2",
            "Biomass CO2": "biomass_co2",
            "Natural Gas CO2": "natural_gas_co2",
            "Coal CO2": "coal_co2",
            "Imports CO2": "imports_co2",
            "Geothermal CO2": "geothermal_co2",
        },
    ),
}


def url_for(slug: str, day: date | None) -> str:
    """``day=None`` means the live file for the current Pacific day."""
    if day is None:
        return f"{CAISO_OUTLOOK_BASE}/current/{slug}.csv"
    return f"{CAISO_OUTLOOK_BASE}/history/{day:%Y%m%d}/{slug}.csv"


def parse_csv(text: str, day: date, series: OutlookSeries) -> pd.DataFrame:
    """Parse one Today's Outlook CSV into a UTC-indexed frame.

    Pure function -- no network -- so the DST cases are unit-testable.
    """
    frame = pd.read_csv(io.StringIO(text))
    frame.columns = [str(c).strip() for c in frame.columns]
    if "Time" not in frame.columns:
        raise ValueError(f"unexpected CAISO schema for {series.slug}: {list(frame.columns)}")

    # Quirk 1: drop the trailing midnight row belonging to the next Pacific day.
    midnights = [i for i, label in enumerate(frame["Time"]) if str(label).strip() == "00:00"]
    if len(midnights) > 1:
        frame = frame.iloc[: midnights[1]]

    frame = frame.rename(columns=series.columns)
    out_columns = list(series.columns.values())
    for column in out_columns:
        # Quirk: schema drift. Absent columns become null, not an exception.
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    timestamps: list[object] = []
    keep: list[int] = []
    for position, label in enumerate(frame["Time"]):
        hour_text, _, minute_text = str(label).strip().partition(":")
        instant = pacific_wall_to_utc(day, int(hour_text), int(minute_text))
        if instant is None:  # Quirk 2: spring-forward gap
            continue
        timestamps.append(instant)
        keep.append(position)

    frame = frame.iloc[keep].copy()
    frame["ts_utc"] = pd.to_datetime(pd.Series(timestamps, index=frame.index), utc=True)

    # Rows past "now" in the live file are blank for actuals but populated for
    # forecasts, so only drop rows that are empty across the board.
    frame = frame.dropna(subset=out_columns, how="all")
    return frame[["ts_utc", *out_columns]].reset_index(drop=True)


def fetch_day(fetcher: Fetcher, day: date, slug: str = "demand") -> pd.DataFrame:
    """Fetch one Pacific day. Past days are cached on disk; today never is."""
    series = SERIES[slug]
    today = today_pacific()
    if day > today:
        raise NotAvailable(f"CAISO Today's Outlook has no data for future day {day}")
    is_past = day < today
    try:
        text = fetcher.get_text(url_for(slug, day if is_past else None), cache=is_past)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise NotAvailable(f"CAISO has no {slug} for {day}") from exc
        raise
    return parse_csv(text, day, series)
