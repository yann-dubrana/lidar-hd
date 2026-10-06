import tempfile
import unittest
from pathlib import Path

import laspy
import numpy as np

from lidar_hd import jobs, site


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def write(self, name, x, y, *, epsg=None, point_format=2):
        header = laspy.LasHeader(version="1.2", point_format=point_format)
        header.scales, header.offsets = [0.001] * 3, [int(min(x)), int(min(y)), 0]
        if epsg:
            from pyproj import CRS
            header.add_crs(CRS.from_epsg(epsg))
        las = laspy.LasData(header)
        las.x, las.y, las.z = np.array(x), np.array(y), np.zeros(len(x))
        las.intensity = np.arange(len(x)) * 100
        las.write(str(self.base / name))
        return self.base / name

    def test_utm_file_lands_in_lambert93_with_surrounding_tiles(self):
        # Two points 100 m apart near Coutras, declared in UTM 31N.
        source = self.write("Point Cloud.las", [266735.0, 266835.0], [4989400.0, 4989500.0], epsg=32631)
        found, tiles = jobs.plan_site(f'"{source}"', buffer=50)
        self.assertEqual((found.epsg, found.code), (32631, "point-cloud"))
        prepared = site.prepare(source, self.base / "out.las", found.epsg, found.bbox)
        with laspy.open(str(prepared)) as f:
            mins, maxs = f.header.mins, f.header.maxs
        # The footprint is the reprojected header box: a few cm wider than the points.
        self.assertAlmostEqual(mins[0], found.bbox[0], delta=1)
        self.assertAlmostEqual(maxs[1], found.bbox[3], delta=1)
        # Every tile touches the 50 m perimeter, and the file's own tile is among them.
        own = (int(found.bbox[0] // 1000), int(found.bbox[1] // 1000) + 1)
        self.assertIn(own, tiles)
        self.assertLessEqual(len(tiles), 4)

    def test_undeclared_crs_is_guessed_or_refused(self):
        lambert = self.write("dam.las", [650333.0, 650376.0], [6477130.0, 6477201.0], point_format=0)
        conic = self.write("indoor.las", [1427402.0, 1427432.0], [4197175.0, 4197238.0])
        unknown = self.write("local.las", [0.0, 10.0], [0.0, 10.0])
        self.assertEqual(jobs.plan_site(lambert)[0].epsg, 2154)
        found, tiles = jobs.plan_site(conic)
        self.assertEqual((found.epsg, found.clip, tiles), (3945, None, []))
        with self.assertRaisesRegex(ValueError, "EPSG"):
            jobs.plan_site(unknown)

    def test_file_without_colour_gets_grey_and_tiles_are_cropped(self):
        source = self.write("dam.las", [650333.0, 650376.0], [6477130.0, 6477201.0], point_format=0)
        found, _ = jobs.plan_site(source)
        with laspy.open(str(site.prepare(source, self.base / "grey.las", found.epsg, found.bbox))) as f:
            points = f.read()
        self.assertEqual(list(points.red), [0, int(100 / 255 * 65535)])

        tile = self.write("LHD.copc.laz", [650000.0, 650350.0], [6477000.0, 6477150.0], point_format=0)
        (kept,) = site.clip_tiles([tile], self.base / "clipped", found.bbox)
        self.assertEqual(laspy.read(str(kept)).header.point_count, 1)
        self.assertEqual(site.clip_tiles([tile], self.base / "none", (0, 0, 1, 1)), [])


if __name__ == "__main__":
    unittest.main()
