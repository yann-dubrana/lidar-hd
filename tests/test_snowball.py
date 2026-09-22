import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from minio import Minio

from lidar_hd import pipeline


ROOT = Path(__file__).resolve().parents[1]


class SnowballTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = SimpleNamespace(endpoint="localhost:9000", access_key="key",
                                   secret_key="secret", secure=False, bucket="bucket")
        self.client = Minio(self.cfg.endpoint, access_key="key", secret_key="secret", secure=False)
        self.remote = {}
        self.archives = []
        self.staging = []
        self.normal = []
        self.events = []
        self.extract = True
        self.stop = False
        self.after_put = lambda: None
        self.client.bucket_exists = Mock(return_value=True)
        self.client.make_bucket = Mock()
        self.client.stat_object = Mock(side_effect=self.stat)
        self.client._execute = Mock(side_effect=AssertionError("network forbidden"))
        self.client._put_object = Mock(side_effect=self.put)
        self.client._create_multipart_upload = Mock(side_effect=AssertionError("multipart forbidden"))
        self.client.remove_object = Mock(side_effect=AssertionError("remote deletion forbidden"))
        upload = self.client.upload_snowball_objects

        def snowball(*args, **kwargs):
            self.assertFalse(kwargs.get("compression", False))
            self.staging.append(Path(kwargs["staging_filename"]))
            return upload(*args, **kwargs)

        self.client.upload_snowball_objects = Mock(side_effect=snowball)
        constructor = patch("minio.Minio", return_value=self.client)
        constructor.start()
        self.addCleanup(constructor.stop)

    def stat(self, bucket, key):
        if key not in self.remote:
            raise RuntimeError("NoSuchKey")
        return SimpleNamespace(size=self.remote[key])

    def put(self, bucket, key, data, headers):
        if key.startswith("snowball."):
            self.assertEqual(headers["X-Amz-Meta-Snowball-Auto-Extract"], "true")
            self.assertTrue(self.staging[-1].is_file())
            self.assertLessEqual(len(data), pipeline._SNOWBALL_MAX_BYTES)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                objects = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}
            self.assertLessEqual(len(objects), pipeline._SNOWBALL_MAX_FILES)
            self.archives.append(objects)
            if self.extract:
                self.remote.update({name: len(content) for name, content in objects.items()})
        else:
            self.normal.append((key, headers["Content-Type"]))
            self.remote[key] = len(data)
        self.after_put()
        return Mock()

    def file(self, name, data=b"abc"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_upload(self, prefix="area", **kwargs):
        return pipeline.upload_dir(self.root, prefix, self.cfg,
                                   lambda *args: self.events.append(args),
                                   lambda: self.stop, **kwargs)

    def test_default_keeps_individual_upload_and_content_types(self):
        self.file("tileset.json", b"{}")
        self.file("ortho.pmtiles")
        result = self.run_upload()
        self.assertEqual((result.ok, result.bytes_moved), (2, 5))
        self.assertCountEqual(self.normal, [("area/tileset.json", "application/json"),
                                            ("area/ortho.pmtiles", "application/vnd.pmtiles")])
        self.client.upload_snowball_objects.assert_not_called()

    def test_real_tar_preserves_tree_prefix_unicode_and_empty_files(self):
        content = {"3dtiles/tileset.json": b"{}", "3dtiles/nested/r.pnts": b"points",
                   "3dtiles/empty": b"", "3dtiles/" + "é" * 80 + ".pnts": b"unicode"}
        for name, data in content.items():
            self.file(name, data)
        result = self.run_upload("/region/area/", snowball=True)
        expected = {f"region/area/{name}": data for name, data in content.items()}
        self.assertEqual(self.archives, [expected])
        self.assertEqual((result.ok, result.failed, result.bytes_moved), (4, [], 15))
        self.assertFalse(self.normal)
        self.assertTrue(all(not path.exists() for path in self.staging))
        self.client._create_multipart_upload.assert_not_called()

    def test_empty_prefix_skips_same_size_and_excludes_temporary_files(self):
        for name in ("same.pnts", "changed.pnts", "new.pnts", "x.tmp", "x.part", "tmp/deep/x.pnts"):
            self.file(name)
        self.remote.update({"same.pnts": 3, "changed.pnts": 1})
        result = self.run_upload("", snowball=True)
        self.assertEqual((result.ok, result.skipped, result.bytes_moved), (2, 1, 6))
        self.assertEqual(self.archives, [{"changed.pnts": b"abc", "new.pnts": b"abc"}])

    def test_missing_extraction_is_explicit_failure_without_fallback(self):
        self.file("a.pnts")
        self.file("b.pnts")
        self.extract = False
        result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.bytes_moved), (0, 0))
        self.assertCountEqual(result.failed, ["area/a.pnts", "area/b.pnts"])
        self.assertTrue(any("extraction" in event[3].lower() for event in self.events))
        self.assertFalse(self.normal)
        self.assertTrue(all(not path.exists() for path in self.staging))
        self.client.remove_object.assert_not_called()

    def test_only_verified_extracted_sizes_count_as_success(self):
        self.file("a.pnts")
        self.file("b.pnts")
        self.after_put = lambda: self.remote.update({"area/b.pnts": 1})
        result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.bytes_moved, result.failed), (1, 3, ["area/b.pnts"]))

    def test_upload_error_cleans_archive_and_stops_next_batch(self):
        for i in range(3):
            self.file(f"{i}.pnts")
        self.client._put_object.side_effect = RuntimeError("Snowball unsupported")
        with patch.object(pipeline, "_SNOWBALL_MAX_FILES", 1):
            result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, len(result.failed)), (0, 1))
        self.assertEqual(self.client.upload_snowball_objects.call_count, 1)
        self.assertFalse(self.normal)
        self.assertTrue(all(not path.exists() for path in self.staging))

    def test_batches_bound_tar_bytes_including_headers_and_file_count(self):
        for i in range(5):
            self.file(f"{'é' * 80}/{i}.pnts", b"x" * 4096)
        with patch.object(pipeline, "_SNOWBALL_MAX_BYTES", 10240), \
                patch.object(pipeline, "_SNOWBALL_MAX_FILES", 2):
            result = self.run_upload(snowball=True)
        self.assertEqual(result.ok, 5)
        self.assertEqual(len(self.archives), 5)
        self.archives.clear()
        self.remote.clear()
        with patch.object(pipeline, "_SNOWBALL_MAX_FILES", 2):
            result = self.run_upload(snowball=True)
        self.assertEqual(result.ok, 5)
        self.assertEqual(sorted(map(len, self.archives)), [1, 2, 2])

    def test_large_objects_and_pmtiles_use_normal_upload(self):
        self.file("small.pnts")
        self.file("large.pnts", b"x" * 11)
        self.file("ortho.PMTILES")
        with patch.object(pipeline, "_SNOWBALL_LARGE_FILE", 10):
            result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.bytes_moved), (3, 17))
        self.assertEqual(self.archives, [{"area/small.pnts": b"abc"}])
        self.assertCountEqual([name for name, _ in self.normal], ["area/large.pnts", "area/ortho.PMTILES"])

    def test_single_put_above_sdk_multipart_threshold(self):
        self.file("points.pnts", b"x" * (6 * 1024 * 1024))
        result = self.run_upload(snowball=True)
        self.assertEqual(result.ok, 1)
        self.client._put_object.assert_called_once()
        self.client._create_multipart_upload.assert_not_called()

    def test_cancellation_before_upload_and_between_batches(self):
        for i in range(3):
            self.file(f"{i}.pnts")
        self.stop = True
        result = self.run_upload(snowball=True)
        self.assertEqual(result.ok, 0)
        self.client.stat_object.assert_not_called()
        self.client.upload_snowball_objects.assert_not_called()
        self.stop = False
        self.after_put = lambda: setattr(self, "stop", True)
        with patch.object(pipeline, "_SNOWBALL_MAX_FILES", 1):
            result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.failed), (1, []))
        self.assertEqual(self.client.upload_snowball_objects.call_count, 1)

    def test_empty_and_all_present_do_not_upload_tar(self):
        result = self.run_upload(snowball=True)
        self.assertEqual(result.ok, 0)
        self.file("present.pnts")
        self.remote["area/present.pnts"] = 3
        result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.skipped), (0, 1))
        self.client.upload_snowball_objects.assert_not_called()

    def test_cancellation_discards_pending_batch(self):
        self.file("a.pnts")
        self.file("b.pnts")

        def cancel(bucket, key):
            if self.client.stat_object.call_count == 2:
                self.stop = True
            return self.stat(bucket, key)

        self.client.stat_object.side_effect = cancel
        result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.failed, result.bytes_moved), (0, [], 0))
        self.client.upload_snowball_objects.assert_not_called()

    def test_failed_extraction_stops_before_later_batches(self):
        for i in range(3):
            self.file(f"{i}.pnts")
        self.extract = False
        with patch.object(pipeline, "_SNOWBALL_MAX_FILES", 1):
            result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.bytes_moved, len(result.failed)), (0, 0, 1))
        self.assertEqual(self.client.upload_snowball_objects.call_count, 1)
        self.assertFalse(self.normal)

    def test_growing_source_fails_before_put_and_cleans_staging(self):
        path = self.file("a.pnts")

        def grow(bucket, key):
            path.write_bytes(b"more than the planned size")
            return self.stat(bucket, key)

        self.client.stat_object.side_effect = grow
        result = self.run_upload(snowball=True)
        self.assertEqual((result.ok, result.bytes_moved, result.failed), (0, 0, ["area/a.pnts"]))
        self.client._put_object.assert_not_called()
        self.assertTrue(self.staging)
        self.assertTrue(all(not path.parent.exists() for path in self.staging))


if __name__ == "__main__":
    unittest.main()