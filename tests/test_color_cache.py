import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import laspy
import numpy as np
from PIL import Image

from lidar_hd import pipeline
from lidar_hd.config import CLASS_COLORS, COLOR_VERSION


class ColorCacheTests(unittest.TestCase):
    def test_colorization_reuses_and_keeps_orthophoto(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            src, dst = root / "source.laz", root / "colorized.laz"
            hdr = laspy.LasHeader(point_format=6, version="1.4")
            las = laspy.LasData(hdr)
            las.x = np.array([400250.0, 400750.0])
            las.y = np.array([6400750.0, 6400250.0])
            las.z = np.array([10.0, 11.0])
            las.write(src)
            cached = root / "ortho.png"
            image = Image.new("RGB", (2, 2), (0, 0, 0))
            image.putpixel((0, 0), (200, 100, 50))
            image.putpixel((1, 1), (25, 75, 125))
            image.save(cached)
            with patch("lidar_hd.ortho.get_ortho", return_value=cached) as get_ortho:
                count = pipeline.colorize_tile(src, dst, ortho_cache=root / "cache")
                get_ortho.assert_called_once_with(400000, 6400000, root / "cache")
            self.assertEqual(count, 2)
            self.assertTrue(cached.is_file())
            result = laspy.read(dst)
            np.testing.assert_array_equal(result.red, np.array([200, 25]) * 257)
            np.testing.assert_array_equal(result.green, np.array([100, 75]) * 257)
            np.testing.assert_array_equal(result.blue, np.array([50, 125]) * 257)

    def test_hidden_points_take_class_colour_and_height(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            src, dst = root / "source.laz", root / "colorized.laz"
            # name: (x offset, z, class). Same x = same occlusion cell.
            points = {
                "roof": (100, 10.0, 6), "wall": (100, 3.0, 6), "floor": (100, 0.0, 2),
                "tree": (200, 8.0, 5), "trunk": (200, 4.0, 5), "shaded": (200, 0.0, 2),
                "wire": (300, 12.0, 1), "open": (300, 0.0, 2),
                "noise": (400, 500.0, 65),
            }
            las = laspy.LasData(laspy.LasHeader(point_format=6, version="1.4"))
            las.x = 400000.0 + np.array([p[0] for p in points.values()])
            las.y = np.full(len(points), 6400500.0)
            las.z = np.array([p[1] for p in points.values()])
            las.classification = np.array([p[2] for p in points.values()], dtype=np.uint8)
            las.intensity = np.full(len(points), 1000, dtype=np.uint16)
            las.write(src)
            cached = root / "ortho.png"
            Image.new("RGB", (2, 2), (255, 0, 0)).save(cached)
            with patch("lidar_hd.ortho.get_ortho", return_value=cached):
                count = pipeline.colorize_tile(src, dst, ortho_cache=root / "cache")

            self.assertEqual(count, len(points) - 1)                  # noise dropped
            result = laspy.read(dst)
            index = {name: i for i, name in enumerate(n for n in points if n != "noise")}
            colour = lambda name: tuple(int(c[index[name]]) // 257     # noqa: E731
                                        for c in (result.red, result.green, result.blue))
            for name in ("roof", "tree", "trunk", "wire", "open"):
                self.assertEqual(colour(name), (255, 0, 0), name)
            # Uniform intensity = full shade, so the palette comes through as is.
            self.assertEqual(colour("wall"), CLASS_COLORS[6])
            self.assertEqual(colour("floor"), CLASS_COLORS[2])
            self.assertEqual(colour("shaded"), CLASS_COLORS[2])
            heights = np.asarray(result.height)
            self.assertEqual([int(heights[index[n]]) for n in ("roof", "wall", "floor", "wire")],
                             [1000, 300, 0, 1200])

    @patch("lidar_hd.pipeline.colorize_tile", return_value=2)
    def test_batch_redoes_tiles_from_an_older_version(self, colorize):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            raw, out = root / "raw", root / "out"
            raw.mkdir()
            out.mkdir()
            for name in ("old.copc.laz", "new.copc.laz"):
                (raw / name).write_bytes(b"input")

            def write(dst, stamp):
                header = laspy.LasHeader(point_format=7, version="1.4")
                header.generating_software = stamp
                laspy.LasData(header).write(str(dst))

            write(out / "old.laz", f"lidar-hd colour {COLOR_VERSION - 1}")
            write(out / "new.laz", pipeline._COLOR_STAMP)
            result = pipeline.colorize_all(raw, out, ortho_cache=root)
            self.assertEqual((result.ok, result.skipped), (1, 1))
            self.assertEqual(colorize.call_args.args[0].name, "old.copc.laz")

    @patch("lidar_hd.pipeline.colorize_tile", return_value=2)
    def test_batch_forwards_cache(self, colorize):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            raw, out, cache = root / "raw", root / "out", root / "cache"
            raw.mkdir()
            (raw / "tile.copc.laz").write_bytes(b"input")
            out.mkdir()

            def write_output(src, dst, *, ortho_cache):
                self.assertEqual(ortho_cache, cache)
                dst.write_bytes(b"output")
                return 2

            colorize.side_effect = write_output
            result = pipeline.colorize_all(raw, out, ortho_cache=cache)
            self.assertEqual(result.ok, 1)
            self.assertFalse(result.failed)


if __name__ == "__main__":
    unittest.main()