"""Shared range presets.

The API takes arbitrary ``start``/``end``, but the static export has to
pre-render a fixed set of windows. Defining them once means the frontend can
address either backend with the same vocabulary.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .timeutil import now_utc

#: key -> (label, lookback). ``None`` means "everything we have".
PRESETS: dict[str, tuple[str, timedelta | None]] = {
    "3d": ("3 days", timedelta(days=3)),
    "14d": ("2 weeks", timedelta(days=14)),
    "90d": ("3 months", timedelta(days=90)),
    "1y": ("1 year", timedelta(days=365)),
    "all": ("All", None),
}
DEFAULT_PRESET = "14d"
#: How far past "now" the forward forecasts are worth showing.
FORWARD_WINDOW = timedelta(days=7)


def resolve(
    key: str, earliest: datetime | None = None, latest: datetime | None = None
) -> tuple[datetime, datetime]:
    """Turn a preset key into a concrete ``[start, end)`` in UTC."""
    if key not in PRESETS:
        raise KeyError(f"unknown range {key!r}; known: {', '.join(PRESETS)}")
    _, lookback = PRESETS[key]
    # Scale the forward window to the lookback, so a 3-day view does not get
    # bucketed coarsely just to make room for a week of forecast.
    forward = FORWARD_WINDOW if lookback is None else min(FORWARD_WINDOW, lookback)
    end = (latest or now_utc()) + forward
    if lookback is None:
        start = earliest or (now_utc() - timedelta(days=365 * 10))
    else:
        start = now_utc() - lookback
        if earliest is not None:
            start = max(start, earliest)
    return start, end
