# The dynamic deployment: FastAPI + DuckDB, for Render / Fly.io / Railway.
# For static hosting (Vercel, GitHub Pages) you do not need this at all --
# `gridcast export` writes everything the site needs. See DEPLOY.md.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 GRIDCAST_DATA_DIR=/app/data

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY web ./web
COPY data/parquet ./data/parquet

# Bake the DuckDB store from the committed Parquet so the container starts
# with data and never needs a writable volume just to serve reads.
RUN gridcast init

EXPOSE 8000
CMD ["sh", "-c", "uvicorn gridcast.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
