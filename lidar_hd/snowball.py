"""Stream a complete TAR through MinIO's single-PUT extraction protocol."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from urllib.parse import urlunsplit
from uuid import uuid4

from minio import time as minio_time
from minio.signer import sign_v4_s3


class UploadReader:
    def __init__(self, stream, total, progress, should_stop):
        self.stream = stream
        self.total = total
        self.progress = progress
        self.should_stop = should_stop
        self.received = 0
        self.last_report = 0.0

    def read(self, size=-1):
        if self.should_stop():
            raise InterruptedError("TAR transfer cancelled")
        data = self.stream.read(min(size, 1024 * 1024) if size >= 0 else 1024 * 1024)
        self.received += len(data)
        now = time.monotonic()
        if now - self.last_report >= 0.1 or self.received == self.total:
            self.progress(self.received, self.total)
            self.last_report = now
        return data


def upload_tar(client, bucket: str, archive: Path, progress, should_stop) -> None:
    """Avoid the SDK's 5 GiB single-part bound and whole-part RAM buffer.

    MinIO extraction requires one PUT, never multipart. Reuse the SDK's URL,
    credentials, signing and TLS pool, but supply a bounded file reader. These
    private SDK access points are covered by transport tests against the lock.
    """
    size = archive.stat().st_size
    region = client._get_region(bucket)
    url = client._base_url.build(method="PUT", region=region, bucket_name=bucket,
                                 object_name=f"snowball.{uuid4()}.tar")
    digest = "UNSIGNED-PAYLOAD"
    if not client._base_url.is_https:
        checksum = hashlib.sha256()
        with archive.open("rb") as stream:
            while data := stream.read(1024 * 1024):
                if should_stop():
                    raise InterruptedError("TAR hashing cancelled")
                checksum.update(data)
        digest = checksum.hexdigest()
    if should_stop():
        raise InterruptedError("TAR transfer cancelled")
    date = minio_time.utcnow()
    headers = {"Host": url.netloc, "Content-Length": str(size),
               "Content-Type": "application/x-tar",
               "X-Amz-Meta-Snowball-Auto-Extract": "true",
               "x-amz-content-sha256": digest, "x-amz-date": minio_time.to_amz_date(date)}
    credentials = client._provider.retrieve()
    if credentials.session_token:
        headers["X-Amz-Security-Token"] = credentials.session_token
    headers = sign_v4_s3(method="PUT", url=url, region=region, headers=headers,
                         credentials=credentials, content_sha256=digest, date=date)
    progress(0, size)
    with archive.open("rb") as stream:
        response = client._http.urlopen(
            "PUT", urlunsplit(url), body=UploadReader(stream, size, progress, should_stop),
            headers=headers, preload_content=False, retries=False, redirect=False)
        try:
            if response.status != 200:
                raise RuntimeError(f"MinIO TAR extraction PUT failed (HTTP {response.status})")
        finally:
            response.close()
            response.release_conn()