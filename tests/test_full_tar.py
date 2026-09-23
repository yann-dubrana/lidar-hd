import unittest
from unittest.mock import Mock, patch

import test_snowball
from lidar_hd import pipeline


class FullTilesetTests(unittest.TestCase):
    setUp = test_snowball.SnowballTests.setUp
    stat = test_snowball.SnowballTests.stat
    put = test_snowball.SnowballTests.put
    stream_put = test_snowball.SnowballTests.stream_put
    file = test_snowball.SnowballTests.file
    run_upload = test_snowball.SnowballTests.run_upload
    def test_complete_tileset_in_one_tar_including_large_and_present_files(self):
        self.file("tileset.json", b"{}")
        self.remote["area/tileset.json"] = 2
        for i in range(257):
            self.file(f"points/{i}/r.pnts")
        large = b"x" * (16 * 1024 * 1024)
        self.file("points/large.pnts", large)
        result = self.run_upload(snowball=True)
        self.assertFalse(result.failed)
        self.assertFalse(self.normal)
        self.assertEqual(len(self.archives), 1)
        self.assertEqual(len(self.archives[0]), 259)
        self.assertEqual(self.archives[0]["area/points/large.pnts"], large)
        self.assertEqual(self.archives[0]["area/tileset.json"], b"{}")
        self.assertEqual((result.ok, result.skipped), (259, 0))

    def test_upload_prunes_only_empty_points_directories(self):
        self.file("tileset.json", b"{}")
        self.file("points/kept/r.pnts")
        self.file("points/zero/empty.pnts", b"")
        empty = self.root / "points" / "unused" / "child"
        empty.mkdir(parents=True)
        result = self.run_upload(snowball=True)
        self.assertFalse(result.failed)
        self.assertFalse(empty.parent.exists())
        self.assertTrue((self.root / "points/kept/r.pnts").is_file())
        self.assertTrue((self.root / "points/zero/empty.pnts").is_file())
        self.assertEqual(len(self.archives[0]), 3)

    def test_conversion_prunes_empty_points_only_after_success(self):
        def convert(*args, **kwargs):
            self.file("tileset.json", b"{}")
            self.file("points/kept/r.pnts")
            (self.root / "points/unused/child").mkdir(parents=True)
            return Mock(returncode=0)
        with patch.object(pipeline, "py3dtiles_exe", return_value="py3dtiles"), \
                patch.object(pipeline.subprocess, "run", side_effect=convert):
            result = pipeline.convert_3dtiles([self.root / "input.laz"], self.root)
        self.assertEqual(result.ok, 1)
        self.assertFalse((self.root / "points/unused").exists())
        self.assertTrue((self.root / "points/kept/r.pnts").is_file())
        with patch.object(pipeline, "py3dtiles_exe", return_value="py3dtiles"), \
                patch.object(pipeline.subprocess, "run", return_value=Mock(returncode=1, stderr="failed")), \
                patch.object(pipeline, "prune_empty_points") as prune:
            result = pipeline.convert_3dtiles([self.root / "input.laz"], self.root)
        self.assertEqual(result.failed, ["failed"])
        prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()