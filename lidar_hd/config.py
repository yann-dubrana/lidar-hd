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


try:
    from dotenv import load_dotenv
    if getattr(sys, "frozen", False):
        load_dotenv(application_root() / ".env", override=False)
    else:
        load_dotenv()
except ImportError:                     # python-dotenv is optional
    pass

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

# IGN rate-limits parallel requests: batches come back empty or 403. Every
# network loop in this package is deliberately sequential.
HTTP_TIMEOUT = 60
HTTP_RETRIES = 4

# --- measured constants ----------------------------------------------------

# Mean .copc.laz size over a 63-tile random sample across Gironde. Used for
# size estimates, which are explicitly approximate (~8% spread).
MEAN_TILE_BYTES = 115.7 * 1024 * 1024

# Colourising rewrites LAS point format 6 (no RGB) to format 7 (with RGB).
COLORIZE_GROWTH = 1.49
# 3D Tiles output measured at ~2.3x the colourised .laz size.
TILES3D_GROWTH = 2.28

TILE_KM = 1                          # LiDAR HD tiles are 1 km squares
ORTHO_PX = 5000                      # 1 km / 5000 px = 20 cm, native BD ORTHO


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
