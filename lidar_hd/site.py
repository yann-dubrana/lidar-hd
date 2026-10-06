"""Enriched point clouds: a user's own LAS/LAZ, alone or set into LiDAR HD.

The file is rewritten in Lambert-93 so it can share one py3dtiles conversion
with LiDAR HD tiles, which accepts a single input CRS.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from .config import LAMBERT93

BBox = tuple[float, float, float, float]             # xmin, ymin, xmax, ymax


@dataclass(frozen=True)
class Site:
    name: str
    code: str
    source: Path
    epsg: int
    bbox: BBox                       # the file's footprint, Lambert-93
    centre: tuple[float, float]      # lon, lat, for the user to check the CRS
    clip: BBox | None = None         # LiDAR HD perimeter; None = file only
    level: str = "site"

    @property
    def label(self) -> str:
        around = "file only" if self.clip is None else "with LiDAR HD"
        return f"{self.name} (EPSG:{self.epsg}, {around})"


def source_epsg(header, override: int | None = None) -> int:
    """The file's CRS: explicit, else declared, else guessed from coordinates."""
    if override:
        return override
    try:
        crs = header.parse_crs()
    except Exception:                                 # noqa: BLE001 - unreadable VLR
        crs = None
    if crs is not None:
        # Compound CRS (horizontal + vertical) have no EPSG code of their own.
        for candidate in (crs, *crs.sub_crs_list):
            code = candidate.to_epsg()
            if code:
                return code
    x, y = (header.mins[0] + header.maxs[0]) / 2, (header.mins[1] + header.maxs[1]) / 2
    if 100_000 < x < 1_300_000 and 6_000_000 < y < 7_200_000:
        return LAMBERT93
    # Lambert CC42..CC50: northing origin (zone - 41) * 1 000 km + 200 km.
    if 1_000_000 < x < 2_400_000 and 1_000_000 < y < 10_000_000:
        return 3900 + 41 + int(y // 1_000_000)
    raise ValueError("No CRS in the file and its coordinates match no French projection. Give an EPSG code.")


def _transformer(epsg: int, target: int = LAMBERT93):
    from pyproj import Transformer

    return Transformer.from_crs(epsg, target, always_xy=True)


def inspect(source: Path, override: int | None = None) -> tuple[int, BBox, tuple[float, float]]:
    """(epsg, Lambert-93 footprint, lon/lat centre) from the header alone."""
    import laspy

    with laspy.open(str(source)) as f:
        header = f.header
        epsg = source_epsg(header, override)
        (x0, y0), (x1, y1) = header.mins[:2], header.maxs[:2]
    xs, ys = _transformer(epsg).transform([x0, x0, x1, x1], [y0, y1, y0, y1])
    bbox = (min(xs), min(ys), max(xs), max(ys))
    lon, lat = _transformer(epsg, 4326).transform((x0 + x1) / 2, (y0 + y1) / 2)
    if not all(map(isfinite, (*bbox, lon, lat))):
        raise ValueError(f"EPSG:{epsg} does not fit this file's coordinates. Give another EPSG code.")
    return epsg, bbox, (lon, lat)


def perimeter(bbox: BBox, buffer: float) -> BBox:
    return (bbox[0] - buffer, bbox[1] - buffer, bbox[2] + buffer, bbox[3] + buffer)


def rectangle(bbox: BBox) -> dict:
    x0, y0, x1, y1 = bbox
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def prepare(source: Path, dst: Path, epsg: int, bbox: BBox) -> Path:
    """Rewrite `source` in Lambert-93, point format 7.

    Files without RGB get their intensity as grey, since py3dtiles would
    otherwise write them black next to coloured LiDAR HD. Always rewritten: a
    kept copy would silently outlive a corrected EPSG.
    """
    import laspy
    import numpy as np

    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    transformer = _transformer(epsg)

    with laspy.open(str(source)) as reader:
        dims = set(reader.header.point_format.dimension_names)
        header = laspy.LasHeader(version="1.4", point_format=7)
        header.scales = [0.001] * 3
        # Centred, so int32 millimetres reach 2 km either way, at any altitude.
        header.offsets = [int((bbox[0] + bbox[2]) / 2), int((bbox[1] + bbox[3]) / 2),
                          int((reader.header.mins[2] + reader.header.maxs[2]) / 2)]
        peak = 0                     # intensity full scale, for the grey ramp
        with laspy.open(str(part), mode="w", header=header) as writer:
            for chunk in reader.chunk_iterator(2_000_000):
                out = laspy.ScaleAwarePointRecord.zeros(len(chunk), header=header)
                x, y, z = (np.asarray(chunk.x), np.asarray(chunk.y), np.asarray(chunk.z))
                if epsg != LAMBERT93:
                    x, y = transformer.transform(x, y)
                out.x, out.y, out.z = x, y, z
                for dim in ("intensity", "classification"):
                    if dim in dims:
                        out[dim] = chunk[dim]
                if "red" in dims:
                    rgb = [np.asarray(chunk[c], dtype=np.uint16) for c in ("red", "green", "blue")]
                    if max(int(c.max()) for c in rgb) < 256:       # 8-bit colour in a 16-bit field
                        rgb = [c * 257 for c in rgb]
                else:
                    intensity = np.asarray(chunk.intensity, dtype=np.float64)
                    # ponytail: 8- or 16-bit decided on the first chunk; take a
                    # percentile over the whole file if the grey looks flat.
                    peak = peak or (255 if intensity.max() < 256 else 65535)
                    rgb = [(np.clip(intensity / peak, 0, 1) * 65535).astype(np.uint16)] * 3
                out.red, out.green, out.blue = rgb
                writer.write_points(out)
    part.replace(dst)
    return dst


def clip_tiles(tiles: list[Path], dst_dir: Path, bbox: BBox) -> list[Path]:
    """Copy each LiDAR HD tile cropped to `bbox`; tiles left empty are dropped.

    Never crops in place: download and colourise recognise their files by size.
    Always recropped, so a changed perimeter is never served a stale copy.
    """
    import laspy

    dst_dir.mkdir(parents=True, exist_ok=True)
    kept = []
    for src in tiles:
        dst = dst_dir / src.name.replace(".copc.laz", ".laz")
        las = laspy.read(str(src))
        mask = ((las.x >= bbox[0]) & (las.x <= bbox[2])
                & (las.y >= bbox[1]) & (las.y <= bbox[3]))
        if not mask.any():
            continue
        # A fresh header drops the COPC index, which no longer matches.
        # The whole format, not its id: colourised tiles carry an extra `height`.
        header = laspy.LasHeader(version="1.4", point_format=las.header.point_format)
        header.scales, header.offsets = las.header.scales, las.header.offsets
        header.vlrs.extend(v for v in las.header.vlrs if v.user_id == "LASF_Projection")
        out = laspy.LasData(header)
        out.points = las.points[mask]
        part = dst.with_suffix(".laz.part")
        out.write(str(part), do_compress=True)
        part.replace(dst)
        kept.append(dst)
    return kept
