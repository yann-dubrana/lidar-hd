import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lidar_hd import jobs, pipeline
from lidar_hd.areas import Area
from lidar_hd.config import MinioConfig


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.batch = [(Area("commune", "A", "001"), [(400, 6401), (401, 6401)]),
                      (Area("commune", "B", "002"), [(401, 6401), (402, 6401)])]

    def test_separate_is_default_and_merge_unions_tiles(self):
        self.assertEqual(jobs.prepare_batch(self.batch), self.batch)
        [(area, tiles)] = jobs.prepare_batch(self.batch, "La CUB")
        self.assertEqual((area.level, area.code, area.name), ("zone", "la-cub", "La CUB"))
        self.assertEqual(tiles, [(400, 6401), (401, 6401), (402, 6401)])
        self.assertEqual(len(area.members), 2)
        self.assertEqual(jobs.prepare_batch(self.batch[::-1], "La CUB"), [(area, tiles)])
        self.assertEqual(jobs.prepare_batch(self.batch * 2, "La CUB"), [(area, tiles)])

    def test_name_validation(self):
        self.assertEqual(jobs.zone_code("Bordeaux Métropole"), "bordeaux-metropole")
        for name in ("", "  ", "../elsewhere", "a\\b", "C:temp", "...", "x" * 81):
            with self.subTest(name=name), self.assertRaises(ValueError):
                jobs.prepare_batch(self.batch, name)
        with self.assertRaises(ValueError):
            jobs.prepare_batch([], "La CUB")
        with self.assertRaises(ValueError):
            jobs.prepare_batch([(self.batch[0][0], [])], "La CUB")

    @patch("lidar_hd.pipeline.download_tiles", return_value=pipeline.StageResult(ok=3))
    def test_manifest_allows_resume_but_rejects_changed_members_or_coverage(self, download):
        area, tiles = jobs.prepare_batch(self.batch, "La CUB")[0]
        options = jobs.Options(cleanup=False)
        jobs.run_area(area, tiles, self.base, Mock(), options)
        manifest = self.base / "zone-la-cub" / "zone.json"
        original = manifest.read_bytes()
        self.assertEqual(json.loads(original)["members"], [["commune", "001"], ["commune", "002"]])
        jobs.run_area(area, tiles, self.base, Mock(), options)
        self.assertEqual(download.call_count, 2)
        changed = [(Area("commune", "C", "003"), tiles)]
        changed_area, changed_tiles = jobs.prepare_batch(changed, "La CUB")[0]
        for a, t in ((changed_area, changed_tiles), (area, tiles[:-1])):
            with self.assertRaisesRegex(ValueError, "different selection"):
                jobs.run_area(a, t, self.base, Mock(), options)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(download.call_count, 2)

    def test_existing_unbound_directory_is_not_overwritten(self):
        root = self.base / "zone-la-cub"
        root.mkdir()
        output = root / "keep.txt"
        output.write_text("existing")
        area, tiles = jobs.prepare_batch(self.batch, "La CUB")[0]
        with self.assertRaisesRegex(ValueError, "no selection manifest"):
            jobs.run_area(area, tiles, self.base, Mock(), jobs.Options())
        self.assertEqual(output.read_text(), "existing")

    @patch("lidar_hd.ortho.export_pmtiles")
    @patch("lidar_hd.pipeline.convert_3dtiles", return_value=pipeline.StageResult(ok=3))
    @patch("lidar_hd.pipeline.download_tiles")
    def test_one_download_conversion_and_raster_export(self, download, convert, ortho):
        def receive(names, raw, *args, **kwargs):
            raw.mkdir(parents=True)
            for name in names:
                (raw / f"{name}.copc.laz").write_bytes(b"test input")
            return pipeline.StageResult(ok=len(names))

        def export(tiles, root, *args):
            archive = root / "ortho" / "orthophoto.pmtiles"
            archive.parent.mkdir()
            archive.write_bytes(b"test raster")
            return archive

        download.side_effect, ortho.side_effect = receive, export
        area, tiles = jobs.prepare_batch(self.batch, "La CUB")[0]
        jobs.run_area(area, tiles, self.base, Mock(),
                      jobs.Options(convert=True, ortho=True, cleanup=False))
        download.assert_called_once()
        self.assertEqual(len(download.call_args.args[0]), 3)
        convert.assert_called_once()
        self.assertEqual(len(convert.call_args.args[0]), 3)
        self.assertEqual(convert.call_args.args[1], self.base / "zone-la-cub" / "3dtiles")
        ortho.assert_called_once()
        self.assertEqual(ortho.call_args.args[0], tiles)

    @patch("lidar_hd.pipeline.upload_dir", return_value=pipeline.StageResult(ok=1))
    def test_named_upload_prefix_and_snowball_option(self, upload):
        area, tiles = jobs.prepare_batch(self.batch, "La CUB")[0]
        with patch("lidar_hd.pipeline.download_tiles", return_value=pipeline.StageResult()):
            jobs.run_area(area, tiles, self.base, Mock(), jobs.Options(cleanup=False))
        ortho = self.base / "zone-la-cub" / "ortho"
        ortho.mkdir()
        (ortho / "orthophoto.pmtiles").write_bytes(b"test archive")
        cfg = MinioConfig("host", "key", "secret", "bucket", prefix="")
        with patch("lidar_hd.jobs.minio_config", return_value=cfg):
            jobs.run_area(area, tiles, self.base, Mock(),
                          jobs.Options(download=False, upload=True, snowball=True, cleanup=False))
        self.assertEqual(upload.call_args.args[1], "zone-la-cub/ortho")
        self.assertTrue(upload.call_args.kwargs["snowball"])
        self.assertEqual(jobs.Options(download=False, cleanup=False, snowball=True).stages, [])


if __name__ == "__main__":
    unittest.main()