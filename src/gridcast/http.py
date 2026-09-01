"""One polite HTTP client for every source.

Three things every scraper of a public feed owes its upstream, and that a
reviewer will look for:

* **Rate limiting.** OASIS in particular will 429 you into a corner. Each
  source declares its own minimum spacing.
* **Retries with backoff**, honouring ``Retry-After``.
* **A disk cache for immutable responses.** A finished historical day never
  changes, so a re-run of a two-year backfill should hit the network zero
  times. This turns an interrupted backfill from a problem into a no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .config import CACHE_DIR, USER_AGENT

log = logging.getLogger("gridcast.http")


class Fetcher:
    """A rate-limited, retrying, optionally-caching HTTP GET wrapper."""

    def __init__(
        self,
        name: str,
        min_interval: float = 0.5,
        retries: int = 4,
        timeout: float = 60.0,
        backoff: float = 2.0,
    ) -> None:
        self.name = name
        self.min_interval = min_interval
        self.retries = retries
        self.backoff = backoff
        self._last_call = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )

    # -- lifecycle ------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ------------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _cache_path(self, url: str, params: dict[str, Any] | None) -> Path:
        blob = url + "?" + json.dumps(params or {}, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode()).hexdigest()[:24]
        return CACHE_DIR / self.name / f"{digest}.bin"

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        cache: bool = False,
    ) -> bytes:
        """GET with retries. Set ``cache=True`` only for immutable responses."""
        path = self._cache_path(url, params)
        if cache and path.exists():
            return path.read_bytes()

        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:  # transport-level: worth retrying
                last_error = exc
                self._sleep_for_attempt(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else None
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} from {url}",
                    request=response.request,
                    response=response,
                )
                log.warning("%s: %s, retrying", self.name, response.status_code)
                self._sleep_for_attempt(attempt, delay)
                continue

            response.raise_for_status()
            if cache:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(response.content)
            return response.content

        assert last_error is not None
        raise last_error

    def _sleep_for_attempt(self, attempt: int, override: float | None = None) -> None:
        time.sleep(override if override is not None else self.backoff**attempt)

    # -- typed helpers --------------------------------------------------------
    def get_text(
        self, url: str, params: dict[str, Any] | None = None, *, cache: bool = False
    ) -> str:
        return self.get(url, params, cache=cache).decode("utf-8-sig")

    def get_json(
        self, url: str, params: dict[str, Any] | None = None, *, cache: bool = False
    ) -> Any:
        return json.loads(self.get(url, params, cache=cache))


class NotAvailable(Exception):
    """Upstream has no data for this request (a 404 on a historical day, say)."""
