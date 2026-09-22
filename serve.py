#!/usr/bin/env python
"""Static file server for local 3D Tiles development.

Adds what a tileset needs that http.server does not provide out of the box:
CORS headers, correct MIME types for .pnts/.b3dm/.json, HTTP range requests
(viewers ask for byte ranges), and no-cache so a re-converted tileset shows up
on reload instead of serving a stale octree.

Usage:  python serve.py [ROOT] [--port 8080]
"""
import argparse
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

TYPES = {
    ".pnts": "application/octet-stream",
    ".b3dm": "application/octet-stream",
    ".cmpt": "application/octet-stream",
    ".glb": "model/gltf-binary",
    ".json": "application/json",
    ".laz": "application/octet-stream",
    ".copc": "application/octet-stream",
}


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Range, Content-Length, Accept-Ranges")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        return TYPES.get(ext) or super().guess_type(path)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        """Serve a byte range when asked, otherwise fall back to the default."""
        rng = self.headers.get("Range")
        if not rng:
            return super().do_GET()

        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().do_GET()

        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if not m:
            return super().do_GET()

        size = os.path.getsize(path)
        start_s, end_s = m.group(1), m.group(2)
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:
            # suffix form: "bytes=-500" means the last 500 bytes
            start = max(0, size - int(end_s)) if end_s else 0
            end = size - 1
        if start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        end = min(end, size - 1)

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt, *args):
        if "404" in (fmt % args):            # keep misses, drop the 200 noise
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    print(f"serving {root} on http://localhost:{a.port}/")
    print("  viewer:  http://localhost:%d/viewer.html" % a.port)
    ThreadingHTTPServer(
        ("0.0.0.0", a.port), partial(Handler, directory=root)
    ).serve_forever()
