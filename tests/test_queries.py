from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from gridcast import ranges
from gridcast.queries import columnar, pick_bucket

UTC = timezone.utc
START = datetime(2025, 7, 15, tzinfo=UTC)


def test_bucket_widens_with_the_requested_span():
    assert pick_bucket(START, START + timedelta(days=1)) == "5 minutes"
    assert pick_bucket(START, START + timedelta(days=20)) == "1 hour"
    assert pick_bucket(START, START + timedelta(days=120)) == "6 hours"
    assert pick_bucket(START, START + timedelta(days=3000)) == "1 day"


BUCKET_SECONDS = {"5 minutes": 300, "1 hour": 3600, "6 hours": 21600, "1 day": 86400}


def points_for(days: int) -> float:
    end = START + timedelta(days=days)
    return (end - START).total_seconds() / BUCKET_SECONDS[pick_bucket(START, end)]


def test_bucketing_holds_windows_up_to_a_year_near_2500_points():
    """The point of adaptive bucketing: the browser gets a comparable number of
    points whether the window is a day or a year."""
    for days in [1, 8, 45, 400]:
        assert points_for(days) <= 2500


def test_the_widest_window_degrades_gracefully_rather_than_exploding():
    """Past a year the bucket floors at one day, so point count grows linearly
    -- but CAISO's whole archive is only ~8 years, which stays manageable."""
    assert points_for(365 * 8) <= 3000


def test_columnar_replaces_missing_values_with_none_so_json_stays_valid():
    frame = pd.DataFrame({"a": [1.0, np.nan], "b": ["x", None]})
    assert columnar(frame) == {"a": [1.0, None], "b": ["x", None]}


def test_columnar_emits_utc_iso_timestamps():
    frame = pd.DataFrame({"t": pd.to_datetime(["2025-07-15T12:00:00Z", None], utc=True)})
    assert columnar(frame)["t"] == ["2025-07-15T12:00:00Z", None]


def test_presets_resolve_to_ordered_windows():
    for key in ranges.PRESETS:
        start, end = ranges.resolve(key)
        assert start < end


def test_short_presets_do_not_get_a_disproportionate_forecast_tail():
    """A 3-day view should stay at 5-minute resolution, not be coarsened just
    to make room for a week of forward forecast."""
    start, end = ranges.resolve("3d")
    assert pick_bucket(start, end) == "5 minutes"


def test_columnar_rounds_floats_to_keep_the_committed_export_small():
    frame = pd.DataFrame({"mw": [30123.456789, 0.001234]})
    assert columnar(frame)["mw"] == [30123.46, 0.0]


def test_columnar_leaves_integers_exact():
    frame = pd.DataFrame({"hour": pd.Series([0, 13, 23], dtype="int64")})
    assert columnar(frame)["hour"] == [0, 13, 23]
