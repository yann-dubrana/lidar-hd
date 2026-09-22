from __future__ import annotations

import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image
from pmtiles.reader import MemorySource, Reader, all_tiles
from pyproj import Transformer

from lidar_hd import ortho


def jpeg(color=(180, 70, 30), size=32):
    out = io.BytesIO()
    Image.new("RGB", (size, size), color).save(out, format="JPEG", quality=100)
    return out.getvalue()


class OrthoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pixels = patch.object(ortho, "ORTHO_PX", 32)
        self.pixels.start()
        self.addCleanup(self.pixels.stop)
        self.http = patch.object(ortho, "get_bytes", return_value=jpeg()).start()
        self.addCleanup(patch.stopall)
        self.events = []

    def export(self, tiles=None, stop=lambda: False):
        return ortho.export_pmtiles(tiles or [(420, 6440)], self.root,
                                   lambda *args: self.events.append(args), stop)

    def test_cache_request_validation_and_reuse(self):
        path = ortho.get_ortho(420000, 6439000, self.root / "cache")
        query = parse_qs(urlparse(self.http.call_args.args[0]).query)
        self.assertEqual(query["BBOX"], ["420000,6439000,421000,6440000"])
        self.assertEqual(query["LAYERS"], ["HR.ORTHOIMAGERY.ORTHOPHOTOS"])
        self.assertEqual(query["WIDTH"], ["32"])
        self.assertEqual(query["CRS"], ["EPSG:2154"])
        self.assertEqual(ortho.get_ortho(420000, 6439000, path.parent), path)
        self.assertEqual(self.http.call_count, 1)
        path.write_bytes(b"broken")
        ortho.get_ortho(420000, 6439000, path.parent)
        self.assertEqual(self.http.call_count, 2)
        self.assertFalse(list(path.parent.glob("*.part")))

    def test_bad_response_not_cached(self):
        for response in (b"<ServiceException>bad</ServiceException>", jpeg(size=16)):
            self.http.return_value = response
            with self.assertRaises(ValueError):
                ortho.get_ortho(420000, 6439000, self.root / "cache")
        self.assertFalse(list((self.root / "cache").glob("*.jpg")))

    def test_archive_geolocation_alpha_and_metadata(self):
        path = self.export()
        self.assertEqual(path, self.root / "ortho" / "orthophoto.pmtiles")
        source = MemorySource(path.read_bytes())
        reader = Reader(source)
        header, metadata = reader.header(), reader.metadata()
        self.assertEqual(header["min_zoom"], 0)
        self.assertEqual(header["max_zoom"], 12)
        self.assertIn("IGN", metadata["attribution"])
        self.assertIn("Licence Ouverte", metadata["attribution"])
        self.assertEqual(metadata["source_gsd_m"], 31.25)
        self.assertEqual(metadata["source_manifest"]["tiles"], [[420, 6440]])
        mercator = Transformer.from_crs(2154, 3857, always_xy=True)
        lonlat = Transformer.from_crs(2154, 4326, always_xy=True)
        lon, lat = lonlat.transform(420500, 6439500)
        self.assertLess(header["min_lon_e7"] / 1e7, lon)
        self.assertGreater(header["max_lat_e7"] / 1e7, lat)
        mx, my = mercator.transform(420500, 6439500)
        span = 2 * math.pi * 6378137 / 2 ** header["max_zoom"]
        origin = math.pi * 6378137
        fx, fy = (mx + origin) / span, (origin - my) / span
        x, y = math.floor(fx), math.floor(fy)
        with Image.open(io.BytesIO(reader.get(header["max_zoom"], x, y))) as image:
            rgba = np.asarray(image)
            pixel = rgba[int((fy - y) * 256), int((fx - x) * 256)]
        np.testing.assert_allclose(pixel, [180, 70, 30, 255], atol=2)
        saw_transparent = False
        for (z, x, y), data in all_tiles(source):
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, (256, 256))
                self.assertEqual(image.mode, "RGBA")
                if z == header["max_zoom"]:
                    alpha = np.asarray(image)[:, :, 3]
                    self.assertTrue(alpha.any())
                    saw_transparent |= bool((alpha == 0).any())
        self.assertTrue(saw_transparent)
        self.assertTrue(all(e[0] == "ortho" for e in self.events))
        self.assertEqual(self.events[-1][1], self.events[-1][2])
        self.assertEqual(len(list((self.root / "ortho").iterdir())), 1)

    def test_resume_selection_change_and_deduplication(self):
        path = self.export()
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        self.export([(420, 6440), (420, 6440)])
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(self.http.call_count, 1)
        self.export([(421, 6440)])
        self.assertNotEqual(path.read_bytes(), before)
        self.assertEqual(self.http.call_count, 2)

    def test_sparse_gap_is_not_filled(self):
        path = self.export([(420, 6440), (450, 6440)])
        reader = Reader(MemorySource(path.read_bytes()))
        z = reader.header()["max_zoom"]
        mx, my = Transformer.from_crs(2154, 3857, always_xy=True).transform(435500, 6439500)
        half = math.pi * 6378137
        x = int((mx + half) / (2 * half) * 2 ** z)
        y = int((half - my) / (2 * half) * 2 ** z)
        self.assertIsNone(reader.get(z, x, y))
        self.assertLess(reader.header()["addressed_tiles_count"], 60)

    def test_image_orientation_black_pixels_and_adjacent_coverage(self):
        image = Image.new("RGB", (32, 32), (0, 0, 0))
        image.paste((220, 20, 20), (0, 0, 16, 16))
        image.paste((20, 220, 20), (16, 0, 32, 16))
        image.paste((20, 20, 220), (0, 16, 16, 32))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=100, subsampling=0)
        self.http.side_effect = [out.getvalue(), jpeg((130, 130, 130))]
        path = self.export([(420, 6440), (421, 6440)])
        reader = Reader(MemorySource(path.read_bytes()))
        z = reader.header()["max_zoom"]
        projection = Transformer.from_crs(2154, 3857, always_xy=True)
        half = math.pi * 6378137
        for east, north, expected in (
            (420250, 6439750, [220, 20, 20, 255]),
            (420750, 6439750, [20, 220, 20, 255]),
            (420250, 6439250, [20, 20, 220, 255]),
            (420750, 6439250, [0, 0, 0, 255]),
            (421500, 6439500, [130, 130, 130, 255]),
        ):
            mx, my = projection.transform(east, north)
            fx, fy = (mx + half) / (2 * half) * 2 ** z, (half - my) / (2 * half) * 2 ** z
            x, y = int(fx), int(fy)
            with Image.open(io.BytesIO(reader.get(z, x, y))) as tile:
                pixel = tile.getpixel((int((fx - x) * 256), int((fy - y) * 256)))
            np.testing.assert_allclose(pixel, expected, atol=3)
        for north in range(6439100, 6439900, 100):
            mx, my = projection.transform(421000, north)
            fx, fy = (mx + half) / (2 * half) * 2 ** z, (half - my) / (2 * half) * 2 ** z
            with Image.open(io.BytesIO(reader.get(z, int(fx), int(fy)))) as tile:
                self.assertEqual(tile.getpixel((int(fx % 1 * 256), int(fy % 1 * 256)))[3], 255)

    def test_failure_and_cancellation_preserve_completed_archive(self):
        path = self.export()
        before = path.read_bytes()
        self.http.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.export([(421, 6440)])
        self.assertEqual(path.read_bytes(), before)
        self.http.side_effect = None
        with self.assertRaises(InterruptedError):
            self.export([(421, 6440)], stop=lambda: True)
        self.assertEqual(path.read_bytes(), before)
        count = 0

        def stop_during_build():
            nonlocal count
            count += 1
            return count > 7

        with self.assertRaises(InterruptedError):
            self.export([(421, 6440)], stop=stop_during_build)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list((self.root / "ortho-cache").glob(".build-*")))
        self.assertTrue(list((self.root / "ortho-cache").glob("*.jpg")))

    def test_truncated_archive_is_rebuilt(self):
        path = self.export()
        path.write_bytes(path.read_bytes()[:200])
        self.export()
        self.assertGreater(path.stat().st_size, 200)
        self.assertEqual(self.http.call_count, 1)

    def test_empty_selection_rejected(self):
        with self.assertRaises(ValueError):
            ortho.export_pmtiles([], self.root, lambda *a: None, lambda: False)


if __name__ == "__main__":
    unittest.main()