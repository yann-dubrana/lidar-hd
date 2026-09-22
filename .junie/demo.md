vm: template-vm

## Setup status

The image builds with uv 0.9.26 and Python 3.14.2. The frozen dependency
installation was smoke-tested and is currently blocked: the locked
`mapbox-earcut==1.0.3` (via py3dtiles) builds from source on this Linux/Python
combination, and the base image has no `c++` compiler. Adding Debian
`build-essential` was proposed; the user explicitly chose to defer it.
Do not treat this setup as verified or record a demo until that prerequisite
has been approved, installed, and the frozen install smoke test passes.
No demo was run or recorded during setup.

## Running inside the VM

Run in the VM shell. This is a Textual terminal application, not a website;
there is no listening port or browser URL. The xterm process is backgrounded
while the application keeps its interactive terminal.

```sh
cd /workspace
export UV_PROJECT_ENVIRONMENT=/tmp/lidarhd-demo-venv
export LIDARHD_DATA=/tmp/lidarhd-demo-data
export MINIO_ENDPOINT= MINIO_ACCESS_KEY= MINIO_SECRET_KEY=
uv sync --frozen --python 3.14 || exit 1

# Public IGN service preflight. Do not proceed against an unavailable API.
uv run --no-sync python -c 'from lidar_hd.areas import search; assert search("commune", "Pessac"), "IGN area search unavailable"' || exit 1

xterm -fa Monospace -fs 12 -geometry 140x44 -T 'LiDAR HD' -e uv run --no-sync python main.py &
# Ready when the LiDAR HD area browser, search field, and footer are visible.
```

## Demo scope

- Show the real TUI in xterm, using keyboard navigation or mouse clicks.
- Browse regions and departments, then switch to Commune and search for
  `Castelmoron-d'Albret`. Select the row and wait for its tile/storage estimate.
- This is a real public-data download, not a simulation. Only start if the
  estimate is at most 4 tiles and 500 MB raw. If it exceeds that limit, show
  the estimate only. Never download an entire department or region.
- Leave Colourise, Export ortho PMTiles, Convert, and Upload off. Start the small download to show
  overall stage progress and live current-file bytes. Existing complete files
  are skipped on reruns; explain this rather than deleting them.
- Stop requests take effect after the current tile (or current conversion).
  Wait for Stopped or Complete before quitting. Space or Enter toggles a
  place in the selection queue; use only one small commune for this demo.
- IGN calls are deliberately sequential and can be slow or rate-limited.
  Show errors honestly; do not repeatedly restart failing downloads.

## Environment and safety

Python 3.14 and uv 0.9.26 are installed by the VM template. Dependencies use
the committed uv.lock from public PyPI, with a separate Linux virtualenv in
/tmp so the host's Windows .venv is untouched.

No login, database, local service, or secret mount is required for this scope.
Uploads are explicitly excluded. Empty MinIO environment variables override
any host .env values, and LIDARHD_DATA overrides its Windows path. Demo data
stays under /tmp/lidarhd-demo-data inside the VM; no host LiDAR directory is
mounted. The optional web viewer and orthophoto/conversion stages are outside
this demo.
