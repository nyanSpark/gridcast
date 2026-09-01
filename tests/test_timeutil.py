"""The DST cases, which is the whole reason this module exists separately."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from gridcast.timeutil import (
    PACIFIC,
    UTC,
    daterange,
    expected_intervals,
    floor_utc,
    is_ambiguous,
    is_nonexistent,
    pacific_day_bounds,
    pacific_wall_to_utc,
    parse_utc,
)

SPRING_FORWARD = date(2025, 3, 9)   # 02:00-02:59 PT never happens
FALL_BACK = date(2025, 11, 2)       # 01:00-01:59 PT happens twice
ORDINARY = date(2025, 7, 15)


def test_ordinary_day_has_288_five_minute_intervals():
    assert expected_intervals(ORDINARY) == 288


def test_spring_forward_day_is_an_hour_short():
    assert expected_intervals(SPRING_FORWARD) == 276


def test_fall_back_day_is_an_hour_long():
    assert expected_intervals(FALL_BACK) == 300


@pytest.mark.parametrize("minute", [0, 5, 30, 55])
def test_spring_forward_gap_has_no_utc_instant(minute):
    assert pacific_wall_to_utc(SPRING_FORWARD, 2, minute) is None


def test_times_either_side_of_the_spring_gap_are_one_interval_apart():
    before = pacific_wall_to_utc(SPRING_FORWARD, 1, 55)
    after = pacific_wall_to_utc(SPRING_FORWARD, 3, 0)
    assert after - before == timedelta(minutes=5)


def test_fall_back_hour_is_ambiguous_and_resolves_to_the_first_pass():
    local = datetime(2025, 11, 2, 1, 30, tzinfo=PACIFIC)
    assert is_ambiguous(local)
    # fold=0 is PDT (UTC-7), so 01:30 PT -> 08:30 UTC, not 09:30.
    assert pacific_wall_to_utc(FALL_BACK, 1, 30) == datetime(2025, 11, 2, 8, 30, tzinfo=UTC)


def test_fall_back_leaves_a_visible_gap_rather_than_inventing_data():
    """CAISO collapses the repeated hour, so 01:55 -> 02:00 jumps 65 minutes.

    Recording that honestly is the point: the alternative is silently
    duplicating an hour of load onto the wrong instants.
    """
    before = pacific_wall_to_utc(FALL_BACK, 1, 55)
    after = pacific_wall_to_utc(FALL_BACK, 2, 0)
    assert after - before == timedelta(minutes=65)


def test_ordinary_times_are_neither_ambiguous_nor_nonexistent():
    local = datetime(2025, 7, 15, 2, 30, tzinfo=PACIFIC)
    assert not is_ambiguous(local)
    assert not is_nonexistent(local)


def test_pacific_day_bounds_are_utc_and_half_open():
    start, end = pacific_day_bounds(ORDINARY)
    assert start == datetime(2025, 7, 15, 7, 0, tzinfo=UTC)
    assert end == datetime(2025, 7, 16, 7, 0, tzinfo=UTC)


def test_daterange_is_inclusive_on_both_ends():
    days = list(daterange(date(2025, 1, 1), date(2025, 1, 3)))
    assert days == [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)]


@pytest.mark.parametrize(
    "text",
    ["2025-07-15T12:00:00Z", "2025-07-15T12:00:00+00:00", "2025-07-15T12:00:00"],
)
def test_parse_utc_accepts_the_shapes_we_actually_receive(text):
    assert parse_utc(text) == datetime(2025, 7, 15, 12, 0, tzinfo=UTC)


def test_floor_utc_snaps_down_to_the_bucket():
    stamp = datetime(2025, 7, 15, 12, 37, 42, tzinfo=UTC)
    assert floor_utc(stamp, 5) == datetime(2025, 7, 15, 12, 35, tzinfo=UTC)
    assert floor_utc(stamp, 60) == datetime(2025, 7, 15, 12, 0, tzinfo=UTC)
