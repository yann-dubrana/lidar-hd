import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lidar_hd.pipeline import prune_empty_points


class EmptyPointsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tileset = self.root / "tileset"
        self.points = self.tileset / "points"
        self.points.mkdir(parents=True)

    def test_removes_empty_subtrees_and_keeps_points_root(self):
        (self.points / "a" / "b" / "c").mkdir(parents=True)
        (self.points / "d").mkdir()
        self.assertEqual(prune_empty_points(self.tileset), 4)
        self.assertTrue(self.points.is_dir())
        self.assertEqual(list(self.points.iterdir()), [])
        self.assertEqual(prune_empty_points(self.tileset), 0)

    def test_preserves_files_including_zero_bytes_and_other_trees(self):
        (self.points / "populated" / "empty").mkdir(parents=True)
        (self.points / "zero").mkdir()
        payload = self.points / "populated" / "tile.pnts"
        payload.write_bytes(b"point cloud")
        zero = self.points / "zero" / "zero.pnts"
        zero.touch()
        outside = self.tileset / "other" / "empty"
        outside.mkdir(parents=True)
        metadata = self.tileset / "tileset.json"
        metadata.write_bytes(b"{}")
        with patch("shutil.rmtree", side_effect=AssertionError("rmtree forbidden")), \
                patch.object(Path, "unlink", side_effect=AssertionError("unlink forbidden")):
            self.assertEqual(prune_empty_points(self.tileset), 1)
        self.assertEqual(payload.read_bytes(), b"point cloud")
        self.assertEqual(zero.read_bytes(), b"")
        self.assertEqual(metadata.read_bytes(), b"{}")
        self.assertTrue(outside.is_dir())

    def test_missing_points(self):
        self.points.rmdir()
        self.assertEqual(prune_empty_points(self.tileset), 0)
        self.assertFalse(self.points.exists())

    def test_points_is_a_file(self):
        self.points.rmdir()
        self.points.write_bytes(b"")
        self.assertEqual(prune_empty_points(self.tileset), 0)
        self.assertEqual(self.points.read_bytes(), b"")

    def test_rmdir_failure_does_not_stop_other_branches(self):
        blocked = self.points / "blocked"
        blocked.mkdir()
        (self.points / "removable").mkdir()
        original = Path.rmdir

        def rmdir(path):
            if path == blocked:
                raise PermissionError("access denied")
            return original(path)

        with patch.object(Path, "rmdir", rmdir):
            self.assertEqual(prune_empty_points(self.tileset), 1)
        self.assertTrue(blocked.is_dir())

    def test_relative_tileset_path(self):
        (self.points / "empty").mkdir()
        relative = self.tileset.relative_to(Path.cwd())
        self.assertEqual(prune_empty_points(relative), 1)
        self.assertTrue(self.points.is_dir())


if __name__ == "__main__":
    unittest.main()
