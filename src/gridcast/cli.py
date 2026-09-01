"""gridcast command line."""

from __future__ import annotations

import logging

import typer

from . import export as export_module
from . import pipeline, store
from .config import DB_PATH, DEFAULT_LOCATION, LOCATIONS

app = typer.Typer(add_completion=False, help="California grid demand vs. weather.")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _locations(values: list[str] | None) -> list[str]:
    if not values:
        return [DEFAULT_LOCATION]
    for value in values:
        if value not in LOCATIONS:
            raise typer.BadParameter(f"unknown location {value!r}; known: {', '.join(LOCATIONS)}")
    return list(values)


@app.command()
def init(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Create the database, restoring any committed Parquet snapshots."""
    _setup_logging(verbose)
    with store.session() as connection:
        loaded = store.restore(connection)
    if loaded:
        for name, count in loaded.items():
            typer.echo(f"  restored {name}: {count:,} rows")
    else:
        typer.echo("  no Parquet snapshots found -- run `gridcast backfill` next")
    typer.echo(f"database ready at {DB_PATH}")


@app.command()
def backfill(
    days: int = typer.Option(730, help="How far back to go (capped at CAISO's mid-2018 start)."),
    location: list[str] | None = typer.Option(None, "--location", "-l"),
    skip_existing: bool = typer.Option(True, help="Skip Pacific days already fully stored."),
    oasis: bool = typer.Option(True, help="Also pull the OASIS 7-day-ahead load forecast."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """One-time historical load. Idempotent and disk-cached, so re-runs are cheap."""
    _setup_logging(verbose)
    with store.session() as connection:
        written = pipeline.backfill(
            connection,
            days=days,
            location_keys=_locations(location),
            skip_existing=skip_existing,
            with_oasis=oasis,
        )
        for name, count in written.items():
            typer.echo(f"  {name}: {count:,} rows")
        store.snapshot(connection)
    typer.echo("backfill complete")


@app.command()
def update(
    location: list[str] | None = typer.Option(None, "--location", "-l"),
    lookback_days: int = typer.Option(2, help="CAISO revises recent intervals; re-read them."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Incremental refresh. This is what the scheduled job runs."""
    _setup_logging(verbose)
    with store.session() as connection:
        written = pipeline.update(
            connection,
            location_keys=_locations(location) if location else None,
            lookback_days=lookback_days,
        )
        for name, count in written.items():
            typer.echo(f"  {name}: {count:,} rows")


@app.command()
def export(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Render static JSON into web/data/ for backend-free hosting."""
    _setup_logging(verbose)
    with store.session(read_only=True) as connection:
        written = export_module.export_all(connection)
    total = sum(written.values())
    typer.echo(f"wrote {len(written)} files, {total / 1024:.1f} KB")


@app.command()
def snapshot(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Write Parquet snapshots for committing to git."""
    _setup_logging(verbose)
    with store.session() as connection:
        files = store.snapshot(connection)
    for name in files:
        typer.echo(f"  {name}")


@app.command()
def stats() -> None:
    """Row counts and coverage per table."""
    with store.session(read_only=True) as connection:
        rows = store.table_stats(connection)
    width = max(len(row["table"]) for row in rows)
    for row in rows:
        span = f"{row['start']} -> {row['end']}" if row["rows"] else "empty"
        typer.echo(f"  {row['table']:<{width}}  {row['rows']:>9,}  {span}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the API and frontend together."""
    import uvicorn

    uvicorn.run("gridcast.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
