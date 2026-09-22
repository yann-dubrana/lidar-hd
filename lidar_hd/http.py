"""HTTP helpers.

Everything here is sequential on purpose. IGN's Géoplateforme rate-limits
concurrent requests: parallel batches come back empty or 403, which looks like
corruption rather than throttling. Retries use a linear backoff.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import HTTP_RETRIES, HTTP_TIMEOUT, USER_AGENT


class HttpError(RuntimeError):
    pass


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def get_bytes(url: str, timeout: int = HTTP_TIMEOUT, retries: int = HTTP_RETRIES) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(_request(url), timeout=timeout) as r:
                return r.read()
        except Exception as e:                      # noqa: BLE001 - retry anything
            last = e
            if attempt < retries:
                time.sleep(attempt * 3)
    raise HttpError(f"GET failed after {retries} attempts: {url} ({last})")


def get_json(url: str, **kw: Any) -> Any:
    return json.loads(get_bytes(url, **kw).decode("utf-8"))


def head_size(url: str, timeout: int = 20, retries: int = 2) -> int | None:
    """Content-Length for a URL, or None if it cannot be determined."""
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(_request(url, "HEAD"), timeout=timeout) as r:
                length = r.headers.get("Content-Length")
                return int(length) if length else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries:
                return None
            time.sleep(attempt * 2)
        except Exception:                            # noqa: BLE001
            if attempt == retries:
                return None
            time.sleep(attempt * 2)
    return None


def download(url: str, dest: Path, expected: int | None = None,
             retries: int = HTTP_RETRIES, chunk: int = 1 << 20,
             on_chunk=None, *,
             on_progress: Callable[[int, int | None], None] | None = None) -> int:
    """Download to `dest`, returning the byte count.

    Writes to a .part file and renames on success, so an interrupted run never
    leaves a short file that looks complete. `on_chunk(n)` is called with each
    chunk's size for progress reporting. `on_progress(received, total)` reports
    cumulative bytes at most every 0.1 s, plus each attempt's start and end.
    Retries reset received to zero; total is None when the size is unknown.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None

    for attempt in range(1, retries + 1):
        total = 0
        size = expected
        last_report = time.monotonic()
        try:
            if on_progress:
                on_progress(0, size)
            with urllib.request.urlopen(_request(url), timeout=HTTP_TIMEOUT * 5) as r, \
                 open(part, "wb") as fh:
                if size is None:
                    length = r.headers.get("Content-Length")
                    if length is not None and str(length).isdigit():
                        size = int(length)
                        if on_progress:
                            on_progress(0, size)
                while True:
                    buf = r.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    total += len(buf)
                    if on_chunk:
                        on_chunk(len(buf))
                    if on_progress:
                        now = time.monotonic()
                        if now - last_report >= 0.1:
                            on_progress(total, size)
                            last_report = now

            if expected is not None and total != expected:
                part.unlink(missing_ok=True)
                raise HttpError(f"size mismatch: got {total}, expected {expected}")

            part.replace(dest)
            if on_progress:
                on_progress(total, size)
            return total
        except Exception as e:                       # noqa: BLE001
            last = e
            part.unlink(missing_ok=True)
            if on_progress:
                on_progress(total, size)
            if attempt < retries:
                time.sleep(attempt * 5)

    raise HttpError(f"download failed after {retries} attempts: {url} ({last})")


def wfs_url(layer: str, *, cql: str | None = None, count: int | None = None,
            properties: str | None = None, srs: int = 2154) -> str:
    """Build a WFS GetFeature URL against the Géoplateforme."""
    from .config import ADMIN_LAYER, WFS

    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": f"{ADMIN_LAYER}:{layer}",
        "OUTPUTFORMAT": "application/json",
        "SRSNAME": f"EPSG:{srs}",
    }
    if cql:
        params["CQL_FILTER"] = cql
    if count:
        params["COUNT"] = str(count)
    if properties:
        params["PROPERTYNAME"] = properties
    return f"{WFS}?{urllib.parse.urlencode(params)}"
