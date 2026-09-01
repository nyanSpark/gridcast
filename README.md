# gridcast

**California grid demand versus local weather data visualizer**

> **Live demo:** Deployment URL Pending — see [DEPLOY.md](DEPLOY.md).
> 
---

## Quickstart

Requires **Python 3.10+**. [uv](https://docs.astral.sh/uv/) is used if present
(`make setup` runs `uv sync` against the committed lockfile); otherwise it falls back
to `venv` + `pip`.

```bash
make setup
```

```bash
make backfill
```

```bash
make serve
```

Then open <http://127.0.0.1:8000>.
The backfill pulls two years of CAISO history and weather; the first run takes roughly ten minutes, and re-runs are near-instant as every immutable response is cached.

**No API keys are required.** CAISO and Open-Meteo are both keyless. An optional free
[EIA key](https://www.eia.gov/opendata/) in `.env` extends history back to 2015.

To preview a static host, with no API running at all:

```bash
make static
```

---

## What it shows

| View | Description |
|---|---|
| **Timeline** | Demand, day-ahead and hour-ahead forecasts, and temperature on one scrubable axis — past, present, and seven days forward. |
| **Load response** | Every hour as a point on a temperature-vs-demand scatter, coloured by hour of day. The V-shaped response, directly. |
| **Forecast accuracy** | Load forecast error over time, which hours CAISO struggles with, how weather skill decays with lead time, and the coupling between the two. |
| **Fuel mix** | Generation by source, with solar irradiance overlaid on the solar band. |

---

## Data sources

Every endpoint below was verified live, not taken from documentation.

| Source | Auth | Coverage | Role |
|---|---|---|---|
| [CAISO Today's Outlook](https://www.caiso.com/todays-outlook) | none | 5-minute, back to **mid-2018** | Actuals + day-ahead + hour-ahead, fuel mix, CO₂ |
| [CAISO OASIS](https://oasis.caiso.com) | none | forward 7 days | The seven-day-ahead load forecast |
| [Open-Meteo](https://open-meteo.com/) | none | ERA5 to 1940; forecasts to +16d; past runs to 2021 | All weather, including archived forecasts |
| [EIA API v2](https://www.eia.gov/opendata/) | free key | hourly, back to **2015-07-01** | Optional deep-history backbone |

### The Today's Outlook endpoints

Lacking documentation — they are what the CAISO page fetches for itself:

```bash
curl 'https://www.caiso.com/outlook/current/demand.csv'
```

```bash
curl 'https://www.caiso.com/outlook/history/20250715/fuelsource.csv'
```

Being undocumented, they drift, and `src/gridcast/sources/caiso_outlook.py` absorbs
three specific quirks — a trailing next-day row, the blanked spring-forward hour, and
a `Demand response` column that only appears from ~2023.

---

## Architecture

```
CAISO / Open-Meteo / EIA
        │   sources/*.py     one client per API, each returning a tidy UTC frame
        ▼
    pipeline.py             backfill (once) and update (scheduled), both idempotent
        ▼
    store.py  ──────────►   data/gridcast.duckdb    working store, gitignored
        │                   data/parquet/*.parquet  committed, so a clone has data
        ▼
    queries.py              adaptive time bucketing
        ├──► api.py         FastAPI, arbitrary ranges          → Render / Fly / local
        └──► export.py      static JSON for preset windows     → Vercel / Pages
                                    ▼
                                  web/           one frontend, either backend
```

Key architectural decisions:

**Everything is stored in UTC (obviously)** Pacific wall-clock exists only at the two edges, and
all of the conversion lives in `timeutil.py` behind tests.

**Time bucketing is chosen from the requested span.** Eight years of 5-minute demand
is ~840k points per series, which no plotting library survives. `pick_bucket` keeps
every payload in the low thousands, so zooming in fetches genuinely finer data rather
than re-rendering the same points.

**Writes are idempotent.** Every table has a natural primary key and every load is
`INSERT OR REPLACE`. An interrupted backfill, a double-fired cron, or a re-read of a
day CAISO has since revised all converge to the same state.

**Immutable responses are cached on disk.** A finished historical day never changes,
so a repeated backfill makes zero network calls — which is both faster and the polite
way to treat a free public feed.

---

## Snags to be aware of...

CAISO publishes bare `HH:MM` labels on a fixed Pacific grid. Twice a year that grid
lies, and a naive `date + HH:MM` parse silently corrupts every downstream average for
those days.

Daylight savings consideration:
- **Spring forward** — 02:00–02:55 PT does not exist. CAISO still emits the rows, blank,
  to keep the file at 288 slots. Materialising them invents an hour that never happened.
- **Fall back** — 01:00–01:59 PT happens twice, but CAISO publishes only one set of
  values. One real hour is genuinely absent from the feed. gridcast records the gap
  rather than duplicating the hour onto the wrong instants.

`tests/test_timeutil.py` and `tests/test_caiso_outlook.py` pin both cases.

## Known limits

- **Weather forecast history is capped at 92 days.** Open-Meteo's Previous Runs API
  will not look further back, so the lead-time and coupling charts cover a rolling
  quarter no matter how far the demand backfill reaches. Everything else honours the
  full window. Let the scheduled job run and the archive grows on its own.
- **Today's Outlook starts mid-2018.** Set `EIA_API_KEY` to reach back to 2015-07-01,
  at hourly rather than 5-minute resolution.
- **One hour a year is missing** from CAISO's feed, on the fall-back day. See above.
- **OASIS is flaky.** Ingest logs a warning and continues rather than failing the run;
  the forward forecast is the only thing that goes stale when it does.

---

## Commands

```bash
gridcast init      # create the store, restoring committed Parquet snapshots
gridcast backfill  # one-time history load (--days, --location, repeatable)
gridcast update    # incremental refresh; what the scheduled job runs
gridcast export    # render static JSON into web/data/
gridcast snapshot  # write Parquet for committing
gridcast stats     # row counts and coverage per table
gridcast serve     # API + frontend
```

## Deploying

See **[DEPLOY.md](DEPLOY.md)**. Short version: Vercel, static, about five minutes and
free — the scheduled GitHub Action refreshes the data and the push triggers a redeploy.

## Development

```bash
make test && make lint
```

## Attribution

Weather data by [Open-Meteo.com](https://open-meteo.com/) under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Grid data from the
[California ISO](https://www.caiso.com/) and the
[U.S. Energy Information Administration](https://www.eia.gov/). This project is not
affiliated with or endorsed by any of them. Please keep the built-in rate limits in
place — the CAISO endpoints in particular are a courtesy, not a contract.

MIT licensed.
