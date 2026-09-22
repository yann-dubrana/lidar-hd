"""Shared IGN imagery cache and a disk-backed, sparse raster PMTiles exporter.

The source is 20 cm BD ORTHO, not a 20 cm Web Mercator grid: maximum zoom
is ceil(log2(156543.03 * cos(latitude) / source_gsd)). Reprojection and
overviews resample the source; lossless PNG avoids further lossy encoding.
Only one source image or one output tile is materialised at a time. Tiled
GeoTIFFs, a VRT and the PNG pyramid live on disk during a build. Coordinate
sets and the PMTiles directory still require memory proportional to tile count.
"""
from __future__ import annotations

import hashlib
import io
import math
import sqlite3
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from PIL import Image
from pmtiles.reader import Reader, all_tiles
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform, transform_bounds
from rasterio.windows import Window

from .config import LAMBERT93, ORTHO_LAYER, ORTHO_PX, WMS
from .http import get_bytes

_HALF_WORLD = math.pi * 6378137
_SIZE = 256
_ATTRIBUTION = "© IGN — BD ORTHO® — Licence Ouverte / Open Licence 2.0"


def _url(tx: int, ty: int) -> str:
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": ORTHO_LAYER, "CRS": f"EPSG:{LAMBERT93}",
        "BBOX": f"{tx},{ty},{tx + 1000},{ty + 1000}",
        "WIDTH": ORTHO_PX, "HEIGHT": ORTHO_PX,
        "FORMAT": "image/jpeg", "STYLES": "",
    }
    return f"{WMS}?{urllib.parse.urlencode(params)}"


def _validate_image(source) -> None:
    try:
        with Image.open(source) as image:
            if image.format != "JPEG" or image.size != (ORTHO_PX, ORTHO_PX):
                raise ValueError(f"Expected a {ORTHO_PX} × {ORTHO_PX} JPEG orthophoto")
            image.load()
    except (OSError, SyntaxError) as exc:
        raise ValueError("Invalid IGN orthophoto image") from exc


def get_ortho(tx: int, ty: int, cache: Path) -> Path:
    """Fetch/cache a 1 km square; tx/ty are bottom-left EPSG:2154 metres.

    Valid cached images survive failed exports. The URL fingerprint prevents
    reuse when the layer, service or requested resolution changes. The service
    is a rolling mosaic; cache reuse deliberately does not check remote freshness.
    """
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    url = _url(tx, ty)
    fingerprint = hashlib.sha256(url.encode()).hexdigest()[:16]
    dest = cache / f"{tx}_{ty}_{fingerprint}.jpg"
    if dest.is_file():
        try:
            _validate_image(dest)
            return dest
        except ValueError:
            pass
    data = get_bytes(url, timeout=300)
    _validate_image(io.BytesIO(data))
    with tempfile.NamedTemporaryFile(dir=cache, suffix=".part", delete=False) as fh:
        part = Path(fh.name)
        try:
            fh.write(data)
        except BaseException:
            fh.close()
            part.unlink(missing_ok=True)
            raise
    try:
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def _check_stop(should_stop: Callable[[], bool]) -> None:
    if should_stop():
        raise InterruptedError("Orthophoto export cancelled")


def _read_at(fh):
    def read(offset, length):
        fh.seek(offset)
        return fh.read(length)
    return read


def _valid_archive(path: Path, manifest: dict) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            source = _read_at(fh)
            reader = Reader(source)
            header = reader.header()
            if header["tile_data_offset"] + header["tile_data_length"] != path.stat().st_size:
                return False
            if reader.metadata().get("source_manifest") != manifest:
                return False
            coords, data = next(all_tiles(source))
            if reader.get(*coords) != data:
                return False
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                return image.format == "PNG" and image.size == (_SIZE, _SIZE)
    except Exception:
        return False


def _mosaic(tiles, cache: Path, work: Path, progress, should_stop) -> Path:
    left = min(x for x, y in tiles) * 1000
    top = max(y for x, y in tiles) * 1000
    width = (max(x for x, y in tiles) * 1000 + 1000 - left) * ORTHO_PX // 1000
    height = (top - (min(y for x, y in tiles) - 1) * 1000) * ORTHO_PX // 1000
    vrt = ET.Element("VRTDataset", rasterXSize=str(width), rasterYSize=str(height))
    ET.SubElement(vrt, "SRS").text = f"EPSG:{LAMBERT93}"
    pixel = 1000 / ORTHO_PX
    ET.SubElement(vrt, "GeoTransform").text = f"{left},{pixel},0,{top},0,{-pixel}"
    bands = []
    for i, color in enumerate(("Red", "Green", "Blue", "Alpha"), 1):
        band = ET.SubElement(vrt, "VRTRasterBand", dataType="Byte", band=str(i))
        ET.SubElement(band, "ColorInterp").text = color
        bands.append(band)
    for done, (tx, ty) in enumerate(tiles, 1):
        _check_stop(should_stop)
        progress("ortho", done - 1, len(tiles), f"Fetching imagery {tx}, {ty}")
        image_path = get_ortho(tx * 1000, (ty - 1) * 1000, cache)
        _check_stop(should_stop)
        tif = work / f"{tx}_{ty}.tif"
        bounds = (tx * 1000, (ty - 1) * 1000, (tx + 1) * 1000, ty * 1000)
        with Image.open(image_path) as image, rasterio.open(
            tif, "w", driver="GTiff", width=ORTHO_PX, height=ORTHO_PX,
            count=3, dtype="uint8", crs=f"EPSG:{LAMBERT93}",
            transform=from_bounds(*bounds, ORTHO_PX, ORTHO_PX),
            tiled=True, blockxsize=256, blockysize=256, compress="deflate",
        ) as dst:
            for row in range(0, ORTHO_PX, 256):
                _check_stop(should_stop)
                rows = min(256, ORTHO_PX - row)
                strip = np.asarray(image.crop((0, row, ORTHO_PX, row + rows)).convert("RGB"))
                dst.write(strip.transpose(2, 0, 1), window=Window(0, row, ORTHO_PX, rows))
        for i, band in enumerate(bands, 1):
            source = ET.SubElement(band, "SimpleSource")
            ET.SubElement(source, "SourceFilename", relativeToVRT="1").text = tif.name
            ET.SubElement(source, "SourceBand").text = str(i) if i < 4 else "mask,1"
            ET.SubElement(source, "SrcRect", xOff="0", yOff="0",
                          xSize=str(ORTHO_PX), ySize=str(ORTHO_PX))
            ET.SubElement(source, "DstRect", xOff=str((tx * 1000 - left) * ORTHO_PX // 1000),
                          yOff=str((top - ty * 1000) * ORTHO_PX // 1000),
                          xSize=str(ORTHO_PX), ySize=str(ORTHO_PX))
        progress("ortho", done, len(tiles), f"Cached imagery {tx}, {ty}")
    path = work / "mosaic.vrt"
    ET.ElementTree(vrt).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _coverage(tiles, zoom):
    span = 2 * _HALF_WORLD / (1 << zoom)
    result = set()
    for tx, ty in tiles:
        west, south, east, north = transform_bounds(
            LAMBERT93, 3857, tx * 1000, (ty - 1) * 1000,
            (tx + 1) * 1000, ty * 1000, densify_pts=21,
        )
        x0 = max(0, math.floor((west + _HALF_WORLD) / span))
        x1 = min((1 << zoom) - 1, math.floor((east + _HALF_WORLD) / span))
        y0 = max(0, math.floor((_HALF_WORLD - north) / span))
        y1 = min((1 << zoom) - 1, math.floor((_HALF_WORLD - south) / span))
        result.update((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    return result


def _store(db, z, x, y, image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    db.execute("INSERT INTO tiles VALUES (?, ?, ?, ?, ?)",
               (zxy_to_tileid(z, x, y), z, x, y, out.getvalue()))


def _pyramid(vrt, db, tiles, zoom, progress, should_stop):
    candidates = _coverage(tiles, zoom)
    present = set()
    span = 2 * _HALF_WORLD / (1 << zoom)
    with rasterio.open(vrt) as src:
        for done, (x, y) in enumerate(sorted(candidates), 1):
            _check_stop(should_stop)
            west, north = x * span - _HALF_WORLD, _HALF_WORLD - y * span
            rgba = np.zeros((4, _SIZE, _SIZE), dtype=np.uint8)
            reproject(
                rasterio.band(src, (1, 2, 3, 4)), rgba,
                dst_transform=from_bounds(west, north - span, west + span, north, _SIZE, _SIZE),
                dst_crs="EPSG:3857", src_alpha=4, dst_alpha=4,
                resampling=Resampling.bilinear, warp_mem_limit=32,
            )
            if rgba[3].any():
                _store(db, zoom, x, y, Image.fromarray(rgba.transpose(1, 2, 0)))
                present.add((x, y))
            progress("ortho", done, len(candidates), f"Raster tiles z{zoom}")
    if not present:
        raise ValueError("No orthophoto pixels in the selected coverage")
    db.commit()
    for z in range(zoom - 1, -1, -1):
        parents = {(x // 2, y // 2) for x, y in present}
        for done, (x, y) in enumerate(sorted(parents), 1):
            _check_stop(should_stop)
            image = Image.new("RGBA", (2 * _SIZE, 2 * _SIZE))
            for dx in range(2):
                for dy in range(2):
                    row = db.execute("SELECT data FROM tiles WHERE id = ?",
                                     (zxy_to_tileid(z + 1, x * 2 + dx, y * 2 + dy),)).fetchone()
                    if row:
                        with Image.open(io.BytesIO(row[0])) as child:
                            image.paste(child, (dx * _SIZE, dy * _SIZE))
            _store(db, z, x, y, image.resize((_SIZE, _SIZE), Image.Resampling.BOX))
            progress("ortho", done, len(parents), f"Overviews z{z}")
        db.commit()
        present = parents


def export_pmtiles(tiles: list[tuple[int, int]], root: Path,
                   progress: Callable[[str, int, int, str], None],
                   should_stop: Callable[[], bool]) -> Path:
    """Export NW kilometre tile coordinates to root/ortho/orthophoto.pmtiles.

    An exact embedded source-selection manifest permits archive reuse. Failed
    or cancelled builds keep the JPEG cache and never replace a completed
    archive. Cancellation is checked between HTTP requests and raster windows;
    an in-flight HTTP request cannot be interrupted by this synchronous API.
    """
    tiles = sorted(set(tiles))
    if not tiles:
        raise ValueError("Select at least one tile for orthophoto export")
    _check_stop(should_stop)
    root = Path(root)
    dest = root / "ortho" / "orthophoto.pmtiles"
    manifest = {"version": 1, "tiles": [list(tile) for tile in tiles],
                "wms": WMS, "layer": ORTHO_LAYER, "source_crs": LAMBERT93,
                "source_pixels": ORTHO_PX, "encoding": "png"}
    if _valid_archive(dest, manifest):
        progress("ortho", 1, 1, "Reusing completed orthophoto archive")
        return dest
    bounds = [transform_bounds(LAMBERT93, 4326, tx * 1000, (ty - 1) * 1000,
                               (tx + 1) * 1000, ty * 1000, densify_pts=21)
              for tx, ty in tiles]
    west, south = min(b[0] for b in bounds), min(b[1] for b in bounds)
    east, north = max(b[2] for b in bounds), max(b[3] for b in bounds)
    latitude = 0 if south <= 0 <= north else min(abs(south), abs(north))
    gsd = 1000 / ORTHO_PX
    zoom = max(0, min(24, math.ceil(math.log2(
        2 * _HALF_WORLD / _SIZE * math.cos(math.radians(latitude)) / gsd))))
    tx, ty = tiles[len(tiles) // 2]
    lon, lat = transform(LAMBERT93, 4326, [tx * 1000 + 500], [ty * 1000 - 500])
    center = [lon[0], lat[0], max(0, zoom - 2)]
    metadata = {
        "name": "IGN BD ORTHO", "format": "png", "type": "baselayer",
        "description": "20 cm source orthophotography resampled onto a Web Mercator display grid",
        "attribution": _ATTRIBUTION, "license": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
        "bounds": f"{west},{south},{east},{north}", "center": ",".join(map(str, center)),
        "minzoom": 0, "maxzoom": zoom, "source_gsd_m": gsd,
        "source_manifest": manifest,
    }
    header = {
        "tile_type": TileType.PNG, "tile_compression": Compression.NONE,
        "min_lon_e7": math.floor(west * 1e7), "min_lat_e7": math.floor(south * 1e7),
        "max_lon_e7": math.ceil(east * 1e7), "max_lat_e7": math.ceil(north * 1e7),
        "center_lon_e7": round(center[0] * 1e7), "center_lat_e7": round(center[1] * 1e7),
        "center_zoom": center[2],
    }
    cache = root / "ortho-cache"
    cache.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=cache) as directory, \
            rasterio.Env(GDAL_CACHEMAX=64 * 1024 * 1024):
        work = Path(directory)
        vrt = _mosaic(tiles, cache, work, progress, should_stop)
        part = work / "orthophoto.pmtiles.part"
        with closing(sqlite3.connect(work / "pyramid.sqlite")) as db:
            db.execute("CREATE TABLE tiles (id INTEGER PRIMARY KEY, z INTEGER, x INTEGER, y INTEGER, data BLOB)")
            _pyramid(vrt, db, tiles, zoom, progress, should_stop)
            count = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
            with part.open("wb") as fh:
                writer = Writer(fh)
                writer.tile_f.close()
                writer.tile_f = tempfile.TemporaryFile(dir=work)
                try:
                    for done, (tile_id, data) in enumerate(db.execute("SELECT id, data FROM tiles ORDER BY id"), 1):
                        _check_stop(should_stop)
                        writer.write_tile(tile_id, data)
                        progress("ortho", done, count, "Packing PMTiles archive")
                    _check_stop(should_stop)
                    writer.finalize(header, metadata)
                finally:
                    writer.tile_f.close()
        if not _valid_archive(part, manifest):
            raise ValueError("Orthophoto PMTiles verification failed")
        _check_stop(should_stop)
        part.replace(dest)
    progress("ortho", 1, 1, "Orthophoto archive complete")
    return dest