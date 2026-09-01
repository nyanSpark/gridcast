from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd
import pytest

from gridcast import store

UTC = timezone.utc


@pytest.fixture()
def connection():
    con = duckdb.connect(":memory:")
    store.init(con)
    yield con
    con.close()


def demand_frame(start: datetime, rows: int, demand: float = 30000.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": [start + timedelta(minutes=5 * i) for i in range(rows)],
            "day_ahead_forecast": [demand + 100] * rows,
            "hour_ahead_forecast": [demand + 50] * rows,
            "demand": [demand] * rows,
            "demand_response": [None] * rows,
        }
    )


def test_upsert_is_idempotent(connection):
    frame = demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 10)
    store.upsert(connection, "caiso_demand", frame)
    store.upsert(connection, "caiso_demand", frame)
    assert connection.execute("SELECT count(*) FROM caiso_demand").fetchone()[0] == 10


def test_reingesting_a_revised_value_overwrites_rather_than_duplicating(connection):
    """CAISO revises recent intervals after publication, so the update path
    deliberately re-reads days it already has."""
    start = datetime(2025, 7, 15, tzinfo=UTC)
    store.upsert(connection, "caiso_demand", demand_frame(start, 5, demand=30000.0))
    store.upsert(connection, "caiso_demand", demand_frame(start, 5, demand=31234.0))
    rows = connection.execute("SELECT count(*), max(demand) FROM caiso_demand").fetchone()
    assert rows == (5, 31234.0)


def test_duplicate_keys_within_one_batch_resolve_to_the_last(connection):
    frame = pd.concat([
        demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 3, demand=100.0),
        demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 3, demand=200.0),
    ])
    store.upsert(connection, "caiso_demand", frame)
    row = connection.execute("SELECT count(*), max(demand) FROM caiso_demand").fetchone()
    assert row == (3, 200.0)


def test_missing_column_is_stored_as_null(connection):
    frame = demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 3).drop(columns=["demand_response"])
    store.upsert(connection, "caiso_demand", frame)
    assert connection.execute(
        "SELECT count(*) FROM caiso_demand WHERE demand_response IS NULL"
    ).fetchone()[0] == 3


def test_unknown_extra_column_is_ignored_not_fatal(connection):
    """Upstream adding a column must not break ingestion."""
    frame = demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 3)
    frame["some_new_caiso_column"] = 1.0
    assert store.upsert(connection, "caiso_demand", frame) == 3


def test_rows_without_a_primary_key_are_dropped(connection):
    frame = demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 3)
    frame.loc[1, "ts_utc"] = None
    assert store.upsert(connection, "caiso_demand", frame) == 2


def test_empty_frame_is_a_no_op(connection):
    assert store.upsert(connection, "caiso_demand", pd.DataFrame()) == 0


def test_composite_key_tables_keep_one_row_per_market_run(connection):
    stamp = datetime(2025, 7, 15, tzinfo=UTC)
    frame = pd.DataFrame({
        "ts_utc": [stamp, stamp],
        "market_run_id": ["DAM", "7DA"],
        "forecast_mw": [30000.0, 31000.0],
    })
    store.upsert(connection, "caiso_load_forecast", frame)
    assert connection.execute("SELECT count(*) FROM caiso_load_forecast").fetchone()[0] == 2


def test_snapshot_and_restore_round_trip(connection, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PARQUET_DIR", tmp_path)
    store.upsert(connection, "caiso_demand", demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 7))
    assert "caiso_demand.parquet" in store.snapshot(connection)

    fresh = duckdb.connect(":memory:")
    store.init(fresh)
    assert store.restore(fresh)["caiso_demand"] == 7
    fresh.close()


def test_table_stats_reports_coverage(connection):
    store.upsert(connection, "caiso_demand", demand_frame(datetime(2025, 7, 15, tzinfo=UTC), 4))
    stats = {row["table"]: row for row in store.table_stats(connection)}
    assert stats["caiso_demand"]["rows"] == 4
    assert stats["eia_demand"]["rows"] == 0
