"""EIA Open Data API v2 -- the deep-history backbone.

Today's Outlook only reaches mid-2018; EIA's Form-930 hourly series reaches
2015-07-01, is documented, versioned and stable, and carries the same
demand-vs-day-ahead-forecast pair. It is the boring source, in the good way.

Optional: gridcast runs fully without it. A free key is instant and self-serve
at https://www.eia.gov/opendata/.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..config import EIA_BASE
from ..http import Fetcher

log = logging.getLogger("gridcast.eia")

PAGE_SIZE = 5000

#: EIA's type codes for a balancing authority.
TYPES = {
    "D": "demand",
    "DF": "day_ahead_forecast",
    "NG": "net_generation",
    "TI": "total_interchange",
}


def fetch_region_data(
    fetcher: Fetcher,
    api_key: str,
    start: date,
    end: date,
    respondent: str = "CISO",
) -> pd.DataFrame:
    """Hourly demand / forecast / generation / interchange, pivoted wide.

    EIA caps a page at 5000 rows and reports ``response.total``, so we page
    until we have them all rather than trusting a single request.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        payload = fetcher.get_json(
            EIA_BASE,
            {
                "api_key": api_key,
                "frequency": "hourly",
                "data[0]": "value",
                "facets[respondent][]": respondent,
                "facets[type][]": list(TYPES),
                "start": f"{start.isoformat()}T00",
                "end": f"{end.isoformat()}T23",
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
                "offset": offset,
                "length": PAGE_SIZE,
            },
        )
        response = payload.get("response", {})
        page = response.get("data", [])
        rows.extend(page)
        total = int(response.get("total", 0))
        offset += PAGE_SIZE
        log.info("eia: %s/%s rows", min(offset, total), total)
        if len(page) < PAGE_SIZE or offset >= total:
            break

    if not rows:
        return pd.DataFrame(columns=["ts_utc", *TYPES.values()])

    frame = pd.DataFrame(rows)
    # EIA periods look like "2015-07-01T07" and are always UTC.
    frame["ts_utc"] = pd.to_datetime(frame["period"], format="%Y-%m-%dT%H", utc=True)
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["column"] = frame["type"].map(TYPES)

    wide = (
        frame.pivot_table(index="ts_utc", columns="column", values="value", aggfunc="last")
        .reset_index()
        .rename_axis(None, axis=1)
    )
    for column in TYPES.values():
        if column not in wide.columns:
            wide[column] = pd.NA
    return wide[["ts_utc", *TYPES.values()]]
