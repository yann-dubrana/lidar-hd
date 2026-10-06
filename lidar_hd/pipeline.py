"""The four pipeline stages: download, colourise, convert, upload.

Every stage is resumable. Re-running skips work that is already done, so an
interrupted job continues where it stopped rather than starting over.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .catalog import Catalog
from .config import (CLASS_COLOR_OTHER, CLASS_COLORS, COLOR_VERSION, COLORIZE_GROWTH,
                     DOWNLOAD_WORKERS, ECEF, GROUND_CELL_M, LAMBERT93, MEAN_TILE_BYTES,
                     OCCLUSION_CELL_M, OCCLUSION_DZ_M, TILES3D_GROWTH)
from .http import HttpError, download

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
        # 14-46 MB/s observed with DOWNLOAD_WORKERS connections to data.geopf.fr,
        # depending on the hour; 20 MB/s is the planning figure.
        return self.raw_bytes / 20e6 / 3600

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

    Tiles are resolved in order on the calling thread and fetched by
    DOWNLOAD_WORKERS threads, so `progress` and `transfer_progress` are called
    from several threads. `transfer_progress(tile, received, total)` reports
    per-attempt byte counts; present files report their actual size as both
    received and total. A slow or failed attempt reports
    `progress("download", done, total, "<tile> retry: <reason>")` without
    advancing `done`. A stop cancels the transfers in flight and removes their
    partial files. A tile whose size probe fails is reported FAILED, not missing.
    """
    dest.mkdir(parents=True, exist_ok=True)
    res = StageResult()
    t0 = time.time()
    progress("download", 0, len(tiles), "Resolving tiles")
    lock = threading.Lock()
    cancelled = threading.Event()
    done = 0

    def stopping() -> bool:
        return cancelled.is_set() or should_stop()

    def finish(detail: str) -> None:
        nonlocal done
        with lock:
            done += 1
            progress("download", done, len(tiles), detail)

    def fetch(tile: str, url: str, out: Path, want: int) -> None:
        def retrying(reason: str) -> None:
            with lock:
                progress("download", done, len(tiles), f"{tile} retry: {reason}")

        try:
            n = download(url, out, expected=want or None, on_retry=retrying, should_stop=stopping,
                         on_progress=transfer_progress and (
                             lambda received, total: transfer_progress(tile, received, total)))
            with lock:
                res.ok += 1
                res.bytes_moved += n
            finish(f"{tile} {human(n)}")
        except InterruptedError:
            pass
        except Exception as e:                      # noqa: BLE001 - nobody reads the future
            with lock:
                res.failed.append(tile)
            finish(f"{tile} FAILED {e}")

    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        pending = []
        try:
            for tile in tiles:
                if should_stop():
                    break
                out = dest / tile
                try:
                    entry = catalog.resolve(tile)
                except HttpError as e:                  # probe failed: unknown, not missing
                    with lock:
                        res.failed.append(tile)
                    finish(f"{tile} FAILED {e}")
                    continue

                if entry is None:                       # outside LiDAR HD coverage
                    with lock:
                        res.failed.append(tile)
                    finish(f"{tile} not available")
                    continue

                want = entry.get("bytes") or 0
                if out.exists() and (out.stat().st_size == want or not want):
                    with lock:
                        res.skipped += 1
                    if transfer_progress:
                        size = out.stat().st_size
                        transfer_progress(tile, size, size)
                    finish(f"{tile} present")
                    continue

                pending.append(pool.submit(fetch, tile, catalog.url(tile, entry["block"]), out, want))
            wait(pending)
        except BaseException:                       # Ctrl+C or a crash: do not drain the queue
            cancelled.set()
            pool.shutdown(wait=True, cancel_futures=True)
            raise

    res.seconds = time.time() - t0
    return res


# --- stage 2: colourise ----------------------------------------------------

# Dimensions copied from LAS point format 6 to format 7 (= 6 + RGB).
_CARRY = [
    "X", "Y", "Z", "intensity", "return_number", "number_of_returns",
    "classification", "scan_angle", "user_data", "point_source_id", "gps_time",
    "scanner_channel", "scan_direction_flag", "edge_of_flight_line",
    "synthetic", "key_point", "withheld", "overlap",
]


# Written into each colourised tile's header: a tile from an older algorithm
# is redone, one from this algorithm carries `height`.
_COLOR_STAMP = f"lidar-hd colour {COLOR_VERSION}"


def _coloured(path: Path) -> bool:
    """True when `path` is a tile colourised by this version of the algorithm."""
    import laspy

    try:
        with laspy.open(str(path)) as reader:
            return reader.header.generating_software == _COLOR_STAMP
    except Exception:                         # noqa: BLE001 - missing or unreadable: redo
        return False


_ARTEFACT = 65                  # LiDAR HD class for noise the survey rejected
_UNCLASSIFIED = 1               # wires, cars: thin things the ground shows through
_VEGETATION = (3, 4, 5)


def _cell_extremes(cell, value, ufunc):
    """(sorted unique cell ids, ufunc-reduced value per cell).

    Sort + reduceat: ufunc.at is far too slow for the 10M+ points of a tile.
    """
    import numpy as np

    order = np.argsort(cell, kind="stable")
    cells = cell[order]
    if not len(cells):
        return cells, value[:0]
    starts = np.flatnonzero(np.r_[True, cells[1:] != cells[:-1]])
    return cells[starts], ufunc.reduceat(value[order], starts)


def colorize_tile(src: Path, dst: Path, *, ortho_cache: Path) -> int:
    """Drape IGN BD ORTHO onto one tile, rewriting format 6 -> 7.

    LiDAR HD tiles have no RGB fields, so colour means rewriting every point
    into a wider format; output is ~50% larger.

    The orthophoto is nadir: it shows only the top of what stands at each spot.
    Points well below that top (walls, ground under a canopy or a bridge) get
    their class colour shaded by intensity instead of the roof or the leaves.
    Each point also gets `height`, centimetres above the tile's ground returns.
    """
    import laspy
    import numpy as np
    from PIL import Image

    from .ortho import get_ortho
    Image.MAX_IMAGE_PIXELS = None       # a 5000x5000 tile trips the bomb guard

    las = laspy.read(str(src))
    las.points = las.points[np.asarray(las.classification) != _ARTEFACT]
    x = np.asarray(las.x, dtype=np.float64)
    y = np.asarray(las.y, dtype=np.float64)
    z = np.asarray(las.z, dtype=np.float64)
    cls = np.asarray(las.classification, dtype=np.uint8)
    tx = int(x.min() // 1000) * 1000
    ty = int(y.min() // 1000) * 1000

    with Image.open(get_ortho(tx, ty, ortho_cache)) as image:
        arr = np.asarray(image.convert("RGB"))
    h, w, _ = arr.shape
    # Image row 0 is the north edge, so y is flipped against Lambert-93.
    px = np.clip(((x - tx) / 1000 * w).astype(np.int32), 0, w - 1)
    py = np.clip(((ty + 1000 - y) / 1000 * h).astype(np.int32), 0, h - 1)
    rgb = arr[py, px].astype(np.uint16)

    def cells(size: float):
        cx, cy = ((x - tx) / size).astype(np.int64), ((y - ty) / size).astype(np.int64)
        shape = int(cy.max()) + 1, int(cx.max()) + 1
        return cy * shape[1] + cx, shape

    # Top of each cell, ignoring unclassified returns: a power line must not
    # hide the ground under it.
    cell, _ = cells(OCCLUSION_CELL_M)
    solid = cls != _UNCLASSIFIED
    tops_at, tops = _cell_extremes(cell[solid], z[solid], np.maximum)
    top = np.full(len(z), -np.inf)
    if len(tops_at):
        at = np.minimum(np.searchsorted(tops_at, cell), len(tops_at) - 1)
        top = np.where(tops_at[at] == cell, tops[at], -np.inf)
    # Foliage under foliage is still the colour of the photo.
    hidden = (z < top - OCCLUSION_DZ_M) & ~np.isin(cls, (_UNCLASSIFIED, *_VEGETATION))
    if hidden.any():
        palette = np.full((256, 3), CLASS_COLOR_OTHER, dtype=np.float64)
        for code, colour in CLASS_COLORS.items():
            palette[code] = colour
        intensity = np.asarray(las.intensity, dtype=np.float64)
        peak = np.percentile(intensity, 98) or 1.0
        shade = 0.6 + 0.4 * np.clip(intensity[hidden] / peak, 0, 1)
        rgb[hidden] = (palette[cls[hidden]] * shade[:, None]).astype(np.uint16)

    # ponytail: ground is the lowest return per GROUND_CELL_M cell, sampled
    # nearest, so heights step by slope x cell size. Interpolate if that shows.
    height = np.zeros(len(z), dtype=np.uint16)
    ground = cls == 2
    if ground.any():
        from rasterio.fill import fillnodata

        cell, shape = cells(GROUND_CELL_M)
        lows_at, lows = _cell_extremes(cell[ground], z[ground], np.minimum)
        grid = np.zeros(shape, dtype=np.float32)
        known = np.zeros(shape, dtype=bool)
        grid.flat[lows_at], known.flat[lows_at] = lows, True
        grid = fillnodata(grid, mask=known, max_search_distance=max(shape))
        height = np.clip((z - grid.flat[cell]) * 100, 0, 65535).astype(np.uint16)

    hdr = laspy.LasHeader(version="1.4", point_format=7)
    hdr.scales = las.header.scales
    hdr.offsets = las.header.offsets
    hdr.vlrs.extend([v for v in las.header.vlrs
                     if v.user_id == "LASF_Projection"])
    hdr.add_extra_dim(laspy.ExtraBytesParams("height", np.uint16))
    hdr.generating_software = _COLOR_STAMP

    out = laspy.LasData(hdr)
    for dim in _CARRY:
        try:
            setattr(out, dim, getattr(las, dim))
        except Exception:                     # noqa: BLE001 - dim absent
            pass
    rgb *= 257                                # 8-bit -> LAS 16-bit
    out.red, out.green, out.blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    out.height = height
    # The header alone says a tile is done, so never leave half a file under dst.
    part = dst.with_name(dst.name + ".part")
    out.write(str(part), do_compress=dst.suffix == ".laz")
    part.replace(dst)
    return len(las.points)


def colorize_all(src_dir: Path, dst_dir: Path, progress: Progress = _noop,
                 should_stop: Callable[[], bool] = lambda: False, *,
                 ortho_cache: Path) -> StageResult:
    dst_dir.mkdir(parents=True, exist_ok=True)
    tiles = sorted(src_dir.glob("*.copc.laz"))
    res = StageResult()
    t0 = time.time()

    for i, src in enumerate(tiles, 1):
        if should_stop():
            break
        dst = dst_dir / src.name.replace(".copc.laz", ".laz")
        if _coloured(dst):
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

def convert_3dtiles(inputs: Iterable[Path], out_dir: Path,
                    jobs: int = 0,
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

    arguments = ["convert", *[str(p.resolve()) for p in inputs],
                 "--out", str(out_dir.resolve()),
                 "--srs_in", str(LAMBERT93), "--srs_out", str(ECEF),
                 # Lands in the .pnts batch table, which the viewer decodes.
                 "--extra-fields", "classification"]
    # Only when every input has it: py3dtiles would give the others height 0.
    if all(_coloured(p) for p in inputs):
        arguments += ["--extra-fields", "height"]
    if jobs:
        arguments += ["--jobs", str(jobs)]

    progress("convert", 0, 1, f"{len(inputs)} tiles -> {out_dir.name}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    # Keep the OS command short even for thousands of inputs. Close the manifest
    # before launching so the worker can open it on Windows.
    with tempfile.TemporaryDirectory(prefix=".conversion-", dir=out_dir.resolve().parent) as directory:
        manifest = Path(directory) / "arguments.json"
        manifest.write_text(json.dumps(arguments, ensure_ascii=False), encoding="utf-8")
        frozen = getattr(sys, "frozen", False)
        cmd = ([sys.executable, "--internal-py3dtiles"] if frozen
               else [sys.executable, "-m", "lidar_hd.conversion"])
        cmd.append(str(manifest))
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=None if frozen else Path(__file__).resolve().parent.parent)
    if proc.returncode != 0:
        res.failed.append(proc.stderr.strip().splitlines()[-1] if proc.stderr else "py3dtiles failed")
    else:
        res.ok = len(inputs)
        res.bytes_moved = sum(p.stat().st_size for p in out_dir.rglob("*.pnts"))
        prune_empty_points(out_dir)

    res.seconds = time.time() - t0
    progress("convert", 1, 1, f"{human(res.bytes_moved)}")
    return res


# --- stage 4: upload -------------------------------------------------------

def upload_dir(local: Path, prefix: str, cfg, progress: Progress = _noop,
               should_stop: Callable[[], bool] = lambda: False, *,
               snowball: bool = False) -> StageResult:
    """Mirror a local directory into a MinIO bucket under `prefix`.

    Snowball sends the complete directory in one disk-staged, streamed TAR,
    regardless of file size/count. PMTiles remain ordinary range-readable
    objects. Extraction is verified by size; there is no individual fallback.
    An entirely present directory is skipped, otherwise the whole TAR is sent.
    """
    from minio import Minio

    if not should_stop() and tileset_ok(local):
        prune_empty_points(local)
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

    # The mc client sends in parallel; one object at a time is the fallback,
    # and picks up whatever a failed mirror left behind.
    mc = _mc_command()
    if mc and _upload_mc(mc, local, prefix, cfg, len(files), res, progress, should_stop):
        res.seconds = time.time() - t0
        return res
    res = StageResult()

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


_MC_ALIAS = "lidarhd"           # defined per run through MC_HOST_<alias>, never saved


def _mc_command() -> list[str] | None:
    """How to run the MinIO client: on PATH first, then inside WSL, else None."""
    candidates = []
    native = shutil.which("mc")
    if native:
        candidates.append([native])
    wsl = shutil.which("wsl") if sys.platform == "win32" else None
    if wsl:
        try:
            # Not always on PATH there: also look where MinIO's installer puts it.
            found = subprocess.run([wsl, "-e", "sh", "-c", "command -v mc || ls ~/aistor-binaries/mc"],
                                   capture_output=True, text=True, timeout=20).stdout.split()
            if found:
                candidates.append([wsl, "-e", found[0]])
        except (OSError, subprocess.SubprocessError):
            pass
    for command in candidates:
        try:
            # `mc` is also Midnight Commander: only MinIO's answers like this.
            version = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=20)
            if version.returncode == 0 and "mc version" in version.stdout:
                return command
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _upload_mc(mc: list[str], local: Path, prefix: str, cfg, total: int, res: StageResult,
               progress: Progress, should_stop: Callable[[], bool]) -> bool:
    """`mc mirror` the directory; False when the caller should fall back."""
    from urllib.parse import quote

    scheme = "https" if cfg.secure else "http"
    host = (f"{scheme}://{quote(cfg.access_key, safe='')}:{quote(cfg.secret_key, safe='')}"
            f"@{cfg.endpoint}")
    name = f"MC_HOST_{_MC_ALIAS}"
    # Credentials travel in the environment, not on the command line. WSLENV
    # is what lets the variable through to a client running inside WSL.
    env = {**os.environ, name: host,
           "WSLENV": ":".join(filter(None, (os.environ.get("WSLENV"), name)))}
    bucket = f"{_MC_ALIAS}/{cfg.bucket}"
    run = {"cwd": local, "env": env, "text": True, "encoding": "utf-8", "errors": "replace"}
    try:
        # An alias mc does not know is taken for a local folder, and the mirror
        # would then "succeed" into it: prove the alias reaches the bucket first.
        if subprocess.run([*mc, "ls", bucket], capture_output=True, timeout=60, **run).returncode:
            progress("upload", 0, total, "mc cannot reach the bucket - sending one by one")
            return False
        progress("upload", 0, total, f"mc mirror -> {cfg.bucket}/{prefix.strip('/')}")
        proc = subprocess.Popen(
            [*mc, "mirror", "--json", "--overwrite", "--exclude", "*.part", "--exclude", "*.tmp",
             "--exclude", "tmp/*", "--exclude", "*/tmp/*", "./", f"{bucket}/{prefix.strip('/')}/"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **run)
    except (OSError, subprocess.SubprocessError) as e:
        progress("upload", 0, total, f"mc unavailable ({e}) - sending one by one")
        return False

    error = ""
    for line in proc.stdout:
        if should_stop():
            proc.terminate()
            break
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("status") != "success":
            error = str((event.get("error") or {}).get("message") or line.strip())
        elif "source" in event:
            res.ok += 1
            res.bytes_moved += event.get("size", 0)
            progress("upload", min(res.ok, total), total,
                     f"{Path(event['source']).name} {human(event.get('size', 0))}")
    if proc.wait() or error:
        if should_stop():
            return True                           # cancelled: nothing to fall back to
        progress("upload", res.ok, total, f"mc mirror failed ({error or proc.returncode}) - sending one by one")
        return False
    res.skipped = max(total - res.ok, 0)          # mirror only reports what it sent
    progress("upload", total, total, f"{res.ok} sent, {res.skipped} already there")
    return True


def _upload_snowball_dir(client, bucket: str, local: Path, prefix: str,
                         files: list[Path], res: StageResult, progress: Progress,
                         should_stop: Callable[[], bool]) -> None:
    from .snowball import UploadReader, upload_tar

    prefix = prefix.strip("/")
    batch: list[tuple[Path, str, int]] = []
    present_count = 0

    def report(detail: str) -> None:
        progress("upload", res.ok + res.skipped + len(res.failed), len(files), detail)

    for path in files:
        if should_stop():
            return
        relative = path.relative_to(local).as_posix()
        key = f"{prefix}/{relative}" if prefix else relative
        try:
            size = path.stat().st_size
            try:
                present = client.stat_object(bucket, key).size == size
            except Exception:                     # noqa: BLE001 - not there yet
                present = False
            if should_stop():
                return
            if path.suffix.lower() == ".pmtiles":
                if present:
                    res.skipped += 1
                else:
                    client.fput_object(bucket, key, str(path), content_type=_content_type(path))
                    res.ok += 1
                    res.bytes_moved += size
                report(f"{key} {'present' if present else human(size)}")
                continue
            batch.append((path, key, size))
            present_count += int(present)
        except Exception as e:                    # noqa: BLE001
            res.failed.append(key)
            report(f"{key} FAILED {e}")
            return
    if not batch or should_stop():
        return
    if present_count == len(batch):
        res.skipped += len(batch)
        report("Complete tileset already present")
        return
    try:
        with tempfile.TemporaryDirectory(prefix=".snowball-", dir=local) as staging:
            archive = Path(staging) / "tileset.tar"
            with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
                for index, (path, key, size) in enumerate(batch, 1):
                    if should_stop():
                        return
                    report(f"Preparing complete TAR {index}/{len(batch)}: {key}")
                    with path.open("rb") as data:
                        if os.fstat(data.fileno()).st_size != size:
                            raise OSError(f"{key} changed while preparing Snowball TAR")
                        info = tarfile.TarInfo(key)
                        info.size = size
                        tar.addfile(info, UploadReader(data, size, lambda *_: None, should_stop))
            report("Sending complete TAR (server-side extraction)")
            upload_tar(client, bucket, archive,
                       lambda done, total: report(f"TAR upload {human(done)} / {human(total)}"),
                       should_stop)
    except InterruptedError:
        if not should_stop():
            raise
        return
    except Exception as e:                        # noqa: BLE001
        if should_stop():
            return
        for _, key, _ in batch:
            res.failed.append(key)
        report(f"Complete TAR FAILED; Snowball upload/extraction required: {e}")
        return
    for _, key, size in batch:
        if should_stop():
            return
        try:
            actual = client.stat_object(bucket, key).size
            if actual != size:
                raise ValueError(f"expected {size} bytes, found {actual}")
        except Exception as e:                    # noqa: BLE001
            res.failed.append(key)
            report(f"{key} FAILED Snowball extraction not verified "
                   f"(server must support auto-extraction): {e}")
        else:
            res.ok += 1
            res.bytes_moved += size
            report(f"{key} {human(size)} (Snowball verified)")


# --- cleanup ---------------------------------------------------------------

def prune_empty_points(tileset: Path) -> int:
    """Remove empty directories under points/, returning how many; files are never deleted.

    rmdir only ever removes an empty directory, and os.walk does not descend
    into links or junctions. Directories that cannot be removed are skipped.
    """
    points = tileset / "points"
    removed = 0
    for directory, _, _ in os.walk(points, topdown=False):
        if Path(directory) != points:
            try:
                Path(directory).rmdir()
                removed += 1
            except OSError:
                pass
    return removed


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
        for stage, path in (("colorized", colorized), ("raw", raw),
                            ("clipped", root / "clipped"), ("enriched", root / "enriched")):
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
        ".pmtiles": "application/vnd.pmtiles",
        ".html": "text/html",
    }.get(path.suffix.lower(), "application/octet-stream")
