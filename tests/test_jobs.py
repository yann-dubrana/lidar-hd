import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lidar_hd import jobs, pipeline, site
from lidar_hd.areas import Area
from lidar_hd.config import MinioConfig


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.area = Area("commune", "Example", "12345")
        self.root = self.base / "commune-12345"
        self.catalog = Mock()

    def options(self, **kw):
        return jobs.Options(download=False, cleanup=False, **kw)

    def test_no_stages_no_work(self):
        result = jobs.run_area(self.area, [(400, 6401)], self.base, self.catalog, self.options())
        self.assertEqual(result, {})
        self.catalog.fetch_blocks.assert_not_called()

    def test_missing_raw_reports_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "Enable Download"):
            jobs.run_area(self.area, [(400, 6401)], self.base, self.catalog,
                          self.options(colorize=True))
        self.catalog.fetch_blocks.assert_not_called()

    @patch("lidar_hd.pipeline.convert_3dtiles", return_value=pipeline.StageResult(ok=1))
    def test_convert_alone_uses_existing_colorized(self, convert):
        source = self.root / "colorized" / "tile.laz"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"local input")
        jobs.run_area(self.area, [(400, 6401)], self.base, self.catalog,
                      self.options(convert=True))
        self.assertEqual(convert.call_args.args[0], [source])
        self.catalog.fetch_blocks.assert_not_called()

    @patch("lidar_hd.jobs.minio_config", return_value=MinioConfig("host", "key", "secret", "bucket"))
    @patch("lidar_hd.pipeline.upload_dir", return_value=pipeline.StageResult(ok=1))
    def test_upload_includes_pmtiles_and_3dtiles(self, upload, config):
        tiles = self.root / "3dtiles"
        tiles.mkdir(parents=True)
        (tiles / "tileset.json").write_text("{}")
        (tiles / "tile.pnts").write_bytes(b"pnts")
        raster = self.root / "ortho"
        raster.mkdir()
        (raster / "orthophoto.pmtiles").write_bytes(b"archive")
        result = jobs.run_area(self.area, [(400, 6401)], self.base, self.catalog,
                               self.options(upload=True))
        self.assertEqual([c.args[0] for c in upload.call_args_list], [tiles, raster])
        self.assertEqual(upload.call_args.args[1], "lidar-hd/commune-12345/ortho")
        self.assertEqual(result["upload"].ok, 2)

    @patch("lidar_hd.site.prepare", side_effect=lambda source, dst, epsg, bbox, **kw: dst)
    @patch("lidar_hd.pipeline.convert_3dtiles", return_value=pipeline.StageResult(ok=1))
    def test_enriched_file_alone_always_converts(self, convert, prepare):
        source = self.base / "in" / "Scan.las"
        source.parent.mkdir()
        source.write_bytes(b"las")
        found = jobs.Site("Scan", "scan", source, 3945, (0, 0, 1, 1), (0, 0))
        jobs.run_area(found, [], self.base, self.catalog, jobs.Options(cleanup=False))
        self.assertEqual(convert.call_args.args[0], [self.base / "site-scan" / "enriched" / "scan.las"])
        self.catalog.fetch_blocks.assert_not_called()

        other = self.base / "elsewhere" / "Scan.las"
        other.parent.mkdir()
        other.write_bytes(b"las")
        with self.assertRaisesRegex(ValueError, "Rename"):
            jobs.run_area(jobs.Site("Scan", "scan", other, 3945, (0, 0, 1, 1), (0, 0)), [], self.base,
                          self.catalog, jobs.Options(cleanup=False))

    @patch("lidar_hd.pipeline.convert_3dtiles", return_value=pipeline.StageResult(ok=1))
    @patch("lidar_hd.site.clip_tiles", return_value=[])
    @patch("lidar_hd.site.occupied", return_value="voxels")
    @patch("lidar_hd.site.prepare", side_effect=lambda source, dst, epsg, bbox, **kw: dst)
    @patch("lidar_hd.site.survey", return_value=(site.ZCheck(3.0, 0.1, 400, True), "ground"))
    def test_enriched_file_is_checked_before_it_replaces_lidar_points(self, survey, prepare, occupied,
                                                                      clip, convert):
        source = self.base / "in" / "Scan.las"
        source.parent.mkdir()
        source.write_bytes(b"las")
        tile = self.base / "site-scan" / "raw" / "LHD.copc.laz"
        tile.parent.mkdir(parents=True)
        tile.touch()
        found = jobs.Site("Scan", "scan", source, 2154, (0, 0, 1, 1), (0, 0), clip=(-5, -5, 6, 6))

        with self.assertRaisesRegex(ValueError, "z-align"):
            jobs.run_area(found, [], self.base, self.catalog, self.options())
        prepare.assert_not_called()

        jobs.run_area(found, [], self.base, self.catalog, self.options(z_align=True))
        self.assertEqual(prepare.call_args.kwargs, {"shift": -3.0, "ground": "ground"})
        self.assertEqual(clip.call_args.args[3], "voxels")
        self.assertEqual(convert.call_args.args[0], [self.base / "site-scan" / "enriched" / "scan.las"])

        jobs.run_area(found, [], self.base, self.catalog, self.options(z_align=True, keep_overlap=True))
        self.assertIsNone(clip.call_args.args[3])

    @patch("lidar_hd.pipeline.download_tiles", return_value=pipeline.StageResult(ok=1))
    def test_download_passes_transfer_callback(self, download):
        transfer = Mock()
        result = jobs.run_area(self.area, [(400, 6401)], self.base, self.catalog,
                               jobs.Options(cleanup=False), transfer_progress=transfer)
        self.assertEqual(result["download"].ok, 1)
        self.assertIs(download.call_args.kwargs["transfer_progress"], transfer)
        self.catalog.save.assert_called_once()

    @patch("lidar_hd.pipeline.download_tiles", return_value=pipeline.StageResult(ok=1))
    @patch("lidar_hd.pipeline.cleanup")
    def test_stop_does_not_cleanup(self, cleanup, download):
        stopped = False

        def progress(stage, done, total, detail):
            nonlocal stopped
            if done == 1:
                stopped = True

        jobs.run_area(self.area, [(400, 6401)], self.base, self.catalog,
                      jobs.Options(), progress, lambda: stopped)
        cleanup.assert_not_called()

    @patch("lidar_hd.pipeline.download_tiles", return_value=pipeline.StageResult(failed=["tile"]))
    @patch("lidar_hd.pipeline.cleanup")
    def test_failure_preserves_inputs(self, cleanup, download):
        results = jobs.run_area(self.area, [(400, 6401)], self.base, self.catalog, jobs.Options())
        cleanup.assert_not_called()
        self.assertEqual(results["cleanup"].skipped, 1)


if __name__ == "__main__":
    unittest.main()