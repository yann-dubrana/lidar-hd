"""The four pipeline stages: download, colourise, convert, upload.

Every stage is resumable. Re-running skips work that is already done, so an
interrupted job continues where it stopped rather than starting over.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
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
                   should_stop: Callable[[], bool] = lambda: False) -> StageResult:
    """Download tiles into `dest`, skipping any already present at full size.

    Sequential by design: IGN rate-limits concurrent requests.
    """
    dest.mkdir(parents=True, exist_ok=True)
    res = StageResult()
    t0 = time.time()

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
            progress("download", i, len(tiles), f"{tile} present")
            continue

        try:
            n = download(catalog.url(tile, entry["block"]), out, expected=want or None)
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


def colorize_tile(src: Path, dst: Path) -> int:
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

    ortho = dst.with_suffix(".ortho.jpg")
    ortho.write_bytes(get_bytes(_ortho_url(tx, ty), timeout=300))

    try:
        arr = np.asarray(Image.open(ortho).convert("RGB"))
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
        ortho.unlink(missing_ok=True)

    return len(las.points)


def colorize_all(src_dir: Path, dst_dir: Path, progress: Progress = _noop,
                 should_stop: Callable[[], bool] = lambda: False) -> StageResult:
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
            n = colorize_tile(src, dst)
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
    return Path(sys.executable).parent / "Scripts" / "py3dtiles.exe"


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

def upload_dir(local: Path, prefix: str, cfg, progress: Progress = _noop,
               should_stop: Callable[[], bool] = lambda: False) -> StageResult:
    """Mirror a local directory into a MinIO bucket under `prefix`."""
    from minio import Minio

    client = Minio(cfg.endpoint, access_key=cfg.access_key,
                   secret_key=cfg.secret_key, secure=cfg.secure)
    if not client.bucket_exists(cfg.bucket):
        client.make_bucket(cfg.bucket)

    files = [p for p in local.rglob("*") if p.is_file()]
    res = StageResult()
    t0 = time.time()

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


def _content_type(path: Path) -> str:
    return {
        ".json": "application/json",
        ".pnts": "application/octet-stream",
        ".laz": "application/octet-stream",
        ".html": "text/html",
    }.get(path.suffix.lower(), "application/octet-stream")
