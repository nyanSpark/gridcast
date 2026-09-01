"""Parser tests built from the exact shapes caiso.com actually returns."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from gridcast.sources.caiso_outlook import SERIES, parse_csv, url_for
from gridcast.timeutil import UTC

DEMAND = SERIES["demand"]
HEADER = "Time,Day ahead forecast,Hour ahead forecast,Current demand,Demand response"


def build_csv(day: date, blank_hours: tuple[int, ...] = ()) -> str:
    """A full 289-row file: 00:00-23:55 plus the trailing next-day midnight."""
    lines = [HEADER]
    for slot in range(288):
        hour, minute = divmod(slot * 5, 60)
        if hour in blank_hours:
            lines.append(f"{hour:02d}:{minute:02d},,,,")
        else:
            lines.append(f"{hour:02d}:{minute:02d},{30000 + slot},{29000 + slot},{28000 + slot},")
    lines.append("00:00,31000,30000,29000,")  # belongs to the next Pacific day
    return "\n".join(lines) + "\n"


def test_ordinary_day_yields_288_rows_and_drops_trailing_midnight():
    frame = parse_csv(build_csv(date(2025, 7, 15)), date(2025, 7, 15), DEMAND)
    assert len(frame) == 288
    assert frame["ts_utc"].iloc[0] == datetime(2025, 7, 15, 7, 0, tzinfo=UTC)
    assert frame["ts_utc"].iloc[-1] == datetime(2025, 7, 16, 6, 55, tzinfo=UTC)


def test_timestamps_are_strictly_increasing():
    frame = parse_csv(build_csv(date(2025, 7, 15)), date(2025, 7, 15), DEMAND)
    assert frame["ts_utc"].is_monotonic_increasing
    assert frame["ts_utc"].is_unique


def test_spring_forward_drops_the_blank_phantom_hour():
    """CAISO keeps the 288-slot grid and blanks 02:00-02:55. We must not
    materialise those as real intervals."""
    day = date(2025, 3, 9)
    frame = parse_csv(build_csv(day, blank_hours=(2,)), day, DEMAND)
    assert len(frame) == 276
    assert frame["ts_utc"].is_unique
    span = frame["ts_utc"].iloc[-1] - frame["ts_utc"].iloc[0]
    assert span == timedelta(hours=22, minutes=55)  # a 23-hour day


def test_fall_back_day_keeps_288_rows_because_upstream_collapses_the_repeat():
    day = date(2025, 11, 2)
    frame = parse_csv(build_csv(day), day, DEMAND)
    assert len(frame) == 288
    assert frame["ts_utc"].is_unique
    # 288 intervals but a 24h55m span: the extra 55 minutes is the 65-minute
    # jump where the collapsed PST repeat of 01:00-01:59 should have been.
    span = frame["ts_utc"].iloc[-1] - frame["ts_utc"].iloc[0]
    assert span == timedelta(hours=24, minutes=55)


def test_missing_columns_become_null_rather_than_raising():
    """Pre-2023 files have no 'Demand response' column."""
    text = build_csv(date(2022, 1, 1)).replace(",Demand response", "")
    text = "\n".join(line.rstrip(",") for line in text.splitlines())
    frame = parse_csv(text, date(2022, 1, 1), DEMAND)
    assert "demand_response" in frame.columns
    assert frame["demand_response"].isna().all()
    assert frame["demand"].notna().all()


def test_rows_blank_across_every_value_column_are_dropped():
    day = date(2025, 7, 15)
    frame = parse_csv(build_csv(day, blank_hours=(23,)), day, DEMAND)
    assert len(frame) == 276


def test_unexpected_schema_raises_rather_than_guessing():
    with pytest.raises(ValueError, match="unexpected CAISO schema"):
        parse_csv("Foo,Bar\n1,2\n", date(2025, 7, 15), DEMAND)


def test_url_shapes():
    assert url_for("demand", None).endswith("/current/demand.csv")
    assert url_for("demand", date(2025, 7, 15)).endswith("/history/20250715/demand.csv")
