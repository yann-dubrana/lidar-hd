import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_installer import build, installer_version


class InstallerTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(installer_version("v0.1.0"), "0.1.0")
        self.assertEqual(installer_version("0.2.0-rc.1"), "0.2.0-rc.1")
        for value in ('v0.1', 'main', '0.1.0"\n[Run]', '../0.1.0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                installer_version(value)

    def test_missing_bundle(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            with self.assertRaises(FileNotFoundError):
                build("0.1.0", "ISCC.exe", Path(directory))

    def test_compiler_arguments_and_failure(self):
        with tempfile.TemporaryDirectory(prefix="installer space ", dir=Path(__file__).parent) as directory:
            root = Path(directory).resolve()
            source = root / "dist" / "lidar-hd"
            (source / "_internal").mkdir(parents=True)
            (source / "lidar-hd.exe").touch()
            output = root / "artifacts" / "lidar-hd-windows-x64-setup.exe"
            with patch("scripts.build_installer.subprocess.run") as run:
                run.side_effect = lambda *args, **kwargs: output.touch()
                self.assertEqual(build("v0.1.0", "ISCC.exe", root), output)
            run.assert_called_once_with([
                "ISCC.exe", "/DAppVersion=0.1.0", f"/DSourceDir={source}",
                f"/DArtifactDir={root / 'artifacts'}", str(root / "scripts" / "installer.iss"),
            ], check=True)
            with patch("scripts.build_installer.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ISCC.exe")):
                with self.assertRaises(subprocess.CalledProcessError):
                    build("0.1.0", "ISCC.exe", root)


if __name__ == "__main__":
    unittest.main()