"""Enriched point clouds: a user's own LAS/LAZ, alone or set into LiDAR HD.

The file is rewritten in Lambert-93 so it can share one py3dtiles conversion
with LiDAR HD tiles, which accepts a single input CRS.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from math import isfinite
from pathlib import Path

from .config import GEOID_DIR, GROUND_CELL_M, LAMBERT93, LAMBERT93_IGN69, SITE_VOXEL_M, Z_TOLERANCE_M
from .pipeline import _ARTEFACT, _cell_extremes, _ground_grid

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


@cache
def _use_geoid() -> None:
    import pyproj

    pyproj.datadir.append_data_dir(str(GEOID_DIR))      # where the RAF20 grid is shipped


def _reprojection(header, epsg: int):
    """(x, y, z) -> Lambert-93 with NGF-IGN69 altitudes, for a file in `epsg`.

    Altitudes are converted only when the file says what they are: a 3D CRS
    means ellipsoidal heights, lowered by the geoid. A file declaring another
    altitude datum is refused, since PROJ would need a grid that is not here
    and would pass the altitudes through unchanged. An `epsg` that is not the
    file's own (an override) means its declaration is not to be trusted.
    """
    from pyproj import Transformer

    try:
        crs = header.parse_crs()
    except Exception:                                 # noqa: BLE001 - unreadable VLR
        crs = None
    if crs is not None and epsg in {c.to_epsg() for c in (crs, *crs.sub_crs_list)}:
        vertical = [c for c in crs.sub_crs_list if c.is_vertical]
        if vertical and vertical[0].to_epsg() != 5720:           # 5720: NGF-IGN69 height
            raise ValueError(f"The file's altitudes are declared as {vertical[0].name}, which cannot be "
                             f"converted to NGF-IGN69 here. Convert them first, or give --srs to ignore it.")
        if not vertical and len(crs.axis_info) == 3:
            _use_geoid()
            return Transformer.from_crs(crs, LAMBERT93_IGN69, always_xy=True).transform
    if epsg == LAMBERT93:
        return lambda x, y, z: (x, y, z)
    flat = _transformer(epsg)
    return lambda x, y, z: (*flat.transform(x, y), z)


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


@dataclass(frozen=True)
class ZCheck:
    offset: float                    # median of file minus LiDAR HD, metres
    spread: float                    # median absolute deviation around it
    cells: int                       # cells both clouds have a return in
    ground: bool                     # ground against ground, else lowest returns


@cache
def _to_ellipsoid():
    _use_geoid()
    return _transformer(LAMBERT93_IGN69, 4979)


def undulation(x: float, y: float) -> float:
    """Ellipsoidal height minus NGF-IGN69 altitude at a Lambert-93 point, metres."""
    lift = _to_ellipsoid().transform(x, y, 0.0)[2]
    if not 30 < lift < 70:           # PROJ answers 0 off the grid or without it, instead of failing
        raise RuntimeError("No RAF20 geoid height here: outside mainland France, or PROJ cannot see the grid.")
    return lift


def survey(source: Path, epsg: int, tiles: list[Path], bbox: BBox):
    """Compare the file with the LiDAR HD lying under it.

    Returns (ZCheck, ground). ZCheck is None when the two clouds share fewer
    than 50 cells. `ground` is LiDAR HD's ground under the file as
    (grid, x0, y0), for `prepare` to derive heights from; None without ground.

    Ground is compared with ground when the file has classified ground;
    otherwise the lowest return of each cell on both sides, which says nothing
    about a file that has no ground at all (a roof, a canopy).
    """
    import laspy
    import numpy as np

    x0, y0, x1, y1 = bbox
    width = int((x1 - x0) / SITE_VOXEL_M) + 1
    ground_shape = int((y1 - y0) / GROUND_CELL_M) + 1, int((x1 - x0) / GROUND_CELL_M) + 1

    def lows(x, y, z, cls):
        """Lowest return per cell, for every point and for ground only."""
        cell = ((y - y0) / SITE_VOXEL_M).astype(np.int64) * width + ((x - x0) / SITE_VOXEL_M).astype(np.int64)
        soil = cls == 2
        return _cell_extremes(cell, z, np.minimum), _cell_extremes(cell[soil], z[soil], np.minimum)

    def merged(parts):
        if not parts:
            return np.zeros(0, np.int64), np.zeros(0)
        return _cell_extremes(np.concatenate([c for c, _ in parts]),
                              np.concatenate([v for _, v in parts]), np.minimum)

    mine, theirs, soil_cells = ([], []), ([], []), []
    with laspy.open(str(source)) as reader:
        project = _reprojection(reader.header, epsg)
        classified = "classification" in reader.header.point_format.dimension_names
        for chunk in reader.chunk_iterator(2_000_000):
            x, y, z = project(np.asarray(chunk.x), np.asarray(chunk.y), np.asarray(chunk.z))
            keep = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
            cls = np.asarray(chunk.classification)[keep] if classified else np.zeros(int(keep.sum()), np.uint8)
            for store, found in zip(mine, lows(x[keep], y[keep], z[keep], cls)):
                store.append(found)
    # ponytail: each tile is read whole here and again by clip_tiles; keep the
    # arrays between the two if that second read ever shows in the timings.
    for tile in tiles:
        with laspy.open(str(tile)) as reader:
            mins, maxs = reader.header.mins, reader.header.maxs
        if maxs[0] < x0 or mins[0] > x1 or maxs[1] < y0 or mins[1] > y1:
            continue
        las = laspy.read(str(tile))
        x, y, z = np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)
        keep = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1) & (np.asarray(las.classification) != _ARTEFACT)
        x, y, z, cls = x[keep], y[keep], z[keep], np.asarray(las.classification)[keep]
        for store, found in zip(theirs, lows(x, y, z, cls)):
            store.append(found)
        soil = cls == 2
        cell = (((y[soil] - y0) / GROUND_CELL_M).astype(np.int64) * ground_shape[1]
                + ((x[soil] - x0) / GROUND_CELL_M).astype(np.int64))
        soil_cells.append(_cell_extremes(cell, z[soil], np.minimum))

    check = None
    for ground in (True, False):
        (a_cells, a_low), (b_cells, b_low) = merged(mine[ground]), merged(theirs[ground])
        _, i, j = np.intersect1d(a_cells, b_cells, assume_unique=True, return_indices=True)
        if len(i) >= 50:
            difference = a_low[i] - b_low[j]
            offset = float(np.median(difference))
            check = ZCheck(offset, float(np.median(np.abs(difference - offset))), len(i), ground)
            break
    cells, values = merged(soil_cells)
    return check, ((_ground_grid(cells, ground_shape, values), x0, y0) if len(cells) else None)


def z_shift(check: ZCheck | None, bbox: BBox, align: bool = False) -> tuple[float, str]:
    """Metres to add to the file's altitudes, and the verdict to show.

    Raises ValueError for a ground offset nothing explains, unless `align`.
    """
    if check is None:
        return 0.0, "Altitude not checked: no LiDAR HD under the file to compare with"
    measured = (f"{check.offset:+.2f} m ({'ground' if check.ground else 'lowest returns'}, "
                f"{check.cells} cells, spread {check.spread:.2f} m)")
    if abs(check.offset) <= Z_TOLERANCE_M:
        return 0.0, f"Altitude agrees with LiDAR HD: {measured}"
    try:
        lift = undulation((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
    except RuntimeError:             # no geoid to explain the offset; the conversion checks the grid itself
        lift = float("inf")
    if abs(check.offset - lift) <= 1:
        return -lift, f"Ellipsoidal heights detected ({measured}): lowered by the geoid, {lift:.2f} m"
    if align:
        return -check.offset, f"Altitude aligned on LiDAR HD: file was {measured}"
    if check.ground and check.spread <= 1:
        raise ValueError(f"The file's ground is {measured} from LiDAR HD. Correct its altitudes, "
                         f"or align it (--z-align, or the Align altitude box).")
    return 0.0, f"Altitude not verified, file left as it is: {measured} from LiDAR HD"


def _voxels(x, y, z):
    """One int64 per point naming its SITE_VOXEL_M cube, in Lambert-93."""
    import numpy as np

    def index(v):
        return np.floor(np.asarray(v) / SITE_VOXEL_M).astype(np.int64)

    # 24 bits of northing and 18 of altitude (offset to stay positive) under the easting.
    return (index(x) << 42) | (index(y) << 18) | (index(z) + 100_000)


def occupied(path: Path):
    """Sorted voxels holding at least one point of a prepared file."""
    import laspy
    import numpy as np

    # ponytail: every key in memory, 8 bytes per occupied cube. Go per tile if
    # a file ever fills hundreds of millions of them.
    keys = [np.zeros(0, np.int64)]
    with laspy.open(str(path)) as reader:
        for chunk in reader.chunk_iterator(2_000_000):
            keys.append(np.unique(_voxels(chunk.x, chunk.y, chunk.z)))
    return np.unique(np.concatenate(keys))


def prepare(source: Path, dst: Path, epsg: int, bbox: BBox, *,
            shift: float = 0.0, ground=None) -> Path:
    """Rewrite `source` in Lambert-93, point format 7.

    Files without RGB get their intensity as grey, since py3dtiles would
    otherwise write them black next to coloured LiDAR HD. Always rewritten: a
    kept copy would silently outlive a corrected EPSG.

    `shift` is added to every altitude. With `ground` (from `survey`) each
    point also gets `height`, centimetres above LiDAR HD's ground, as
    colourised tiles have.
    """
    import laspy
    import numpy as np

    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")

    with laspy.open(str(source)) as reader:
        project = _reprojection(reader.header, epsg)
        dims = set(reader.header.point_format.dimension_names)
        header = laspy.LasHeader(version="1.4", point_format=7)
        header.scales = [0.001] * 3
        # Centred, so int32 millimetres reach 2 km either way, at any altitude.
        header.offsets = [int((bbox[0] + bbox[2]) / 2), int((bbox[1] + bbox[3]) / 2),
                          int((reader.header.mins[2] + reader.header.maxs[2]) / 2 + shift)]
        if ground:
            header.add_extra_dim(laspy.ExtraBytesParams("height", np.uint16))
        peak = 0                     # intensity full scale, for the grey ramp
        with laspy.open(str(part), mode="w", header=header) as writer:
            for chunk in reader.chunk_iterator(2_000_000):
                out = laspy.ScaleAwarePointRecord.zeros(len(chunk), header=header)
                x, y, z = project(np.asarray(chunk.x), np.asarray(chunk.y), np.asarray(chunk.z))
                z = z + shift
                out.x, out.y, out.z = x, y, z
                if ground:
                    grid, x0, y0 = ground
                    row = np.clip(((y - y0) / GROUND_CELL_M).astype(np.int64), 0, grid.shape[0] - 1)
                    column = np.clip(((x - x0) / GROUND_CELL_M).astype(np.int64), 0, grid.shape[1] - 1)
                    out.height = np.clip((z - grid[row, column]) * 100, 0, 65535).astype(np.uint16)
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


def clip_tiles(tiles: list[Path], dst_dir: Path, bbox: BBox, occupied=None) -> list[Path]:
    """Copy each LiDAR HD tile cropped to `bbox`; tiles left empty are dropped.

    Never crops in place: download and colourise recognise their files by size.
    Always recropped, so a changed perimeter is never served a stale copy.
    Points sharing a voxel with the user's file (`occupied`) are dropped: the
    file replaces LiDAR HD where it has points, and only there, so an interior
    scan keeps the LiDAR HD roof above it.
    """
    import laspy
    import numpy as np

    dst_dir.mkdir(parents=True, exist_ok=True)
    kept = []
    for src in tiles:
        dst = dst_dir / src.name.replace(".copc.laz", ".laz")
        las = laspy.read(str(src))
        x, y = np.asarray(las.x), np.asarray(las.y)
        mask = ((x >= bbox[0]) & (x <= bbox[2]) & (y >= bbox[1]) & (y <= bbox[3])
                & (np.asarray(las.classification) != _ARTEFACT))
        if occupied is not None and len(occupied):
            inside = np.flatnonzero(mask)
            doubled = np.isin(_voxels(x[inside], y[inside], np.asarray(las.z)[inside]), occupied)
            mask[inside[doubled]] = False
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
