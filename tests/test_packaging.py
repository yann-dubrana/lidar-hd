import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lidar_hd import config, pipeline


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_frozen_data_is_beside_executable_not_inside_bundle(self):
        executable = Path(__file__).resolve().parent / "portable" / "lidar-hd.exe"
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(executable)), \
                patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.data_root(), executable.parent / "data")

    def test_data_environment_override_still_wins(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.dict(os.environ, {"LIDARHD_DATA": "custom-data"}):
            self.assertEqual(config.data_root(), Path("custom-data"))

    def test_frozen_dotenv_is_only_beside_executable(self):
        executable = Path(__file__).resolve().parent / "portable" / "lidar-hd.exe"
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(executable)), \
                patch("dotenv.load_dotenv") as load:
            runpy.run_path(str(Path(config.__file__)))
        load.assert_called_once_with(executable.parent / ".env", override=False)

    def test_frozen_conversion_relaunches_bundled_worker(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", "portable-lidar.exe"), \
                patch("lidar_hd.pipeline.py3dtiles_exe") as external, \
                patch("lidar_hd.pipeline.subprocess.run", return_value=Mock(returncode=0)) as run:
            result = pipeline.convert_3dtiles([Path("input.laz")], self.root / "output", jobs=1)
        external.assert_not_called()
        self.assertEqual(run.call_args.args[0][:3],
                         ["portable-lidar.exe", "--internal-py3dtiles", "convert"])
        self.assertEqual(result.ok, 1)

    def test_source_conversion_keeps_existing_entry_point(self):
        with patch.object(sys, "frozen", False, create=True), \
                patch("lidar_hd.pipeline.py3dtiles_exe", return_value=Path("py3dtiles.exe")), \
                patch("lidar_hd.pipeline.subprocess.run", return_value=Mock(returncode=0)) as run:
            pipeline.convert_3dtiles([Path("input.laz")], self.root / "output")
        self.assertEqual(run.call_args.args[0][:2], ["py3dtiles.exe", "convert"])


if __name__ == "__main__":
    unittest.main()