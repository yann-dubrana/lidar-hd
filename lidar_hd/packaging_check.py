"""Offline smoke workload for the actual frozen executable, including workers."""
from __future__ import annotations

import asyncio
import io
import json
import os
import ssl
import tempfile
from pathlib import Path
from unittest.mock import patch


async def _check_tui(root: Path) -> None:
    from main import LidarApp
    from textual.widgets import DataTable
    from .areas import Area

    with patch.dict(os.environ, {"LIDARHD_DATA": str(root)}), \
            patch("main.areas.browse", return_value=[Area("region", "Test area", "01")]):
        app = LidarApp()
        async with app.run_test(size=(140, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#results", DataTable).row_count == 1
            assert app.query_one("#overall-progress")


def run() -> None:
    import laspy
    import numpy as np
    from minio import Minio
    from PIL import Image
    from pmtiles.reader import MemorySource, Reader
    from pyproj import Transformer
    from . import ortho, pipeline
    from .config import application_root

    assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0
    Minio("localhost:9000", access_key="self-test", secret_key="self-test")
    lon, lat = Transformer.from_crs(2154, 4326, always_xy=True).transform(420500, 6439500)
    assert -2 < lon < 0 and 44 < lat < 46

    with tempfile.TemporaryDirectory(prefix="self-test-", dir=application_root()) as directory:
        root = Path(directory)
        asyncio.run(_check_tui(root))

        header = laspy.LasHeader(point_format=3, version="1.2")
        header.offsets = [420000, 6439000, 0]
        header.scales = [0.01, 0.01, 0.01]
        points = laspy.LasData(header)
        points.x = 420000 + np.arange(32, dtype=float)
        points.y = 6439000 + np.arange(32, dtype=float)
        points.z = np.full(32, 50.0)
        points.red = np.full(32, 65535, dtype=np.uint16)
        points.classification = np.full(32, 2, dtype=np.uint8)
        source = root / "sample.laz"
        points.write(source)
        assert len(laspy.read(source).points) == 32
        result = pipeline.convert_3dtiles([source], root / "3dtiles", jobs=1)
        if result.failed:
            raise RuntimeError(f"Bundled conversion failed: {result.failed}")
        tileset = json.loads((root / "3dtiles" / "tileset.json").read_text())
        assert tileset["root"]["boundingVolume"]
        assert list((root / "3dtiles").rglob("*.pnts"))

        image = io.BytesIO()
        Image.new("RGB", (32, 32), (180, 70, 30)).save(image, format="JPEG")
        with patch.object(ortho, "ORTHO_PX", 32), \
                patch.object(ortho, "get_bytes", return_value=image.getvalue()):
            archive = ortho.export_pmtiles([(420, 6440)], root, lambda *args: None, lambda: False)
        reader = Reader(MemorySource(archive.read_bytes()))
        assert reader.header()["addressed_tiles_count"] > 0
        assert reader.metadata()["source_manifest"]["tiles"] == [[420, 6440]]
    print("Self-test passed: TUI, TLS, LAZ, PROJ, 3D Tiles worker and raster PMTiles.")