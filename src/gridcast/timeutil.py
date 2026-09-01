"""Pacific <-> UTC conversion, isolated and tested.

Everything gridcast stores is UTC. Pacific wall-clock exists only at the two
edges: CAISO publishes bare ``HH:MM`` labels on a fixed 24-hour Pacific grid,
and the UI renders in ``America/Los_Angeles``.

The two DST transition days each year are where projects like this quietly
corrupt their joins -- a naive ``date + HH:MM`` parse produces a duplicated or
a phantom hour, and every downstream average is then wrong for that day. All of
that logic lives here so it can be tested directly (see tests/test_timeutil.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def _wall(dt: datetime) -> datetime:
    """The naive wall-clock components, ignoring offset."""
    return dt.replace(tzinfo=None)


def is_nonexistent(local_dt: datetime) -> bool:
    """True for wall-clock times skipped by spring-forward (02:00-02:59 PT).

    Detected by round-tripping through UTC: a skipped time comes back as a
    different wall time, because no instant maps to it.
    """
    round_trip = local_dt.astimezone(UTC).astimezone(PACIFIC)
    return _wall(round_trip) != _wall(local_dt)


def is_ambiguous(local_dt: datetime) -> bool:
    """True for wall-clock times that happen twice on fall-back day (01:00-01:59 PT)."""
    return local_dt.replace(fold=0).utcoffset() != local_dt.replace(fold=1).utcoffset()


def pacific_wall_to_utc(day: date, hour: int, minute: int) -> datetime | None:
    """Convert a CAISO ``HH:MM`` label on a Pacific date to an aware UTC instant.

    Returns ``None`` for the spring-forward gap, which CAISO emits as rows with
    empty values so that its CSV is always a fixed 288-interval grid. Ambiguous
    fall-back times resolve to ``fold=0`` -- the first (PDT) pass. CAISO only
    publishes one set of values for that hour, so the PST repeat is genuinely
    absent from the upstream feed rather than being dropped here.
    """
    local = datetime.combine(day, time(hour, minute), tzinfo=PACIFIC)
    if is_nonexistent(local):
        return None
    return local.astimezone(UTC)


def pacific_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a Pacific calendar day as aware UTC instants."""
    start = datetime.combine(day, time(0, 0), tzinfo=PACIFIC).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=PACIFIC).astimezone(UTC)
    return start, end


def expected_intervals(day: date, minutes: int = 5) -> int:
    """Real intervals in a Pacific day: 288 normally, 276 or 300 on DST days."""
    start, end = pacific_day_bounds(day)
    return int((end - start).total_seconds() // (minutes * 60))


def today_pacific() -> date:
    return datetime.now(UTC).astimezone(PACIFIC).date()


def now_utc() -> datetime:
    return datetime.now(UTC)


def daterange(start: date, end: date) -> Iterator[date]:
    """Inclusive on both ends."""
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 string to aware UTC, treating naive input as UTC."""
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def floor_utc(dt: datetime, minutes: int) -> datetime:
    """Floor an instant to a whole number of minutes past the hour."""
    dt = dt.astimezone(UTC)
    discard = timedelta(minutes=dt.minute % minutes, seconds=dt.second, microseconds=dt.microsecond)
    return dt - discard
