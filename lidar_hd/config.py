"""Settings, loaded from the environment (and a .env file if present).

Only MinIO credentials are secret; everything else has a sensible default that
can still be overridden by an environment variable.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def application_root() -> Path:
    """Writable portable directory, never PyInstaller's internal resources."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


from dotenv import load_dotenv                              # noqa: E402

if getattr(sys, "frozen", False):
    load_dotenv(application_root() / ".env", override=False)
else:
    load_dotenv()

# --- IGN endpoints ---------------------------------------------------------

WFS = "https://data.geopf.fr/wfs/ows"
ADMIN_LAYER = "ADMINEXPRESS-COG.LATEST"
DOWNLOAD_BASE = "https://data.geopf.fr/telechargement/download/LiDARHD-NUALID"
RESOURCE = "https://data.geopf.fr/telechargement/resource/LiDARHD-NUALID"
WMS = "https://data.geopf.fr/wms-r/wms"
ORTHO_LAYER = "HR.ORTHOIMAGERY.ORTHOPHOTOS"

USER_AGENT = "Mozilla/5.0"          # data.geopf.fr rejects requests without one
LAMBERT93 = 2154
ECEF = 4978                         # what 3D Tiles viewers expect

# Catalogue/WFS/WMS loops stay sequential. Tile downloads run in parallel:
# against data.geopf.fr, 8 connections ran without errors and 16 added HTTP 429
# for no extra throughput. Throughput looks capped per IP, so a second
# downloader on the same line takes its share. Tune these to the line.
HTTP_TIMEOUT = 60
HTTP_RETRIES = 4
DOWNLOAD_WORKERS = 8
DOWNLOAD_RETRIES = 10
# Some download connections are served at ~0.25 MB/s for their whole life while
# others run at ~20 MB/s. A transfer below DOWNLOAD_MIN_SPEED (bytes/s) for
# DOWNLOAD_STALL_SECONDS is dropped and restarted.
DOWNLOAD_MIN_SPEED = 512_000
DOWNLOAD_STALL_SECONDS = 15

# --- measured constants ----------------------------------------------------

# Mean .copc.laz size over a 63-tile random sample across Gironde. Used for
# size estimates, which are explicitly approximate (~8% spread).
MEAN_TILE_BYTES = 115.7 * 1024 * 1024

# Colourising rewrites LAS point format 6 (no RGB) to format 7 (with RGB) and
# adds a 16-bit height. 1.49 measured for RGB alone; the height field added 11%
# on one urban Bordeaux tile (1.66 -> 1.84), applied here to the mean.
COLORIZE_GROWTH = 1.65
# 3D Tiles output measured at ~2.3x the colourised .laz size.
TILES3D_GROWTH = 2.28

ORTHO_PX = 5000                      # 1 km / 5000 px = 20 cm, native BD ORTHO

# --- colourise tuning --------------------------------------------------------

# Bump when colourised output changes: colorized/.version older than this is redone.
COLOR_VERSION = 2
# The orthophoto only shows the top of each cell. Points more than
# OCCLUSION_DZ_M below it (walls, ground under a canopy) get their class colour.
OCCLUSION_CELL_M = 0.5
OCCLUSION_DZ_M = 1.0
# Ground model for the per-point height: lowest ground return per cell.
GROUND_CELL_M = 5.0
# Colour of a hidden point, by class. CLASSES in viewer.html, except buildings:
# a wall in the photo view wants stone, not the map's red.
CLASS_COLORS = {
    1: (150, 150, 150), 2: (139, 119, 101), 3: (120, 160, 80), 4: (70, 140, 60),
    5: (35, 105, 45), 6: (196, 186, 170), 9: (70, 130, 180), 17: (176, 160, 112),
}
CLASS_COLOR_OTHER = (130, 136, 144)


@dataclass(frozen=True)
class MinioConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    prefix: str = "lidar-hd"
    secure: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.access_key and self.secret_key)


def minio_config() -> MinioConfig:
    """Read MinIO settings from the environment.

    Set these in a .env file next to main.py, or export them:

        MINIO_ENDPOINT=storage.optimaize.fr
        MINIO_ACCESS_KEY=...
        MINIO_SECRET_KEY=...
        MINIO_BUCKET=lidar-hd
    """
    return MinioConfig(
        endpoint=os.getenv("MINIO_ENDPOINT", "").replace("https://", "").replace("http://", "").rstrip("/"),
        access_key=os.getenv("MINIO_ACCESS_KEY", ""),
        secret_key=os.getenv("MINIO_SECRET_KEY", ""),
        bucket=os.getenv("MINIO_BUCKET", "lidar-hd"),
        prefix=os.getenv("MINIO_PREFIX", "lidar-hd"),
        secure=os.getenv("MINIO_SECURE", "true").lower() != "false",
    )


def data_root() -> Path:
    """Where downloads and derived data land.

    Defaults to `data/` beside the project (or the frozen executable).
    Override with LIDARHD_DATA to put the (large) output on another drive.
    """
    env = os.getenv("LIDARHD_DATA")
    if env:
        return Path(env)
    return application_root() / "data"
