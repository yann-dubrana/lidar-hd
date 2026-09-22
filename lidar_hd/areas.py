"""Administrative areas and the LiDAR HD tile grid.

Areas come from IGN ADMIN EXPRESS via WFS, fetched directly in Lambert-93 so
no reprojection is needed: LiDAR HD tiles are named by their Lambert-93
kilometre coordinates.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal

from .http import get_json, wfs_url

Level = Literal["commune", "departement", "region"]

LEVELS: dict[Level, str] = {
    "commune": "Commune",
    "departement": "Department",
    "region": "Region",
}


@dataclass(frozen=True)
class Area:
    level: Level
    name: str
    code: str
    population: int | None = None

    @property
    def label(self) -> str:
        if self.population:
            return f"{self.name} ({self.code}) · {self.population:,} hab".replace(",", " ")
        return f"{self.name} ({self.code})"


def search(level: Level, term: str, limit: int = 25) -> list[Area]:
    """Find areas whose name starts with `term` (case-insensitive)."""
    term = term.strip().replace("'", "''")
    if not term:
        return []

    props = "nom_officiel,code_insee"
    if level == "commune":
        props += ",population"

    # ILIKE keeps the search case-insensitive (plain LIKE matches nothing);
    # the trailing % makes it a prefix match. Passed unencoded -- wfs_url()
    # urlencodes every parameter itself.
    cql = f"nom_officiel ILIKE '{term}%'"
    url = wfs_url(level, cql=cql, count=limit, properties=props)
    data = get_json(url)

    areas = []
    for feat in data.get("features", []):
        p = feat["properties"]
        areas.append(Area(
            level=level,
            name=p.get("nom_officiel", "?"),
            code=p.get("code_insee", "?"),
            population=p.get("population"),
        ))
    # Prefer exact matches, then larger places -- "Pessac" should outrank
    # "Pessac-sur-Dordogne".
    areas.sort(key=lambda a: (a.name.lower() != term.lower(), -(a.population or 0)))
    return areas


def geometry(area: Area) -> dict:
    """Fetch an area's polygon in Lambert-93 (EPSG:2154)."""
    data = get_json(wfs_url(area.level, cql=f"code_insee='{area.code}'"))
    feats = data.get("features") or []
    if not feats:
        raise LookupError(f"no geometry for {area.label}")
    return feats[0]["geometry"]


# --- tile grid -------------------------------------------------------------

def _rings(geom: dict) -> list[list[list]]:
    """All polygons of a (Multi)Polygon as lists of rings."""
    if geom["type"] == "MultiPolygon":
        return list(geom["coordinates"])
    return [geom["coordinates"]]


def area_km2(geom: dict) -> float:
    """Planar area in km². Valid because the geometry is already in metres."""
    def ring_area(ring: list) -> float:
        s = 0.0
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            s += x1 * y2 - x2 * y1
        return abs(s / 2)

    total = 0.0
    for poly in _rings(geom):
        total += ring_area(poly[0])
        for hole in poly[1:]:
            total -= ring_area(hole)
    return total / 1e6


def tiles_for(geom: dict) -> list[tuple[int, int]]:
    """The 1 km tiles a geometry touches, as (x_km, y_km) Lambert-93 pairs.

    Uses scanline fill rather than point-in-polygon per tile: a department is
    ~10k tiles against a boundary with tens of thousands of edges, and the
    naive form takes minutes where this takes under a second.

    Tiles are named by their NW corner, so tile (tx, ty) covers
    x in [tx, tx+1) km and y in [ty-1, ty) km.
    """
    polys = _rings(geom)

    edges: list[tuple[float, float, float, float]] = []
    xs: list[float] = []
    ys: list[float] = []
    for poly in polys:
        for ring in poly:
            n = len(ring)
            for i in range(n):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                xs.append(x1)
                ys.append(y1)
                if y1 != y2:                     # horizontal edges never cross
                    edges.append((x1, y1, x2, y2))

    if not edges:
        return []

    # Bucket edges by kilometre row so each scanline only tests nearby edges.
    buckets: dict[int, list] = defaultdict(list)
    for e in edges:
        lo = int(min(e[1], e[3]) // 1000)
        hi = int(max(e[1], e[3]) // 1000) + 1
        for row in range(lo, hi + 1):
            buckets[row].append(e)

    y0 = int(min(ys) // 1000)
    y1 = int(max(ys) // 1000) + 1

    covered: set[tuple[int, int]] = set()
    for ty in range(y0, y1 + 2):
        cell_lo, cell_hi = (ty - 1) * 1000, ty * 1000
        # Several scanlines per row so a tile clipped by a thin sliver of the
        # boundary is still caught.
        for frac in (0.02, 0.25, 0.5, 0.75, 0.98):
            y = cell_lo + (cell_hi - cell_lo) * frac
            crossings = []
            for x1, ey1, x2, ey2 in buckets.get(int(y // 1000), ()):
                if (ey1 > y) != (ey2 > y):
                    crossings.append(x1 + (x2 - x1) * (y - ey1) / (ey2 - ey1))
            crossings.sort()
            for i in range(0, len(crossings) - 1, 2):
                xa, xb = crossings[i], crossings[i + 1]
                for tx in range(int(xa // 1000), int(xb // 1000) + 1):
                    covered.add((tx, ty))

    return sorted(covered)


def tile_name(tx: int, ty: int) -> str:
    """The IGN filename for a tile, e.g. LHD_FXX_0404_6420_PTS_LAMB93_IGN69."""
    return f"LHD_FXX_{tx:04d}_{ty:04d}_PTS_LAMB93_IGN69.copc.laz"


def tile_names(tiles: Iterable[tuple[int, int]]) -> list[str]:
    return [tile_name(tx, ty) for tx, ty in tiles]
