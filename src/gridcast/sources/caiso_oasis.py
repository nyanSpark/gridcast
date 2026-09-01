"""CAISO OASIS -- the official, documented, keyless API.

Today's Outlook cannot see past the current day, so OASIS is what supplies the
*forward* half of the picture: ``SLD_FCST`` with ``market_run_id=7DA`` is the
seven-day-ahead system load forecast. It also carries locational prices.

The interface is unfriendly in three specific ways, all handled here:

* Responses are a **zip containing a CSV**, and on an empty/invalid query the
  zip contains an XML fault document instead.
* Most reports cap a single query at **31 days**, so ranges are chunked.
* It throttles hard. The default spacing here is deliberately slow (5s); do not
  lower it.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd

from ..config import CAISO_OASIS_URL
from ..http import Fetcher, NotAvailable
from ..timeutil import UTC

log = logging.getLogger("gridcast.caiso.oasis")

MAX_QUERY_DAYS = 30
SYSTEM_AREA = "CA ISO-TAC"

MARKET_RUNS = {
    "DAM": "day_ahead",
    "2DA": "two_day_ahead",
    "7DA": "seven_day_ahead",
    "ACTUAL": "actual",
}


def _oasis_stamp(day: date) -> str:
    """OASIS wants ``YYYYMMDDTHH:MM-0000`` in GMT."""
    return f"{day:%Y%m%d}T00:00-0000"


def _read_zip(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if not names:
            raise NotAvailable("OASIS returned an empty archive")
        name = names[0]
        if name.lower().endswith(".xml"):
            # OASIS signals "no data" / "invalid request" with an XML fault.
            raise NotAvailable(f"OASIS returned a fault document: {name}")
        with archive.open(name) as handle:
            return pd.read_csv(handle)


def fetch_load_forecast(
    fetcher: Fetcher,
    start: date,
    end: date,
    market_run_id: str = "7DA",
) -> pd.DataFrame:
    """System-wide load forecast for ``[start, end)``, chunked to OASIS limits."""
    if market_run_id not in MARKET_RUNS:
        raise ValueError(f"market_run_id must be one of {list(MARKET_RUNS)}")

    chunks: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=MAX_QUERY_DAYS), end)
        log.info("oasis: SLD_FCST %s %s -> %s", market_run_id, cursor, stop)
        payload = fetcher.get(
            CAISO_OASIS_URL,
            {
                "queryname": "SLD_FCST",
                "market_run_id": market_run_id,
                "startdatetime": _oasis_stamp(cursor),
                "enddatetime": _oasis_stamp(stop),
                "version": 1,
                "resultformat": 6,  # CSV
            },
            cache=stop < datetime.now(UTC).date(),
        )
        try:
            chunks.append(_read_zip(payload))
        except NotAvailable as exc:
            log.warning("oasis: %s", exc)
        cursor = stop

    if not chunks:
        return pd.DataFrame(columns=["ts_utc", "market_run_id", "forecast_mw"])

    frame = pd.concat(chunks, ignore_index=True)
    frame = frame[frame["TAC_AREA_NAME"] == SYSTEM_AREA]
    frame["ts_utc"] = pd.to_datetime(frame["INTERVALSTARTTIME_GMT"], utc=True, format="mixed")
    frame["forecast_mw"] = pd.to_numeric(frame["MW"], errors="coerce")
    frame["market_run_id"] = market_run_id

    return (
        frame[["ts_utc", "market_run_id", "forecast_mw"]]
        .dropna(subset=["forecast_mw"])
        .drop_duplicates(subset=["ts_utc", "market_run_id"], keep="last")
        .sort_values("ts_utc")
        .reset_index(drop=True)
    )
