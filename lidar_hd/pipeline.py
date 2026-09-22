"""The four pipeline stages: download, colourise, convert, upload.

Every stage is resumable. Re-running skips work that is already done, so an
interrupted job continues where it stopped rather than starting over.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .catalog import Catalog
from .config import (COLORIZE_GROWTH, ECEF, LAMBERT93, MEAN_TILE_BYTES,
                     ORTHO_LAYER, ORTHO_PX, TILES3D_GROWTH, WMS)
from .http import HttpError, download, get_bytes

Progress = Callable[[str, int, int, str], None]   # stage, done, total, detail


def _noop(stage: str, done: int, total: int, detail: str = "") -> None:
    pass


# --- estimates -------------------------------------------------------------

@dataclass(frozen=True)
class Estimate:
    tiles: int
    raw_bytes: float
    colorized_bytes: float
    tiles3d_bytes: float

    @property
    def total_bytes(self) -> float:
        return self.raw_bytes + self.colorized_bytes + self.tiles3d_bytes

    @property
    def download_hours(self) -> float:
        # ~16 s/tile observed sequentially against data.geopf.fr.
        return self.tiles * 16 / 3600

    @property
    def process_hours(self) -> float:
        # ~27 s colourise + ~21 s convert, per tile.
        return self.tiles * 48 / 3600


def estimate(n_tiles: int) -> Estimate:
    raw = n_tiles * MEAN_TILE_BYTES
    col = raw * COLORIZE_GROWTH
    return Estimate(n_tiles, raw, col, col * TILES3D_GROWTH)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}".replace(",", " ")
        n /= 1024
    return f"{n:.1f} TB"


# --- stage 1: download -----------------------------------------------------

@dataclass
class StageResult:
    ok: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    bytes_moved: int = 0
    seconds: float = 0.0


def download_tiles(tiles: list[str], dest: Path, catalog: Catalog,
                   progress: Progress = _noop,
                   should_stop: Callable[[], bool] = lambda: False, *,
                   transfer_progress: Callable[[str, int, int | None], None] | None = None,
                   ) -> StageResult:
    """Download tiles into `dest`, skipping any already present at full size.

    Sequential by design: IGN rate-limits concurrent requests.
    `transfer_progress(tile, received, total)` reports per-attempt byte counts;
    present files report their actual size as both received and total.
    """
    dest.mkdir(parents=True, exist_ok=True)
    res = StageResult()
    t0 = time.time()
    progress("download", 0, len(tiles), "Resolving tiles")

    for i, tile in enumerate(tiles, 1):
        if should_stop():
            break
        out = dest / tile
        entry = catalog.resolve(tile)

        if entry is None:                       # outside LiDAR HD coverage
            res.failed.append(tile)
            progress("download", i, len(tiles), f"{tile} not available")
            continue

        want = entry.get("bytes") or 0
        if out.exists() and (out.stat().st_size == want or not want):
            res.skipped += 1
            if transfer_progress:
                size = out.stat().st_size
                transfer_progress(tile, size, size)
            progress("download", i, len(tiles), f"{tile} present")
            continue

        try:
            kwargs = {}
            if transfer_progress:
                kwargs["on_progress"] = lambda received, total, tile=tile: transfer_progress(
                    tile, received, total)
            n = download(catalog.url(tile, entry["block"]), out, expected=want or None, **kwargs)
            res.ok += 1
            res.bytes_moved += n
            progress("download", i, len(tiles), f"{tile} {human(n)}")
        except HttpError as e:
            res.failed.append(tile)
            progress("download", i, len(tiles), f"{tile} FAILED {e}")

    res.seconds = time.time() - t0
    return res


# --- stage 2: colourise ----------------------------------------------------

def _ortho_url(tx: int, ty: int) -> str:
    bbox = f"{tx},{ty},{tx + 1000},{ty + 1000}"
    return (f"{WMS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
            f"&LAYERS={ORTHO_LAYER}&CRS=EPSG:{LAMBERT93}"
            f"&BBOX={bbox}&WIDTH={ORTHO_PX}&HEIGHT={ORTHO_PX}"
            f"&FORMAT=image/jpeg&STYLES=")


# Dimensions copied from LAS point format 6 to format 7 (= 6 + RGB).
_CARRY = [
    "X", "Y", "Z", "intensity", "return_number", "number_of_returns",
    "classification", "scan_angle", "user_data", "point_source_id", "gps_time",
    "scanner_channel", "scan_direction_flag", "edge_of_flight_line",
    "synthetic", "key_point", "withheld", "overlap",
]


def colorize_tile(src: Path, dst: Path, *, ortho_cache: Path | None = None) -> int:
    """Drape IGN BD ORTHO onto one tile, rewriting format 6 -> 7.

    LiDAR HD tiles have no RGB fields, so colour means rewriting every point
    into a wider format; output is ~50% larger.
    """
    import laspy
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None       # a 5000x5000 tile trips the bomb guard

    las = laspy.read(str(src))
    x = np.asarray(las.x, dtype=np.float64)
    y = np.asarray(las.y, dtype=np.float64)
    tx = int(x.min() // 1000) * 1000
    ty = int(y.min() // 1000) * 1000

    if ortho_cache is not None:
        from .ortho import get_ortho

        ortho = get_ortho(tx, ty, ortho_cache)
    else:
        ortho = dst.with_suffix(".ortho.jpg")
        ortho.write_bytes(get_bytes(_ortho_url(tx, ty), timeout=300))

    try:
        with Image.open(ortho) as image:
            arr = np.asarray(image.convert("RGB"))
        h, w, _ = arr.shape
        # Image row 0 is the north edge, so y is flipped against Lambert-93.
        px = np.clip(((x - tx) / 1000 * w).astype(np.int32), 0, w - 1)
        py = np.clip(((ty + 1000 - y) / 1000 * h).astype(np.int32), 0, h - 1)
        rgb = arr[py, px].astype(np.uint16) * 257      # 8-bit -> LAS 16-bit

        hdr = laspy.LasHeader(version="1.4", point_format=7)
        hdr.scales = las.header.scales
        hdr.offsets = las.header.offsets
        hdr.vlrs.extend([v for v in las.header.vlrs
                         if v.user_id == "LASF_Projection"])

        out = laspy.LasData(hdr)
        for dim in _CARRY:
            try:
                setattr(out, dim, getattr(las, dim))
            except Exception:                     # noqa: BLE001 - dim absent
                pass
        out.red, out.green, out.blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
        out.write(str(dst))
    finally:
        if ortho_cache is None:
            ortho.unlink(missing_ok=True)

    return len(las.points)


def colorize_all(src_dir: Path, dst_dir: Path, progress: Progress = _noop,
                 should_stop: Callable[[], bool] = lambda: False, *,
                 ortho_cache: Path | None = None) -> StageResult:
    dst_dir.mkdir(parents=True, exist_ok=True)
    tiles = sorted(src_dir.glob("*.copc.laz"))
    res = StageResult()
    t0 = time.time()

    for i, src in enumerate(tiles, 1):
        if should_stop():
            break
        dst = dst_dir / src.name.replace(".copc.laz", ".laz")
        if dst.exists() and dst.stat().st_size > 0:
            res.skipped += 1
            progress("colorize", i, len(tiles), f"{src.name} present")
            continue
        try:
            n = colorize_tile(src, dst, ortho_cache=ortho_cache)
            res.ok += 1
            res.bytes_moved += dst.stat().st_size
            progress("colorize", i, len(tiles), f"{src.name} {n:,} pts".replace(",", " "))
        except Exception as e:                    # noqa: BLE001 - keep going
            res.failed.append(src.name)
            progress("colorize", i, len(tiles), f"{src.name} FAILED {e}")

    res.seconds = time.time() - t0
    return res


# --- stage 3: 3D Tiles -----------------------------------------------------

def py3dtiles_exe() -> Path:
    """Locate the py3dtiles entry point.

    In a venv sys.executable already lives in Scripts/ (or bin/), while for a
    system Python the scripts sit in a sibling Scripts/ directory. Check both,
    then fall back to PATH.
    """
    exe = "py3dtiles.exe" if os.name == "nt" else "py3dtiles"
    here = Path(sys.executable).parent
    for candidate in (here / exe,                       # venv layout
                      here / "Scripts" / exe,           # system Python, Windows
                      here / "bin" / exe):              # system Python, POSIX
        if candidate.exists():
            return candidate

    found = shutil.which("py3dtiles")
    if found:
        return Path(found)

    raise FileNotFoundError(
        "py3dtiles not found. Install it into the environment running this "
        "app: pip install py3dtiles (or `uv add py3dtiles`)."
    )


def convert_3dtiles(inputs: Iterable[Path], out_dir: Path,
                    jobs: int = 0, keep_classification: bool = True,
                    progress: Progress = _noop) -> StageResult:
    """Convert every input into ONE tileset.

    All tiles go through a single py3dtiles call on purpose: converting them
    individually would produce unrelated tilesets whose LODs disagree at the
    seams.
    """
    inputs = list(inputs)
    res = StageResult()
    t0 = time.time()
    if not inputs:
        return res

    # py3dtiles refuses an existing output directory, even an empty one.
    if out_dir.exists():
        shutil.rmtree(out_dir)

    cmd = [str(py3dtiles_exe()), "convert", *[str(p) for p in inputs],
           "--out", str(out_dir),
           "--srs_in", str(LAMBERT93), "--srs_out", str(ECEF)]
    if keep_classification:
        # Lands in the .pnts batch table, which the viewer decodes.
        cmd += ["--extra-fields", "classification"]
    if jobs:
        cmd += ["--jobs", str(jobs)]

    progress("convert", 0, 1, f"{len(inputs)} tiles -> {out_dir.name}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        res.failed.append(proc.stderr.strip().splitlines()[-1] if proc.stderr else "py3dtiles failed")
    else:
        res.ok = len(inputs)
        res.bytes_moved = sum(p.stat().st_size for p in out_dir.rglob("*.pnts"))

    res.seconds = time.time() - t0
    progress("convert", 1, 1, f"{human(res.bytes_moved)}")
    return res


# --- stage 4: upload -------------------------------------------------------

_SNOWBALL_MAX_BYTES = 128 * 1024 * 1024
_SNOWBALL_MAX_FILES = 256
_SNOWBALL_LARGE_FILE = 16 * 1024 * 1024


def upload_dir(local: Path, prefix: str, cfg, progress: Progress = _noop,
               should_stop: Callable[[], bool] = lambda: False, *,
               snowball: bool = False) -> StageResult:
    """Mirror a local directory into a MinIO bucket under `prefix`.

    Snowball is opt-in and requires server-side TAR auto-extraction. Each TAR
    is staged on disk, bounded to 128 MiB / 256 files including TAR overhead,
    and sent with one PUT (SDK single-part limit: 5 GiB). MinIO Python 7.2.20
    still buffers that PUT in memory. Files >= 16 MiB and PMTiles use normal
    uploads. Extraction is checked by object size, not checksum; failure stops
    batching without fallback or remote deletion. Cancellation is between PUTs.
    Fewer PUTs may help small files, but TAR I/O and verification HEADs mean
    performance gains are not guaranteed. Per-file MIME metadata is not carried
    by the Snowball API; individual uploads retain their normal content types.
    """
    from minio import Minio

    client = Minio(cfg.endpoint, access_key=cfg.access_key,
                   secret_key=cfg.secret_key, secure=cfg.secure)
    if not client.bucket_exists(cfg.bucket):
        client.make_bucket(cfg.bucket)

    files = [p for p in local.rglob("*") if p.is_file()
             and p.suffix not in (".part", ".tmp") and "tmp" not in p.relative_to(local).parts]
    res = StageResult()
    t0 = time.time()

    if snowball:
        _upload_snowball_dir(client, cfg.bucket, local, prefix, files, res,
                             progress, should_stop)
        res.seconds = time.time() - t0
        return res

    for i, path in enumerate(files, 1):
        if should_stop():
            break
        key = f"{prefix}/{path.relative_to(local).as_posix()}"
        size = path.stat().st_size
        try:
            # Skip objects already present at the same size.
            try:
                if client.stat_object(cfg.bucket, key).size == size:
                    res.skipped += 1
                    progress("upload", i, len(files), f"{key} present")
                    continue
            except Exception:                     # noqa: BLE001 - not there yet
                pass

            client.fput_object(cfg.bucket, key, str(path),
                               content_type=_content_type(path))
            res.ok += 1
            res.bytes_moved += size
            progress("upload", i, len(files), f"{key} {human(size)}")
        except Exception as e:                    # noqa: BLE001
            res.failed.append(key)
            progress("upload", i, len(files), f"{key} FAILED {e}")

    res.seconds = time.time() - t0
    return res


def _snowball_tar_size(member_bytes: int) -> int:
    # Two end blocks, then the record padding written by tarfile.close().
    return ((member_bytes + 2 * tarfile.BLOCKSIZE + tarfile.RECORDSIZE - 1)
            // tarfile.RECORDSIZE * tarfile.RECORDSIZE)


def _upload_snowball_dir(client, bucket: str, local: Path, prefix: str,
                         files: list[Path], res: StageResult, progress: Progress,
                         should_stop: Callable[[], bool]) -> None:
    from minio.commonconfig import SnowballObject

    prefix = prefix.strip("/")
    batch: list[tuple[Path, str, int]] = []
    member_bytes = 0

    def report(detail: str) -> None:
        progress("upload", res.ok + res.skipped + len(res.failed), len(files), detail)

    def objects():
        for path, key, size in batch:
            # Data sources make regular members even for symlinks/hardlinks,
            # and fixed lengths keep growing source files within the TAR bound.
            with path.open("rb") as data:
                if os.fstat(data.fileno()).st_size != size:
                    raise OSError(f"{key} changed while preparing Snowball TAR")
                yield SnowballObject(key, data=data, length=size)

    def flush() -> bool:
        nonlocal member_bytes
        if should_stop():
            return False
        if not batch:
            return True
        try:
            with tempfile.TemporaryDirectory(prefix=".snowball-", dir=local) as staging:
                with closing(objects()) as sources:
                    client.upload_snowball_objects(
                        bucket, sources, staging_filename=str(Path(staging) / "batch.tar"),
                        compression=False)
        except Exception as e:                    # noqa: BLE001
            for _, key, _ in batch:
                res.failed.append(key)
                report(f"{key} FAILED Snowball upload/extraction required: {e}")
            return False

        verified = True
        for _, key, size in batch:
            try:
                actual = client.stat_object(bucket, key).size
                if actual != size:
                    raise ValueError(f"expected {size} bytes, found {actual}")
            except Exception as e:                # noqa: BLE001
                verified = False
                res.failed.append(key)
                report(f"{key} FAILED Snowball extraction not verified "
                       f"(server must support auto-extraction): {e}")
            else:
                res.ok += 1
                res.bytes_moved += size
                report(f"{key} {human(size)} (Snowball verified)")
        batch.clear()
        member_bytes = 0
        return verified

    for path in files:
        if should_stop():
            break
        relative = path.relative_to(local).as_posix()
        key = f"{prefix}/{relative}" if prefix else relative
        try:
            size = path.stat().st_size
            try:
                present = client.stat_object(bucket, key).size == size
            except Exception:                     # noqa: BLE001 - not there yet
                present = False
            if present:
                res.skipped += 1
                report(f"{key} present")
                continue

            info = tarfile.TarInfo(key)
            info.size = size
            overhead = len(info.tobuf(format=tarfile.PAX_FORMAT))
            entry_bytes = overhead + ((size + 511) // 512 * 512)
            if (path.suffix.lower() == ".pmtiles" or size >= _SNOWBALL_LARGE_FILE
                    or _snowball_tar_size(entry_bytes) > _SNOWBALL_MAX_BYTES):
                if not flush() or should_stop():
                    break
                client.fput_object(bucket, key, str(path), content_type=_content_type(path))
                res.ok += 1
                res.bytes_moved += size
                report(f"{key} {human(size)}")
                continue

            if batch and _snowball_tar_size(member_bytes + entry_bytes) > _SNOWBALL_MAX_BYTES:
                if not flush():
                    break
            if should_stop():
                break
            batch.append((path, key, size))
            member_bytes += entry_bytes
            if len(batch) >= _SNOWBALL_MAX_FILES and not flush():
                break
        except Exception as e:                    # noqa: BLE001
            res.failed.append(key)
            report(f"{key} FAILED {e}")
    else:
        flush()


# --- cleanup ---------------------------------------------------------------

def tileset_ok(tiles3d: Path) -> bool:
    """True when a 3D Tiles directory looks like a finished conversion.

    py3dtiles writes into tmp/ while running and only emits tileset.json at the
    end, so "tileset.json exists, some .pnts exist, tmp/ is gone" distinguishes
    a completed run from an interrupted one.
    """
    if not (tiles3d / "tileset.json").is_file():
        return False
    if (tiles3d / "tmp").exists():
        return False
    return any(tiles3d.rglob("*.pnts"))


def colorized_ok(colorized: Path, expected: int) -> bool:
    """True when every expected tile was colourised."""
    if expected <= 0:
        return False
    return sum(1 for _ in colorized.glob("*.laz")) >= expected


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def remove_tree(path: Path) -> int:
    """Delete a directory, returning the bytes freed (0 if it was absent)."""
    if not path.exists():
        return 0
    freed = dir_size(path)
    shutil.rmtree(path, ignore_errors=True)
    return freed if not path.exists() else 0


def sweep_scratch(root: Path) -> int:
    """Remove interrupted-download leftovers and py3dtiles scratch."""
    freed = 0
    for part in root.rglob("*.part"):
        try:
            freed += part.stat().st_size
            part.unlink()
        except OSError:
            pass
    tmp = root / "3dtiles" / "tmp"
    if tmp.exists():
        freed += remove_tree(tmp)
    return freed


def cleanup(root: Path, *, expected_tiles: int, did_color: bool,
            did_tiles: bool, progress: Progress = _noop) -> int:
    """Drop intermediates whose successor stage completed successfully.

    Deliberately conservative: each stage is only removed once the thing
    derived from it has been verified on disk. If a later stage failed or was
    skipped, its input is kept so a re-run does not start from nothing.
    """
    raw, colorized, tiles3d = root / "raw", root / "colorized", root / "3dtiles"

    # Decide BEFORE sweeping: sweep_scratch removes 3dtiles/tmp, which is the
    # very signal that tells an interrupted conversion from a finished one.
    tiles_done = did_tiles and tileset_ok(tiles3d)
    freed = sweep_scratch(root)

    color_done = did_color and colorized_ok(colorized, expected_tiles)

    if did_tiles and not tiles_done:
        # The conversion was asked for but did not finish. Both earlier stages
        # are the only way to retry it, so keep everything.
        progress("cleanup", 1, 1, "conversion incomplete - keeping raw/ and colorized/")
        return freed

    if tiles_done:
        # The tileset supersedes both earlier stages.
        for stage, path in (("colorized", colorized), ("raw", raw)):
            if path.exists():
                n = remove_tree(path)
                freed += n
                progress("cleanup", 0, 1, f"removed {stage}/ ({human(n)})")
    elif color_done:
        # No conversion requested: the colourised output is the deliverable,
        # so only raw/ is redundant.
        if raw.exists():
            n = remove_tree(raw)
            freed += n
            progress("cleanup", 0, 1, f"removed raw/ ({human(n)})")

    if freed:
        progress("cleanup", 1, 1, f"freed {human(freed)}")
    return freed


def _content_type(path: Path) -> str:
    return {
        ".json": "application/json",
        ".pnts": "application/octet-stream",
        ".laz": "application/octet-stream",
        ".pmtiles": "application/vnd.pmtiles",
        ".html": "text/html",
    }.get(path.suffix.lower(), "application/octet-stream")
