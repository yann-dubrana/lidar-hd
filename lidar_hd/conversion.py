"""Run one py3dtiles conversion without passing its inputs through the OS command line."""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path

from .config import GEOID_DIR, LAMBERT93_IGN69


def run_manifest(path: str) -> None:
    arguments = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(arguments, list) or not arguments or arguments[0] != "convert"
            or not all(isinstance(arg, str) for arg in arguments)):
        raise ValueError("Invalid conversion argument manifest")

    # Set before PROJ loads, and inherited by the py3dtiles workers that do the
    # reprojection: this is how they find the geoid grid.
    os.environ["PROJ_USER_WRITABLE_DIRECTORY"] = str(GEOID_DIR)
    if str(LAMBERT93_IGN69) in arguments:
        from pyproj import Transformer

        # PROJ without the grid copies altitudes unchanged instead of failing.
        lift = Transformer.from_crs(LAMBERT93_IGN69, 4979, always_xy=True).transform(700_000, 6_600_000, 0.0)[2]
        if not 30 < lift < 70:
            raise RuntimeError("PROJ cannot see the RAF20 geoid grid: the tileset would sit about 45 m too low")

    from py3dtiles.command_line import main

    original = sys.argv
    try:
        # argparse reads this in memory; no subprocess receives the expanded list.
        sys.argv = ["py3dtiles", *arguments]
        main()
    finally:
        sys.argv = original


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run_manifest(sys.argv[1])