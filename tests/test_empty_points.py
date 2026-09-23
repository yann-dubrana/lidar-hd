import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lidar_hd.cleanup import prune_empty_points


class EmptyPointsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tileset = self.root / "tileset"
        self.points = self.tileset / "points"
        self.points.mkdir(parents=True)

    @contextmanager
    def link(self, path, target, junction=False, simulated=False):
        created = False
        if not simulated:
            try:
                if junction:
                    if os.name == "nt":
                        result = subprocess.run(
                            ["cmd", "/c", "mklink", "/J", str(path), str(target)],
                            capture_output=True, check=False,
                        )
                        created = result.returncode == 0
                else:
                    path.symlink_to(target, target_is_directory=True)
                    created = True
            except (OSError, NotImplementedError):
                pass
        if created:
            try:
                yield
            finally:
                if junction:
                    path.rmdir()
                else:
                    path.unlink()
        else:
            path.mkdir()
            (path / "untouched").mkdir()
            original = Path.lstat

            def lstat(candidate, *args, **kwargs):
                if candidate == path:
                    return SimpleNamespace(
                        st_mode=stat.S_IFDIR if junction else stat.S_IFLNK,
                        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                    )
                return original(candidate, *args, **kwargs)

            with patch.object(Path, "lstat", lstat):
                yield
            self.assertTrue((path / "untouched").is_dir())

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

    def check_link(self, junction=False, simulated=False, root=False):
        target = self.root / "outside"
        (target / "empty").mkdir(parents=True)
        (target / "file").write_bytes(b"safe")
        link = self.points / "link"
        if root:
            self.points.rmdir()
            link = self.points
        else:
            (self.points / "removable").mkdir()
        original = os.scandir

        def scandir(path):
            self.assertNotEqual(Path(path), link, "must not traverse links")
            return original(path)

        with self.link(link, target, junction=junction, simulated=simulated):
            with patch("os.scandir", side_effect=scandir):
                self.assertEqual(prune_empty_points(self.tileset), 0 if root else 1)
            self.assertTrue(link.exists())
            self.assertTrue((target / "empty").is_dir())
            self.assertEqual((target / "file").read_bytes(), b"safe")

    def test_symlink_is_not_traversed_or_removed(self):
        self.check_link()

    def test_junction_is_not_traversed_or_removed(self):
        self.check_link(junction=True)

    def test_simulated_symlink(self):
        self.check_link(simulated=True)

    def test_simulated_junction(self):
        self.check_link(junction=True, simulated=True)

    def test_points_symlink(self):
        self.check_link(root=True)

    def test_points_junction(self):
        self.check_link(junction=True, root=True)

    def test_tileset_link_is_not_traversed(self):
        alias = self.root / "alias"
        (self.points / "empty").mkdir()
        with self.link(alias, self.tileset):
            self.assertEqual(prune_empty_points(alias), 0)
        self.assertTrue((self.points / "empty").is_dir())

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

    def test_file_created_before_rmdir_is_preserved(self):
        directory = self.points / "racing"
        directory.mkdir()
        original = Path.rmdir

        def rmdir(path):
            if path == directory:
                (directory / "new.pnts").write_bytes(b"new")
            return original(path)

        with patch.object(Path, "rmdir", rmdir):
            self.assertEqual(prune_empty_points(self.tileset), 0)
        self.assertEqual((directory / "new.pnts").read_bytes(), b"new")

    def test_directory_disappears_before_rmdir(self):
        directory = self.points / "racing"
        directory.mkdir()
        original = Path.rmdir

        def rmdir(path):
            if path == directory:
                original(path)
            return original(path)

        with patch.object(Path, "rmdir", rmdir):
            self.assertEqual(prune_empty_points(self.tileset), 0)
        self.assertTrue(self.points.is_dir())

    def test_scan_failure_does_not_stop_other_branches(self):
        blocked = self.points / "blocked"
        (blocked / "empty").mkdir(parents=True)
        (self.points / "removable").mkdir()
        original = os.scandir

        def scandir(path):
            if Path(path) == blocked:
                raise PermissionError("access denied")
            return original(path)

        with patch("os.scandir", side_effect=scandir):
            self.assertEqual(prune_empty_points(self.tileset), 1)
        self.assertTrue((blocked / "empty").is_dir())

    def test_metadata_errors_are_ignored(self):
        for error in (PermissionError, FileNotFoundError):
            with self.subTest(error=error):
                with patch.object(Path, "lstat", side_effect=error):
                    self.assertEqual(prune_empty_points(self.tileset), 0)

    def test_ancestor_becoming_a_junction_is_rechecked(self):
        directory = self.points / "branch"
        (directory / "empty").mkdir(parents=True)
        scanned = False
        original_scan = os.scandir
        original_stat = Path.lstat

        def scandir(path):
            nonlocal scanned
            result = original_scan(path)
            if Path(path) == directory:
                scanned = True
            return result

        def lstat(path, *args, **kwargs):
            if path == directory and scanned:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return original_stat(path, *args, **kwargs)

        with patch("os.scandir", side_effect=scandir), \
                patch.object(Path, "lstat", lstat), \
                patch.object(Path, "rmdir", side_effect=AssertionError("unsafe removal")):
            self.assertEqual(prune_empty_points(self.tileset), 0)
        self.assertTrue((directory / "empty").is_dir())

    def test_relative_tileset_path(self):
        (self.points / "empty").mkdir()
        relative = self.tileset.relative_to(Path.cwd())
        self.assertEqual(prune_empty_points(relative), 1)
        self.assertTrue(self.points.is_dir())


if __name__ == "__main__":
    unittest.main()