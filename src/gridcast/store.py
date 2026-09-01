"""DuckDB storage layer.

Why DuckDB: a single file, no server, columnar, and genuinely good at the one
query shape this project makes constantly -- bucket a few hundred thousand rows
by time and average them. At 5-minute resolution eight years of demand is about
840k rows, which it handles without complaint.

Why Parquet alongside it: the ``.duckdb`` file is a build artifact and is
gitignored. ``snapshot()`` writes one Parquet file per table into
``data/parquet/``, and those *are* committed -- so a clone of the repo carries
its data and ``gridcast init`` rebuilds the database offline. It also keeps the
scheduled job's commits to a readable set of columnar files rather than a
churning binary blob.

Writes are idempotent. Every table has a natural primary key and every load is
``INSERT OR REPLACE``, so re-running a backfill over a range that is already
present is a no-op rather than a source of duplicates.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import duckdb
import pandas as pd

from .config import DB_PATH, PARQUET_DIR, ensure_dirs

log = logging.getLogger("gridcast.store")

TIMESTAMP = "TIMESTAMPTZ"
VALUE = "DOUBLE"
TEXT = "VARCHAR"


@dataclass(frozen=True)
class Table:
    name: str
    columns: dict[str, str]
    primary_key: tuple[str, ...]

    @property
    def value_columns(self) -> list[str]:
        return [c for c in self.columns if c not in self.primary_key]

    def ddl(self) -> str:
        body = ",\n    ".join(f"{name} {kind}" for name, kind in self.columns.items())
        pk = ", ".join(self.primary_key)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {body},\n    PRIMARY KEY ({pk})\n)"


_WEATHER_VARIABLES = [
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


def _weather_columns(extra: dict[str, str]) -> dict[str, str]:
    columns: dict[str, str] = {"location": TEXT, "ts_utc": TIMESTAMP}
    columns.update({name: VALUE for name in _WEATHER_VARIABLES})
    columns.update(extra)
    return columns


TABLES: dict[str, Table] = {
    table.name: table
    for table in [
        Table(
            "caiso_demand",
            {
                "ts_utc": TIMESTAMP,
                "day_ahead_forecast": VALUE,
                "hour_ahead_forecast": VALUE,
                "demand": VALUE,
                "demand_response": VALUE,
            },
            ("ts_utc",),
        ),
        Table(
            "caiso_netdemand",
            {"ts_utc": TIMESTAMP, "net_demand": VALUE, "net_demand_forecast": VALUE},
            ("ts_utc",),
        ),
        Table(
            "caiso_fuelmix",
            {
                "ts_utc": TIMESTAMP,
                **{
                    name: VALUE
                    for name in [
                        "solar", "wind", "geothermal", "biomass", "biogas", "small_hydro",
                        "coal", "nuclear", "natural_gas", "large_hydro", "batteries",
                        "imports", "other",
                    ]
                },
            },
            ("ts_utc",),
        ),
        Table(
            "caiso_co2",
            {
                "ts_utc": TIMESTAMP,
                **{
                    name: VALUE
                    for name in [
                        "biogas_co2", "biomass_co2", "natural_gas_co2",
                        "coal_co2", "imports_co2", "geothermal_co2",
                    ]
                },
            },
            ("ts_utc",),
        ),
        Table(
            "caiso_load_forecast",
            {"ts_utc": TIMESTAMP, "market_run_id": TEXT, "forecast_mw": VALUE},
            ("ts_utc", "market_run_id"),
        ),
        Table(
            "eia_demand",
            {
                "ts_utc": TIMESTAMP,
                "demand": VALUE,
                "day_ahead_forecast": VALUE,
                "net_generation": VALUE,
                "total_interchange": VALUE,
            },
            ("ts_utc",),
        ),
        Table("weather_actual", _weather_columns({"source": TEXT}), ("location", "ts_utc")),
        Table(
            "weather_forecast",
            _weather_columns({"run_time_utc": TIMESTAMP}),
            ("location", "ts_utc", "run_time_utc"),
        ),
        Table(
            "weather_forecast_error",
            {
                "location": TEXT,
                "ts_utc": TIMESTAMP,
                "lead_days": "INTEGER",
                "variable": TEXT,
                "predicted": VALUE,
                "actual": VALUE,
            },
            ("location", "ts_utc", "lead_days", "variable"),
        ),
    ]
}


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    ensure_dirs()
    if read_only and not DB_PATH.exists():
        # A read-only connection cannot create the file, so make it first.
        init(duckdb.connect(str(DB_PATH)))
    connection = duckdb.connect(str(DB_PATH), read_only=read_only)
    if not read_only:
        init(connection)
    return connection


@contextmanager
def session(read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()


def init(connection: duckdb.DuckDBPyConnection) -> None:
    for table in TABLES.values():
        connection.execute(table.ddl())


def _align(frame: pd.DataFrame, table: Table) -> pd.DataFrame:
    """Reindex to the declared schema and coerce dtypes.

    Guards against upstream schema drift in both directions: a column CAISO
    stopped sending becomes null, and one it started sending that we do not
    model is dropped rather than breaking the insert.
    """
    aligned = pd.DataFrame(index=frame.index)
    for name, kind in table.columns.items():
        if name in frame.columns:
            column = frame[name]
        else:
            column = pd.Series(pd.NA, index=frame.index)
        if kind == VALUE:
            aligned[name] = pd.to_numeric(column, errors="coerce").astype("float64")
        elif kind == "INTEGER":
            aligned[name] = pd.to_numeric(column, errors="coerce").astype("Int64")
        elif kind == TIMESTAMP:
            aligned[name] = pd.to_datetime(column, utc=True, errors="coerce")
        else:
            aligned[name] = column.astype("string")
    return aligned.dropna(subset=list(table.primary_key))


def upsert(connection: duckdb.DuckDBPyConnection, table_name: str, frame: pd.DataFrame) -> int:
    """Idempotent load. Returns the number of rows written."""
    table = TABLES[table_name]
    if frame is None or frame.empty:
        return 0

    aligned = _align(frame, table)
    # Last write wins within a single batch too, so a caller can hand us
    # overlapping chunks without deduplicating first.
    aligned = aligned.drop_duplicates(subset=list(table.primary_key), keep="last")
    if aligned.empty:
        return 0

    columns = ", ".join(table.columns)
    connection.register("_stage", aligned)
    try:
        connection.execute(
            f"INSERT OR REPLACE INTO {table_name} ({columns}) SELECT {columns} FROM _stage"
        )
    finally:
        connection.unregister("_stage")
    log.debug("upsert %s: %s rows", table_name, len(aligned))
    return len(aligned)


def table_stats(connection: duckdb.DuckDBPyConnection) -> list[dict]:
    stats: list[dict] = []
    for name in TABLES:
        row = connection.execute(
            f"SELECT count(*), min(ts_utc), max(ts_utc) FROM {name}"
        ).fetchone()
        stats.append(
            {
                "table": name,
                "rows": int(row[0] or 0),
                "start": row[1].isoformat() if row[1] else None,
                "end": row[2].isoformat() if row[2] else None,
            }
        )
    return stats


def snapshot(connection: duckdb.DuckDBPyConnection) -> list[str]:
    """Write each non-empty table to ``data/parquet/`` for committing to git."""
    ensure_dirs()
    written: list[str] = []
    for name in TABLES:
        count = connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        if not count:
            continue
        path = PARQUET_DIR / f"{name}.parquet"
        connection.execute(
            f"COPY (SELECT * FROM {name} ORDER BY 1) TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        written.append(path.name)
    return written


def restore(connection: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Rebuild the database from committed Parquet, so a fresh clone has data."""
    loaded: dict[str, int] = {}
    for name in TABLES:
        path = PARQUET_DIR / f"{name}.parquet"
        if not path.exists():
            continue
        columns = ", ".join(TABLES[name].columns)
        connection.execute(
            f"INSERT OR REPLACE INTO {name} ({columns}) "
            f"SELECT {columns} FROM read_parquet('{path}')"
        )
        loaded[name] = connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
    return loaded
