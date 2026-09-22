import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import laspy
import numpy as np
from PIL import Image

from lidar_hd import pipeline


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