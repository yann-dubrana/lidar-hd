"""Resolving tile names to downloadable URLs.

IGN publishes LiDAR HD as ~224 delivery blocks (a zone code plus an acquisition
date). A tile lives in exactly one block, but nothing in the tile name says
which, so the block has to be discovered by probing.

Probing every tile against every block would be 9x the requests, so results are
cached: blocks are tried most-recently-successful first, which makes runs over a
contiguous area hit on the first try almost every time.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import DOWNLOAD_BASE, RESOURCE
from .http import get_bytes, head_size


@dataclass
class Catalog:
    """Block list plus a tile -> (block, size) cache."""

    blocks: list[str] = field(default_factory=list)
    resolved: dict[str, dict] = field(default_factory=dict)
    footprints: dict[str, tuple] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    # --- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(blocks=raw.get("blocks", []),
                       resolved=raw.get("resolved", {}),
                       footprints={k: tuple(v) for k, v in
                                   raw.get("footprints", {}).items()},
                       _order=raw.get("blocks", [])[:])
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"blocks": self.blocks, "resolved": self.resolved,
             "footprints": self.footprints}, indent=1),
            encoding="utf-8")

    # --- block list --------------------------------------------------------

    def fetch_blocks(self, progress=None) -> list[str]:
        """Read every delivery block, with its footprint, from the Atom feed."""
        if self.blocks:
            return self.blocks

        first = get_bytes(f"{RESOURCE}?page=1&pagesize=10").decode("utf-8", "replace")
        pages = int(re.search(r'gpf_dl:pagecount="(\d+)"', first).group(1))

        names: list[str] = []
        for page in range(1, pages + 1):
            body = first if page == 1 else get_bytes(
                f"{RESOURCE}?page={page}&pagesize=10").decode("utf-8", "replace")
            for entry in re.findall(r"<entry>.*?</entry>", body, re.S):
                title = re.search(r"<title>(NUALHD_[^<]+)</title>", entry)
                if not title:
                    continue
                name = title.group(1)
                names.append(name)
                poly = re.search(r"georss:polygon>([^<]*)", entry)
                if poly:
                    # georss here is lon lat despite the spec saying lat lon;
                    # verified against a known Brittany block.
                    vals = [float(v) for v in poly.group(1).split()]
                    lons, lats = vals[0::2], vals[1::2]
                    if lons and lats:
                        self.footprints[name] = (min(lons), min(lats),
                                                 max(lons), max(lats))
            if progress:
                progress(page, pages)

        self.blocks = names
        self._order = names[:]
        return names

    def prioritise(self, bbox_l93: tuple[float, float, float, float]) -> int:
        """Order blocks by whether their footprint covers a Lambert-93 bbox.

        Turns tile resolution from "probe up to 223 blocks" into "probe the
        handful that actually cover this area". Returns how many matched.
        """
        if not self.footprints:
            return 0
        try:
            from pyproj import Transformer
        except ImportError:
            return 0

        to_wgs = Transformer.from_crs(2154, 4326, always_xy=True)
        minx, miny, maxx, maxy = bbox_l93
        corners = [to_wgs.transform(x, y)
                   for x in (minx, maxx) for y in (miny, maxy)]
        lons = [c[0] for c in corners]
        lats = [c[1] for c in corners]
        w, s, e, n = min(lons), min(lats), max(lons), max(lats)

        def overlaps(fp) -> bool:
            return not (fp[2] < w or fp[0] > e or fp[3] < s or fp[1] > n)

        hits = [b for b in self.blocks if overlaps(self.footprints.get(b, (0, 0, 0, 0)))]
        rest = [b for b in self.blocks if b not in set(hits)]
        self._order = hits + rest
        return len(hits)

    # --- tile resolution ---------------------------------------------------

    def url(self, tile: str, block: str) -> str:
        return f"{DOWNLOAD_BASE}/{block}/{tile}"

    def resolve(self, tile: str) -> dict | None:
        """Find which block holds `tile`, returning {'block', 'bytes'}.

        Returns None if no block has it (outside LiDAR HD coverage).
        """
        hit = self.resolved.get(tile)
        if hit:
            return hit

        if not self.blocks:
            self.fetch_blocks()

        for block in list(self._order):
            size = head_size(self.url(tile, block))
            if size:
                entry = {"block": block, "bytes": size}
                self.resolved[tile] = entry
                # Contiguous areas share blocks: try this one first next time.
                self._order.remove(block)
                self._order.insert(0, block)
                return entry
        return None
