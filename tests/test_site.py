import tempfile
import unittest
from pathlib import Path

import laspy
import numpy as np

from lidar_hd import jobs, pipeline, site


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def write(self, name, x, y, *, epsg=None, point_format=2, z=None, classification=None):
        header = laspy.LasHeader(version="1.4" if point_format >= 6 else "1.2", point_format=point_format)
        header.scales, header.offsets = [0.001] * 3, [int(min(x)), int(min(y)), 0]
        if epsg:
            from pyproj import CRS
            header.add_crs(CRS.from_epsg(epsg))
        las = laspy.LasData(header)
        las.x, las.y, las.z = np.array(x), np.array(y), np.zeros(len(x)) if z is None else np.array(z)
        if classification is not None:
            las.classification = np.full(len(x), classification, dtype=np.uint8)
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

    def patch_of_ground(self, name, z, **kw):
        """A 20 m x 20 m patch with one point per metre, mid-cell."""
        x, y = np.meshgrid(650300.5 + np.arange(20), 6477100.5 + np.arange(20))
        return self.write(name, x.ravel(), y.ravel(), z=np.full(400, z), **kw)

    def checked(self, z, **kw):
        tile = self.patch_of_ground("LHD.copc.laz", 10.5, classification=2)
        source = self.patch_of_ground("scan.las", z, **kw)
        found, _ = jobs.plan_site(source)
        check, ground = site.survey(source, found.epsg, [tile], found.bbox)
        return source, tile, found, check, ground

    def test_altitude_is_checked_against_lidar_hd_ground(self):
        source, tile, found, check, ground = self.checked(10.5, classification=2)
        self.assertEqual((round(check.offset, 3), check.ground, check.cells), (0.0, True, 400))
        self.assertEqual(site.z_shift(check, found.bbox)[0], 0.0)

        lift = site.undulation(650310, 6477110)
        self.assertTrue(40 < lift < 60, lift)
        *_, check, _ = self.checked(10.5 + lift, classification=2)
        shift, verdict = site.z_shift(check, found.bbox)
        self.assertAlmostEqual(shift, -lift, places=2)
        self.assertIn("Ellipsoidal", verdict)

        *_, check, _ = self.checked(13.5, classification=2)
        with self.assertRaisesRegex(ValueError, r"\+3\.00 m"):
            site.z_shift(check, found.bbox)
        self.assertAlmostEqual(site.z_shift(check, found.bbox, align=True)[0], -3.0, places=3)

    def test_declared_ellipsoidal_heights_are_converted_once(self):
        from pyproj import CRS, Transformer

        lift = site.undulation(650310, 6477110)
        x, y = np.meshgrid(650300.5 + np.arange(20), 6477100.5 + np.arange(20))
        lon, lat = Transformer.from_crs(2154, 4326, always_xy=True).transform(x.ravel(), y.ravel())
        header = laspy.LasHeader(version="1.4", point_format=6)
        header.scales, header.offsets = [1e-8, 1e-8, 0.001], [lon.min(), lat.min(), 0]
        header.add_crs(CRS.from_epsg(4979))
        las = laspy.LasData(header)
        las.x, las.y, las.z = lon, lat, np.full(400, 10.5 + lift)
        las.classification = np.full(400, 2, dtype=np.uint8)
        las.write(str(self.base / "gnss.las"))

        found, _ = jobs.plan_site(self.base / "gnss.las")
        self.assertEqual(found.epsg, 4979)
        alone = laspy.read(str(site.prepare(found.source, self.base / "alone.las", found.epsg, found.bbox)))
        self.assertAlmostEqual(float(np.median(alone.z)), 10.5, delta=0.02)

        tile = self.patch_of_ground("LHD.copc.laz", 10.5, classification=2)
        check, _ = site.survey(found.source, found.epsg, [tile], found.bbox)
        shift, verdict = site.z_shift(check, found.bbox)
        self.assertEqual(shift, 0.0)                 # not lowered a second time
        self.assertIn("agrees", verdict)

        other = laspy.LasHeader(version="1.4", point_format=6)
        other.add_crs(CRS.from_user_input("EPSG:2154+5773"))          # EGM96 heights
        with self.assertRaisesRegex(ValueError, "EGM96"):
            site._reprojection(other, 2154)
        self.assertEqual(site._reprojection(other, 3945)(1.0, 2.0, 3.0)[2], 3.0)     # overridden

    def test_file_without_ground_is_reported_not_refused(self):
        *_, found, check, _ = self.checked(16.5)                 # a roof 6 m up, unclassified
        self.assertFalse(check.ground)
        shift, verdict = site.z_shift(check, found.bbox)
        self.assertEqual(shift, 0.0)
        self.assertIn("not verified", verdict)
        self.assertEqual(site.z_shift(None, found.bbox)[0], 0.0)

    def test_duplicated_lidar_points_go_and_the_file_gets_heights(self):
        source, _, found, check, ground = self.checked(16.5)
        prepared = site.prepare(source, self.base / "out.las", found.epsg, found.bbox, ground=ground)
        self.assertEqual(set(np.asarray(laspy.read(str(prepared)).height)), {600})
        self.assertTrue(pipeline._conversion_inputs([prepared]))

        # LiDAR HD has the same roof, the ground under it, and a noise point.
        x, y = np.meshgrid(650300.5 + np.arange(20), 6477100.5 + np.arange(20))
        x, y = np.tile(x.ravel(), 2), np.tile(y.ravel(), 2)
        z = np.r_[np.full(400, 16.5), np.full(400, 10.5)]
        tile = self.write("both.copc.laz", np.r_[x, 650310.5], np.r_[y, 6477110.5], z=np.r_[z, 80.0],
                          point_format=6, classification=np.r_[np.full(800, 2), 65])
        (kept,) = site.clip_tiles([tile], self.base / "clipped", found.bbox, site.occupied(prepared))
        self.assertEqual(set(np.asarray(laspy.read(str(kept)).z)), {10.5})
        (kept,) = site.clip_tiles([tile], self.base / "all", found.bbox)
        self.assertEqual(laspy.read(str(kept)).header.point_count, 800)

    def test_conversion_refuses_an_input_declared_outside_lambert93(self):
        utm = self.write("utm.las", [266735.0, 266835.0], [4989400.0, 4989500.0], epsg=32631)
        with self.assertRaisesRegex(ValueError, "not Lambert-93"):
            pipeline._conversion_inputs([utm])
        self.assertFalse(pipeline._conversion_inputs([self.base / "missing.laz"]))


if __name__ == "__main__":
    unittest.main()
