# Deploying gridcast

gridcast is built to ship two ways from the same codebase. Pick based on one
question: **do you need arbitrary date ranges, or are preset windows enough?**

| | Static (Vercel, Pages) | Dynamic (Render, Fly, Railway) |
|---|---|---|
| Cost | Free, permanently | Free tier with caveats |
| Setup | ~5 minutes | ~15 minutes |
| Cold starts | None | 30–60s on free tiers |
| Date ranges | 5 presets | Anything |
| Zoom behaviour | Pans within the loaded window | Refetches at a finer bucket |
| Freshness | Cron interval (3h default) | Cron interval |
| Backend to maintain | None | A server |

**Recommendation: start static on Vercel.** It is the easiest thing to get running,
it never sleeps, and for a portfolio project the preset windows are almost always
enough. You can add the dynamic deployment later without touching the frontend — it
detects which backend it is talking to at load time.

---

## Option 1 — Vercel, static (recommended)

The trick that makes this work: **there is no backend, and no build step.**
`gridcast export` pre-renders every chart payload to JSON in `web/data/`, and
`vercel.json` tells Vercel to serve `web/` as-is.

### One-time setup

1. Generate the data and commit it:

```bash
make backfill && gridcast snapshot && git add -A && git commit -m "seed data"
```

2. Push to GitHub.

3. At [vercel.com/new](https://vercel.com/new), import the repo and deploy. Take every
   default — `vercel.json` already sets the output directory and cache headers. There
   is nothing to configure.

### Keeping it fresh

`.github/workflows/update.yml` already does this. Every three hours it fetches new
data, re-renders `web/data/`, and commits — and that push triggers a Vercel redeploy
automatically. Enable Actions on the repo and it runs itself.

Optionally add an `EIA_API_KEY` repository secret for the deep-history backbone. The
workflow succeeds without it.

### Watch out for

**Repo growth.** Each run commits refreshed Parquet and JSON, which sounds alarming
-- a full snapshot is ~9 MB. In practice git delta-compresses consecutive snapshots
very well: measured over six commits, the entire `.git` packed to 6.3 MB, *less than
one* working-tree copy, and a refresh carrying genuinely new data cost ~156 KB packed.
At the default 3-hour cadence that is roughly 1 MB/day, so a few hundred MB after a
year. Comfortable, but not free. If it ever becomes a problem:

- move the Parquet snapshots to [Git LFS](https://git-lfs.com/), or
- drop the `gridcast snapshot` step from the cron job and run it weekly instead —
  only `web/data/` needs to be fresh for the site, or
- publish snapshots as release assets rather than committing them.

**Adding locations multiplies the export.** Files are written per location per preset,
so six locations is 6× the JSON. Restrict the cron job with `-l los-angeles` if you
only chart one.

---

## Option 2 — GitHub Pages, static

Same static output, no third-party account. Add this workflow and enable Pages
(Settings → Pages → Source: GitHub Actions):

```yaml
name: pages
on:
  push:
    branches: [main]
permissions:
  contents: read
  pages: write
  id-token: write
concurrency:
  group: pages
jobs:
  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: web
      - id: deploy
        uses: actions/deploy-pages@v4
```

The frontend uses relative paths throughout, so it works from a project subpath
(`you.github.io/gridcast/`) without configuration.

---

## Option 3 — Render / Fly.io / Railway, dynamic

Use this when you want arbitrary date ranges and zoom-to-refetch. The included
`Dockerfile` bakes the DuckDB store from the committed Parquet at build time, so the
container serves reads without needing a writable volume.

### Render

Point a new Web Service at the repo. Render detects the `Dockerfile`; leave the start
command empty (the image sets it) and set the health check path to `/api/health`.
Free instances sleep after ~15 minutes idle, so the first visitor waits ~50 seconds.

### Fly.io

```bash
fly launch --no-deploy
```

```bash
fly deploy
```

Set `internal_port = 8000` in `fly.toml`. Fly's free allowance keeps a small machine
running without sleeping, which makes it the better pick if cold starts bother you.

### Refreshing data on the dynamic path

The container's store is baked at build time and is not writable. Keep the same
GitHub Action — it commits Parquet, and the push triggers a rebuild. If you would
rather ingest inside the container, mount a volume at `/app/data` and run
`gridcast update` from a scheduled job.

---

## What about Vercel serverless functions?

You can run FastAPI on Vercel via a Python function, but it fights the design in two
places: the filesystem is read-only apart from `/tmp` and is discarded between
invocations, so the ingest cannot run there and DuckDB gets no durable home; and every
cold start pays the DuckDB import. Since `gridcast export` already produces exactly
what the frontend needs, the static path gives you the same site with less to go wrong.

---

## Bandwidth and cost

Measured, gzipped, as a host actually serves it:

| | Transfer |
|---|---|
| Plotly from CDN (cached ~1 year after the first visit) | 347 KB |
| App shell: HTML + CSS + JS | 10 KB |
| `meta.json` + the default chart payload | 10 KB |
| **First visit, total** | **~367 KB** |
| **Repeat visit** (shell and Plotly cached) | **~10 KB** |
| Each extra tab or window click | 9-63 KB |

Two things keep this small. Plotly is loaded from `cdn.plot.ly`, so it never touches
your hosting bandwidth at all *and* is cached across visits — and it is the
`plotly-basic` build (347 KB) rather than the full one (1.3 MB), because the app only
uses three trace types. Second, chart payloads are columnar and rounded to two
decimals, so even a full year of hourly data is 63 KB on the wire.

Practically: the bytes billed to your host are ~20 KB on a first visit and ~10 KB
after. Vercel's free tier has historically included 100 GB/month (check current
terms) — at those sizes you would need on the order of a million visits to approach
it. This will not cost you money.

## Does the database grow on Vercel?

**No — there is no database on Vercel.** This is the part worth understanding.

In the static deployment there is no DuckDB, no server, and no per-visitor state. The
build is a folder of files: one HTML page, one CSS file, one JS file, and 41 JSON
files. A visitor downloads the JSON for the view they are looking at, exactly the way
they would download an image. Nothing is written anywhere.

What grows is **your Git repository**, on your own schedule, because the scheduled job
commits refreshed Parquet and JSON every three hours. That is disk in your GitHub
repo, not a live database, and it grows the same amount whether one person visits or a
million do. Traffic has no effect on storage.

Visitors keep nothing beyond ordinary browser HTTP cache — the same cache that holds
any image or script, cleared whenever they clear their browser. gridcast sets no
cookies, uses no `localStorage`, and stores no per-user state.

If you deploy the *dynamic* version instead (Render/Fly), there is a DuckDB file, but
it is baked into the container image at build time and mounted read-only. It answers
queries; it never grows from traffic.

---

## Local production check

Verify the static build before you push — this serves `web/` with no API at all, so
the frontend takes its fallback path exactly as it will in production:

```bash
gridcast export && python -m http.server 8080 --directory web
```

The status dot in the header reads **static snapshot** rather than **live API** when
the fallback is active.
