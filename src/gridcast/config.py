"""Central configuration: paths, locations, and source endpoints.

Design note: gridcast runs end to end with **zero API keys**. CAISO and
Open-Meteo are both keyless. ``EIA_API_KEY`` is optional and only unlocks the
deep-history backbone (hourly CAISO demand back to 2015-07-01).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("GRIDCAST_DATA_DIR", ROOT / "data")).resolve()
PARQUET_DIR = DATA_DIR / "parquet"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "gridcast.duckdb"
WEB_DIR = ROOT / "web"
WEB_DATA_DIR = WEB_DIR / "data"

USER_AGENT = os.getenv(
    "GRIDCAST_USER_AGENT",
    "gridcast/0.1 (personal research project; +https://github.com/yourname/gridcast)",
)

EIA_API_KEY = os.getenv("EIA_API_KEY") or None

# --- Endpoints ---------------------------------------------------------------
CAISO_OUTLOOK_BASE = "https://www.caiso.com/outlook"
CAISO_OASIS_URL = "https://oasis.caiso.com/oasisapi/SingleZip"
EIA_BASE = "https://api.eia.gov/v2/electricity/rto/region-data/data/"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_HISTORICAL_FORECAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OPEN_METEO_PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"

# CAISO's Today's Outlook history archive starts partway through 2018. Probing
# the boundary: 2018-04-01 -> 404, 2018-07-01 -> 200.
CAISO_OUTLOOK_EARLIEST = "2018-07-01"
# EIA Form-930 hourly data starts here.
EIA_EARLIEST = "2015-07-01"


@dataclass(frozen=True)
class Location:
    """A weather sample point. These are load centres, not just big cities --
    Fresno and Bakersfield drive a disproportionate share of CAISO's summer
    cooling load relative to their population."""

    key: str
    name: str
    latitude: float
    longitude: float


LOCATIONS: dict[str, Location] = {
    loc.key: loc
    for loc in [
        Location("los-angeles", "Los Angeles", 34.0522, -118.2437),
        Location("fresno", "Fresno", 36.7378, -119.7871),
        Location("sacramento", "Sacramento", 38.5816, -121.4944),
        Location("san-francisco", "San Francisco", 37.7749, -122.4194),
        Location("san-diego", "San Diego", 32.7157, -117.1611),
        Location("bakersfield", "Bakersfield", 35.3733, -119.0187),
    ]
}
DEFAULT_LOCATION = "los-angeles"


def location(key: str | None = None) -> Location:
    key = key or DEFAULT_LOCATION
    if key not in LOCATIONS:
        raise KeyError(f"unknown location {key!r}; known: {', '.join(LOCATIONS)}")
    return LOCATIONS[key]


def ensure_dirs() -> None:
    for d in (DATA_DIR, PARQUET_DIR, CACHE_DIR, WEB_DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
