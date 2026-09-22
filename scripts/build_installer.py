"""Wrap the existing PyInstaller folder in a per-user Inno Setup installer."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parent.parent


def installer_version(value: str) -> str:
    value = value.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", value):
        raise ValueError("Use a version such as 0.1.0 or v0.1.0")
    return value


def find_compiler(explicit: str | None = None) -> str:
    candidate = explicit or os.getenv("INNO_SETUP_COMPILER") or shutil.which("ISCC.exe")
    if candidate:
        if not Path(candidate).is_file():
            raise FileNotFoundError(f"Inno Setup compiler not found: {candidate}")
        return str(Path(candidate).resolve())
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        if base := os.getenv(variable):
            for folder in ("Inno Setup 6", "Programs/Inno Setup 6"):
                path = Path(base) / folder / "ISCC.exe"
                if path.is_file():
                    return str(path)
    raise FileNotFoundError("Install Inno Setup 6, or pass --compiler PATH_TO_ISCC.exe")


def build(version: str, compiler: str, root: Path = ROOT) -> Path:
    version = installer_version(version)
    source = root / "dist" / "lidar-hd"
    if not (source / "lidar-hd.exe").is_file() or not (source / "_internal").is_dir():
        raise FileNotFoundError("Build lidar-hd.spec with PyInstaller before the installer")
    output = root / "artifacts"
    output.mkdir(exist_ok=True)
    subprocess.run([
        compiler, f"/DAppVersion={version}", f"/DSourceDir={source}",
        f"/DArtifactDir={output}", str(root / "scripts" / "installer.iss"),
    ], check=True)
    installer = output / "lidar-hd-windows-x64-setup.exe"
    if not installer.is_file():
        raise FileNotFoundError(f"Compiler did not produce {installer}")
    return installer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", help="Path to Inno Setup 6 ISCC.exe")
    parser.add_argument("--version", help="Release tag; defaults to pyproject.toml version")
    args = parser.parse_args()
    version = args.version
    if version is None:
        with (ROOT / "pyproject.toml").open("rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
    print(build(version, find_compiler(args.compiler)))


if __name__ == "__main__":
    main()