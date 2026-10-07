import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from minio import Minio

from lidar_hd import pipeline


ROOT = Path(__file__).resolve().parents[1]


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = SimpleNamespace(endpoint="localhost:9000", access_key="key",
                                   secret_key="secret", secure=False, bucket="bucket")
        self.client = Minio(self.cfg.endpoint, access_key="key", secret_key="secret", secure=False)
        self.remote = {}
        self.normal = []
        self.events = []
        self.client.bucket_exists = Mock(return_value=True)
        self.client.make_bucket = Mock()
        self.client.stat_object = Mock(side_effect=self.stat)
        self.client._execute = Mock(side_effect=AssertionError("network forbidden"))
        self.client._put_object = Mock(side_effect=self.put)
        self.client.remove_object = Mock(side_effect=AssertionError("remote deletion forbidden"))
        constructor = patch("minio.Minio", return_value=self.client)
        constructor.start()
        self.addCleanup(constructor.stop)
        # Never the mc client really installed on the machine running the tests.
        self.mc = patch("lidar_hd.pipeline._mc_command", return_value=None).start()
        self.addCleanup(self.mc.stop)

    def stat(self, bucket, key):
        if key not in self.remote:
            raise RuntimeError("NoSuchKey")
        return SimpleNamespace(size=self.remote[key])

    def put(self, bucket, key, data, headers):
        self.normal.append((key, headers["Content-Type"]))
        self.remote[key] = len(data)
        return Mock()

    def file(self, name, data=b"abc"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_upload(self, prefix="area"):
        return pipeline.upload_dir(self.root, prefix, self.cfg,
                                   lambda *args: self.events.append(args), lambda: False)

    def test_individual_upload_sets_content_types(self):
        self.file("tileset.json", b"{}")
        self.file("ortho.pmtiles")
        result = self.run_upload()
        self.assertEqual((result.ok, result.bytes_moved), (2, 5))
        self.assertCountEqual(self.normal, [("area/tileset.json", "application/json"),
                                            ("area/ortho.pmtiles", "application/vnd.pmtiles")])

    def test_individual_upload_skips_same_size_and_temporary_files(self):
        for name in ("same.pnts", "changed.pnts", "new.pnts", "x.tmp", "x.part", "tmp/deep/x.pnts"):
            self.file(name)
        self.remote.update({"area/same.pnts": 3, "area/changed.pnts": 1})
        result = self.run_upload()
        self.assertEqual((result.ok, result.skipped, result.failed), (2, 1, []))
        self.assertCountEqual([key for key, _ in self.normal], ["area/changed.pnts", "area/new.pnts"])

    def mirror(self, lines, code=0):
        self.mc.return_value = ["mc"]
        process = Mock(stdout=iter(lines), wait=Mock(return_value=code), returncode=code)
        self.file("tileset.json", b"{}")
        self.file("r.pnts", b"points")
        with patch("lidar_hd.pipeline.subprocess.run", return_value=Mock(returncode=0)), \
                patch("lidar_hd.pipeline.subprocess.Popen", return_value=process) as popen:
            return self.run_upload("/area/"), popen

    def test_mc_client_mirrors_with_credentials_out_of_the_command(self):
        result, popen = self.mirror(['{"status":"success","source":"./r.pnts","size":6}\n',
                                     '{"status":"success","total":6,"transferred":6}\n'])
        self.assertEqual((result.ok, result.skipped, result.bytes_moved), (1, 1, 6))
        self.assertEqual(self.normal, [])
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ["./", "lidarhd/bucket/area/"])
        self.assertNotIn(self.cfg.secret_key, " ".join(command))
        environment = popen.call_args.kwargs["env"]
        self.assertIn(self.cfg.secret_key, environment["MC_HOST_lidarhd"])
        self.assertIn("MC_HOST_lidarhd", environment["WSLENV"])

    def test_failed_mirror_falls_back_to_individual_upload(self):
        result, _ = self.mirror(['{"status":"error","error":{"message":"denied"}}\n'], code=1)
        self.assertEqual(result.ok, 2)
        self.assertEqual(len(self.normal), 2)

    def test_upload_prunes_only_empty_points_directories(self):
        self.file("tileset.json", b"{}")
        self.file("points/kept/r.pnts")
        self.file("points/zero/empty.pnts", b"")
        empty = self.root / "points" / "unused" / "child"
        empty.mkdir(parents=True)
        result = self.run_upload()
        self.assertEqual((result.ok, result.failed), (3, []))
        self.assertFalse(empty.parent.exists())
        self.assertTrue((self.root / "points/kept/r.pnts").is_file())
        self.assertTrue((self.root / "points/zero/empty.pnts").is_file())

    def test_conversion_prunes_empty_points_only_after_success(self):
        def convert(*args, **kwargs):
            self.file("tileset.json", b"{}")
            self.file("points/kept/r.pnts")
            (self.root / "points/unused/child").mkdir(parents=True)
            return Mock(returncode=0)
        with patch.object(pipeline.subprocess, "run", side_effect=convert):
            result = pipeline.convert_3dtiles([self.root / "input.laz"], self.root)
        self.assertEqual(result.ok, 1)
        self.assertFalse((self.root / "points/unused").exists())
        self.assertTrue((self.root / "points/kept/r.pnts").is_file())
        with patch.object(pipeline.subprocess, "run", return_value=Mock(returncode=1, stderr="failed")), \
                patch.object(pipeline, "prune_empty_points") as prune:
            result = pipeline.convert_3dtiles([self.root / "input.laz"], self.root)
        self.assertEqual(result.failed, ["failed"])
        prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()
