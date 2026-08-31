"""One HTTP session for every source, with an on-disk cache.

The cache is what lets a plan be regenerated on a boat with no signal: a value
that was fetched at the dock is reused, and if the network is down every cached
response is served regardless of age (with ``stale=True`` on the result so the
caller can stamp the PDF).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .. import config

_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        _session = s
    return _session


@dataclass
class Fetched:
    """A response body plus where it came from."""

    text: str
    url: str
    from_cache: bool
    stale: bool
    fetched_at: float

    def json(self) -> Any:
        return json.loads(self.text)


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return config.CACHE_DIR / f"{h}.json"


def _read_cache(path: Path) -> tuple[str, float] | None:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return blob["body"], float(blob["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None


def _write_cache(path: Path, url: str, body: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"url": url, "fetched_at": time.time(), "body": body}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a cache we cannot write is not fatal


def fetch(url: str, *, ttl: float = config.CACHE_TTL_FORECAST_S,
          force: bool = False, timeout: int | None = None) -> Fetched:
    """GET ``url`` as text, via the cache.

    Fresh cache (age < ttl) is returned without a request. On a network error a
    stale cache entry is returned if one exists; otherwise the error propagates.
    """
    path = _cache_path(url)
    cached = _read_cache(path)
    now = time.time()

    if cached and not force and (now - cached[1]) < ttl:
        return Fetched(cached[0], url, from_cache=True, stale=False, fetched_at=cached[1])

    last_err: Exception | None = None
    for attempt in range(config.HTTP_RETRIES):
        try:
            r = session().get(url, timeout=timeout or config.HTTP_TIMEOUT)
            r.raise_for_status()
            _write_cache(path, url, r.text)
            return Fetched(r.text, url, from_cache=False, stale=False, fetched_at=now)
        except requests.RequestException as e:
            last_err = e
            if attempt < config.HTTP_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))

    if cached:
        return Fetched(cached[0], url, from_cache=True, stale=True, fetched_at=cached[1])
    raise RuntimeError(f"fetch failed and no cache: {url}\n  {last_err}")


def fetch_json(url: str, **kw) -> tuple[Any, Fetched]:
    f = fetch(url, **kw)
    return f.json(), f
