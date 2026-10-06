"""HTTP helpers.

Each helper handles one request; `pipeline.download_tiles` runs `download` in
parallel. Retries use a linear backoff, except for stalled downloads, which
restart immediately on a fresh connection.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import (ADMIN_LAYER, DOWNLOAD_MIN_SPEED, DOWNLOAD_RETRIES, DOWNLOAD_STALL_SECONDS,
                     HTTP_RETRIES, HTTP_TIMEOUT, LAMBERT93, USER_AGENT, WFS)


class HttpError(RuntimeError):
    pass


class _Stalled(Exception):
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
    """Content-Length for a URL; None when the server says it is not there.

    The download endpoint answers 400 for a tile probed in the wrong block and
    404 for an unknown path: both mean "not here". Anything else that survives
    the retries raises HttpError, so an outage is never mistaken for a missing
    tile. HTTP 429 is waited out separately from the retries.
    """
    throttled = 0
    attempt = 0
    last: Exception | None = None
    while attempt < retries:
        attempt += 1
        try:
            with urllib.request.urlopen(_request(url, "HEAD"), timeout=timeout) as r:
                length = r.headers.get("Content-Length")
                return int(length) if length else None
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            if e.code == 429 and throttled < 6:
                throttled += 1
                attempt -= 1
                wait = e.headers.get("Retry-After", "")
                time.sleep(int(wait) if wait.isdigit() else 5)
                continue
            last = e
        except Exception as e:                       # noqa: BLE001
            last = e
        if attempt < retries:
            time.sleep(attempt * 2)
    raise HttpError(f"HEAD failed after {retries} attempts: {url} ({last})")


def download(url: str, dest: Path, expected: int | None = None,
             retries: int = DOWNLOAD_RETRIES, chunk: int = 1 << 20, *,
             on_progress: Callable[[int, int | None], None] | None = None,
             on_retry: Callable[[str], None] | None = None,
             should_stop: Callable[[], bool] | None = None) -> int:
    """Download to `dest`, returning the byte count.

    Writes to a .part file and renames on success, so an interrupted run never
    leaves a short file that looks complete. `on_progress(received, total)` reports
    cumulative bytes at most every 0.1 s, plus each attempt's start and end;
    total is None when the size is unknown.

    A connection slower than DOWNLOAD_MIN_SPEED for DOWNLOAD_STALL_SECONDS is
    dropped and retried at once. The last attempt accepts any speed, so a
    genuinely slow line still finishes. A retry asks for the missing bytes with
    a Range request and falls back to zero if the server sends the whole file.
    `on_retry(reason)` is called before each retry. `should_stop` is polled
    between chunks; a stop removes the .part file and raises InterruptedError.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    part.unlink(missing_ok=True)                    # never resume another run's bytes
    last: Exception | None = None

    for attempt in range(1, retries + 1):
        offset = part.stat().st_size if part.exists() else 0
        if expected is not None and offset >= expected:
            offset = 0                              # nothing left to ask for
        total = offset
        size = expected
        last_report = window_start = time.monotonic()
        window_bytes = total
        try:
            if should_stop and should_stop():
                raise InterruptedError("Download cancelled")
            if on_progress and not offset:
                on_progress(0, size)
            request = _request(url)
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as r:
                resumed = bool(offset) and getattr(r, "status", 200) == 206 and \
                    (r.headers.get("Content-Range") or "").startswith(f"bytes {offset}-")
                if offset and not resumed:
                    total = window_bytes = 0
                    if on_progress:
                        on_progress(0, size)
                if size is None:
                    length = r.headers.get("Content-Length")
                    if length is not None and str(length).isdigit():
                        size = int(length) + total
                        if on_progress:
                            on_progress(total, size)
                with open(part, "ab" if resumed else "wb") as fh:
                    while True:
                        buf = r.read(chunk)
                        if not buf:
                            break
                        fh.write(buf)
                        total += len(buf)
                        now = time.monotonic()
                        if on_progress and now - last_report >= 0.1:
                            on_progress(total, size)
                            last_report = now
                        if should_stop and should_stop():
                            raise InterruptedError("Download cancelled")
                        if now - window_start >= DOWNLOAD_STALL_SECONDS:
                            if attempt < retries and \
                                    total - window_bytes < DOWNLOAD_MIN_SPEED * (now - window_start):
                                raise _Stalled(f"slow connection at {total} bytes")
                            window_start, window_bytes = now, total

            if expected is not None and total != expected:
                raise HttpError(f"size mismatch: got {total}, expected {expected}")

            part.replace(dest)
            if on_progress:
                on_progress(total, size)
            return total
        except InterruptedError:
            part.unlink(missing_ok=True)
            raise
        except Exception as e:                       # noqa: BLE001
            last = e
            if on_progress:
                on_progress(total, size)
            # Keep a short .part to resume from; drop one that cannot be trusted.
            if isinstance(e, urllib.error.HTTPError) or (expected is not None and total > expected):
                part.unlink(missing_ok=True)
            if attempt < retries:
                if on_retry:
                    on_retry(str(e))
                if not isinstance(e, _Stalled):
                    time.sleep(attempt * 5)

    part.unlink(missing_ok=True)
    raise HttpError(f"download failed after {retries} attempts: {url} ({last})")


def wfs_url(layer: str, *, cql: str | None = None, count: int | None = None,
            properties: str | None = None) -> str:
    """Build a WFS GetFeature URL against the Géoplateforme."""
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": f"{ADMIN_LAYER}:{layer}",
        "OUTPUTFORMAT": "application/json",
        "SRSNAME": f"EPSG:{LAMBERT93}",
    }
    if cql:
        params["CQL_FILTER"] = cql
    if count:
        params["COUNT"] = str(count)
    if properties:
        params["PROPERTYNAME"] = properties
    return f"{WFS}?{urllib.parse.urlencode(params)}"
