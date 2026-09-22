"""Administrative areas and the LiDAR HD tile grid.

Communes, departments and regions come from IGN ADMIN EXPRESS via WFS in
Lambert-93. EPCI come from API Geo (https://geo.api.gouv.fr/decoupage-administratif/epcis),
whose WGS84 GeoJSON contours are reprojected to the same LiDAR HD tile grid.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil, isfinite
from typing import Iterable, Literal
from urllib.parse import urlencode

from .http import get_json, wfs_url

Level = Literal["commune", "epci", "departement", "region"]

LEVELS: dict[Level, str] = {
    "commune": "Commune",
    "epci": "Intermunicipal authority (EPCI)",
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
    """Find areas by name prefix; EPCI use API Geo name matching or a SIREN."""
    if level == "epci":
        term = term.strip()
        if not term or limit <= 0:
            return []
        key = "code" if _epci_code(term) else "nom"
        areas = _epci_list(**{key: term, "limit": limit})
        areas.sort(key=lambda a: a.name.casefold() != term.casefold())
        return areas[:limit]

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


def browse(level: Level) -> list[Area]:
    """List EPCI, regions or departments alphabetically, without geometry.

    Communes must be searched rather than bulk loaded.
    """
    if level == "epci":
        # Without a name filter /epcis returns the entire, unpaginated list.
        return sorted(_epci_list(), key=lambda a: (a.name.casefold(), a.code))
    if level == "commune":
        return []
    if level not in LEVELS:
        raise ValueError(f"unknown administrative level: {level}")

    count = 1000
    url = wfs_url(level, count=count, properties="nom_officiel,code_insee")
    areas = []
    offset = 0
    while True:
        data = get_json(f"{url}&STARTINDEX={offset}&SORTBY=nom_officiel,code_insee")
        features = data.get("features") or []
        if not features:
            break
        for feat in features:
            p = feat["properties"]
            areas.append(Area(level, p.get("nom_officiel", "?"), p.get("code_insee", "?")))
        offset += len(features)
        matched = data.get("numberMatched")
        if matched is not None and str(matched).isdigit():
            if offset >= int(matched):
                break
        elif len(features) < count:
            break
    areas.sort(key=lambda a: (a.name.casefold(), a.code))
    return areas


def geometry(area: Area) -> dict:
    """Fetch an area's polygon in Lambert-93 (EPSG:2154)."""
    if area.level == "epci":
        return _epci_geometry(area)
    data = get_json(wfs_url(area.level, cql=f"code_insee='{area.code}'"))
    feats = data.get("features") or []
    if not feats:
        raise LookupError(f"no geometry for {area.label}")
    return feats[0]["geometry"]


def _epci_code(code: str) -> bool:
    """API Geo's EPCI code is the nine-digit SIREN, not a commune INSEE code."""
    return isinstance(code, str) and len(code) == 9 and code.isascii() and code.isdecimal()


def _epci_list(**params: str | int) -> list[Area]:
    query = urlencode({"fields": "nom,code", **params})
    data = get_json(f"https://geo.api.gouv.fr/epcis?{query}")
    if not isinstance(data, list):
        raise ValueError("invalid EPCI list from API Geo")
    areas = {}
    for record in data:
        if not isinstance(record, dict):
            continue
        name, code = record.get("nom"), record.get("code")
        if isinstance(name, str) and name.strip() and _epci_code(code):
            areas.setdefault(code, Area("epci", name.strip(), code))
    return list(areas.values())


def _epci_geometry(area: Area) -> dict:
    from pyproj import Transformer
    from pyproj.exceptions import ProjError

    if not _epci_code(area.code):
        raise ValueError(f"invalid EPCI SIREN: {area.code}")
    query = urlencode({"fields": "nom,code", "format": "geojson", "geometry": "contour"})
    data = get_json(f"https://geo.api.gouv.fr/epcis/{area.code}?{query}")
    geom = data.get("geometry") if isinstance(data, dict) else None
    if not isinstance(geom, dict) or not geom.get("coordinates"):
        raise LookupError(f"no geometry for {area.label}")
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"invalid polygon geometry for {area.label}")

    # API Geo uses WGS84; GeoJSON positions are longitude, latitude, regardless
    # of EPSG:4326's formal axis order. Keep holes and separate islands intact.
    transformer = Transformer.from_crs(4326, 2154, always_xy=True)

    def project_ring(ring: list) -> list:
        if not isinstance(ring, list) or len(ring) < 4 or ring[0][:2] != ring[-1][:2]:
            raise ValueError("invalid polygon ring")
        result = []
        for point in ring:
            lon, lat = point[:2]
            if not (isfinite(lon) and isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("invalid WGS84 position")
            x, y = transformer.transform(lon, lat, errcheck=True)
            if not (isfinite(x) and isfinite(y)):
                raise ValueError("invalid Lambert-93 position")
            result.append([x, y])
        return result

    try:
        projected = []
        for polygon in _rings(geom):
            if not isinstance(polygon, list) or not polygon:
                raise ValueError("empty polygon")
            projected.append([project_ring(ring) for ring in polygon])
    except (TypeError, ValueError, ProjError) as exc:
        raise ValueError(f"invalid polygon geometry for {area.label}: {exc}") from exc
    return {"type": geom["type"], "coordinates": projected[0] if geom["type"] == "Polygon" else projected}


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
    """The 1 km tiles with positive-area overlap, as Lambert-93 (x_km, y_km).

    Scanline bands split at every vertex and grid row preserve thin slivers
    without sampling. Within each band a valid polygon's edge order is fixed;
    paired edges bound trapezoids whose horizontal extents give covered tiles.

    Tiles are named by their NW corner, so tile (tx, ty) covers
    x in [tx, tx+1) km and y in [ty-1, ty) km.
    """
    if geom["type"] == "MultiPolygon":
        # Union each polygon's tiles, not its scanline crossings: overlapping
        # components must not cancel one another through even/odd filling.
        return sorted({tile for polygon in geom["coordinates"]
                       for tile in tiles_for({"type": "Polygon", "coordinates": polygon})})
    polys = _rings(geom)

    edges: list[tuple[float, float, float, float]] = []
    ys: list[float] = []
    for poly in polys:
        for ring in poly:
            n = len(ring)
            for i in range(n):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                ys.append(y1)
                if y1 != y2:                     # horizontal edges never cross
                    edges.append((x1, y1, x2, y2))

    if not edges:
        return []

    # Bucket edges by kilometre row so each band only tests nearby edges.
    buckets: dict[int, list] = defaultdict(list)
    for e in edges:
        lo = int(min(e[1], e[3]) // 1000)
        hi = ceil(max(e[1], e[3]) / 1000)
        for row in range(lo, hi):
            buckets[row].append(e)

    y0 = int(min(ys) // 1000)
    y1 = ceil(max(ys) / 1000)

    def x_at(edge, y):
        x1, ey1, x2, ey2 = edge
        if y == ey1:
            return x1
        if y == ey2:
            return x2
        return x1 + (x2 - x1) * (y - ey1) / (ey2 - ey1)

    covered: set[tuple[int, int]] = set()
    for ty in range(y0 + 1, y1 + 1):
        cell_lo, cell_hi = (ty - 1) * 1000, ty * 1000
        nearby = buckets.get(ty - 1, ())
        cuts = sorted({cell_lo, cell_hi} | {
            y for edge in nearby for y in (edge[1], edge[3]) if cell_lo < y < cell_hi})
        for lower, upper in zip(cuts, cuts[1:]):
            crossings = []
            for edge in nearby:
                if min(edge[1], edge[3]) <= lower and max(edge[1], edge[3]) >= upper:
                    bottom, top = x_at(edge, lower), x_at(edge, upper)
                    crossings.append(((bottom + top) / 2, bottom, top))
            crossings.sort()
            for i in range(0, len(crossings) - 1, 2):
                left, right = crossings[i], crossings[i + 1]
                if left[0] >= right[0]:
                    continue
                xa, xb = min(left[1:]), max(right[1:])
                for tx in range(int(xa // 1000), ceil(xb / 1000)):
                    covered.add((tx, ty))

    return sorted(covered)


def tile_name(tx: int, ty: int) -> str:
    """The IGN filename for a tile, e.g. LHD_FXX_0404_6420_PTS_LAMB93_IGN69."""
    return f"LHD_FXX_{tx:04d}_{ty:04d}_PTS_LAMB93_IGN69.copc.laz"


def tile_names(tiles: Iterable[tuple[int, int]]) -> list[str]:
    return [tile_name(tx, ty) for tx, ty in tiles]
