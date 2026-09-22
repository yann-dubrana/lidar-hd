from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / "lidar_hd" / "tui.tcss"), "lidar_hd"),
         (str(root / "README.md"), "."),
         (str(root / ".env.example"), ".")]
binaries = []
hiddenimports = []

# These packages load readers/widgets/extensions dynamically. Rasterio also
# needs its GDAL/PROJ databases and wheel DLLs, not just Python imports.
for package in ("textual", "py3dtiles", "rasterio"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports
datas += copy_metadata("py3dtiles")

a = Analysis(
    [str(root / "scripts" / "frozen_entry.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="lidar-hd",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="lidar-hd")