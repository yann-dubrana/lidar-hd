"""Install, upgrade and uninstall on a clean Windows build machine."""
import os
from pathlib import Path
import subprocess
import tempfile
import winreg


def main() -> None:
    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{179F783B-89DC-419F-95CE-A4DD31A4D542}_is1"
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | view):
                raise RuntimeError("Refusing to test over an existing LiDAR HD installation")
        except FileNotFoundError:
            pass
    root = Path(__file__).resolve().parent.parent
    installer = root / "artifacts" / "lidar-hd-windows-x64-setup.exe"
    flags = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
    with tempfile.TemporaryDirectory(prefix="installer-test-", dir=root / "artifacts") as directory:
        target = Path(directory) / "app"
        command = [str(installer), *flags, "/NOICONS", f"/DIR={target}"]
        try:
            subprocess.run(command, check=True)
            env = dict(os.environ, LIDARHD_DATA=str(target / "data"))
            for option in ("--help", "--self-test"):
                subprocess.run([str(target / "lidar-hd.exe"), option], env=env, check=True)
            config = target / ".env"
            config.write_text("# installer preservation test\n")
            data = target / "data" / "keep.txt"
            data.parent.mkdir(exist_ok=True)
            data.write_text("keep downloaded data")
            subprocess.run(command, check=True)
            assert config.read_text() == "# installer preservation test\n"
            assert data.read_text() == "keep downloaded data"
        finally:
            uninstaller = target / "unins000.exe"
            if uninstaller.exists():
                subprocess.run([str(uninstaller), *flags], check=True)
        assert not (target / "lidar-hd.exe").exists()
        assert config.read_text() == "# installer preservation test\n"
        assert data.read_text() == "keep downloaded data"
    print("Installer passed: installed runtime, upgrade and uninstall preserve user files.")


if __name__ == "__main__":
    main()