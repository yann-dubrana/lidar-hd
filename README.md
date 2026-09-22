# lidarhd

Download IGN LiDAR HD for French communes, intercommunalities (EPCI), departments or regions; optionally
drape orthophoto colour, export a 20 cm raster basemap, convert to 3D Tiles,
and mirror to MinIO.

    uv run python main.py

Browse regions, departments and EPCI immediately, or search communes by name.
Use **Space** or **Enter** on a row to toggle it in the selection queue. Select
multiple places, even across searches and administrative levels; they run
sequentially, each in its own output directory by default. The estimate appears before
anything is downloaded. **Clear selection** empties the queue.

The roomier split-pane TUI keeps **Run** and **Stop** visible. **Ctrl+F** or
**/** focuses search, **Tab** changes controls, **Ctrl+R** starts the batch,
**Escape** requests a stop, and **Ctrl+Q** quits. Stop waits for the current
tile or conversion. Narrow terminals use a vertically scrollable layout.

Overall progress weights each enabled stage and area equally, **not by time**.
The download bar tracks the current file's bytes and transfer speed, resets on
retry, and shows an indeterminate bar if its size is unknown. Failed and stopped
runs are labelled explicitly; a finished bar does not imply every file succeeded.

```
Bordeaux (33063) · 50 km²
73 tiles ≈ 8.2 GB raw (±8%)
+ colourised 12.3 GB + 3D Tiles 28.0 GB = 48.6 GB if all stages run
~0.3 h download · ~1.0 h processing
```

Download and safe cleanup are selected by default. **Every stage is a separate
option**: Download, Colourise, Export ortho PMTiles, Convert, Upload, Cleanup.
Uncheck Download to test processing against existing files, or to export only
the orthophoto. Missing inputs produce an actionable error rather than silently
enabling extra work. Uncheck Cleanup to retain intermediates while testing.

Cleanup is on by default: once a stage's successor is verified on disk, the
intermediate is removed. See **Cleanup** below.

## Install

    uv sync --frozen

Requires Python 3.14. Alternatively, install the project with `pip install .`.

`numpy` is pinned to 2.3.4 because py3dtiles requires `<2.4` and Python 3.14
otherwise resolves 2.5. PDAL is **not** needed — and cannot pip-install on
Windows anyway.

For uploads, copy `.env.example` to `.env` and fill in the MinIO keys. Without
them the upload checkbox stays disabled.

### Windows executable (no Python installation needed)

Choose from the assets on a GitHub release (both include Python and dependencies):

- **Installer:** `lidar-hd-windows-x64-setup.exe` installs for your Windows user,
  without administrator rights, under `%LOCALAPPDATA%\Programs\LiDAR HD`.
  It adds a Start menu shortcut, an optional desktop shortcut and a Windows
  installed-apps uninstaller. Upgrades use the same location; uninstalling leaves
  your `.env` and downloaded `data` intact. No system PATH changes are made.
- **Portable:** `lidar-hd-windows-x64.zip` needs no installation.

Download `SHA256SUMS.txt` too. In PowerShell, verify your chosen file before
running the installer or extracting the ZIP:

```powershell
$asset = 'lidar-hd-windows-x64-setup.exe' # Or lidar-hd-windows-x64.zip
$line = Get-Content .\SHA256SUMS.txt | Where-Object { ($_ -split '\s+', 2)[1] -eq $asset }
if (@($line).Count -ne 1) { throw 'Missing or duplicate checksum entry' }
$expected = ($line -split '\s+', 2)[0]
$actual = (Get-FileHash $asset -Algorithm SHA256).Hash
if ($actual -ne $expected) { throw 'SHA256 checksum mismatch' }
& .\lidar-hd-windows-x64-setup.exe
```

For the portable ZIP instead:

```powershell
Expand-Archive .\lidar-hd-windows-x64.zip -DestinationPath .\lidar-hd-release
Set-Location .\lidar-hd-release\lidar-hd
& .\lidar-hd.exe --help
& .\lidar-hd.exe
```

Extract and keep the **entire folder**, including `_internal`; do not copy just
`lidar-hd.exe`. This is a PyInstaller **onedir console** application: run it in
a terminal to use the TUI or pass the same CLI options shown below in place of
`python main.py`. Use a writable location for the extracted folder.
The executable is unsigned, so Windows SmartScreen may warn about an unknown
publisher. Only run a release you trust; checksums detect corruption, not publisher
identity.

The packaged `README.md` and `.env.example` are under `_internal`. For uploads,
copy the example next to the executable and edit your credentials:

```powershell
Copy-Item .\_internal\.env.example .\.env
notepad .\.env
```

The frozen application loads `.env` **only beside `lidar-hd.exe`**, not from the
current working directory or `_internal`. Existing environment variables take
precedence over `.env`. Data defaults to `data` beside the executable; set
`LIDARHD_DATA` to choose another location. Personal `.env` files and downloaded
data are never included in the package. Conversion uses the bundled runtime
through the internal `--internal-py3dtiles` entry point, not a system Python;
that switch is not intended for normal use.

### Build the Windows package

Build on **Windows x64** with `uv` installed; PyInstaller does not cross-compile
this package from Linux or macOS. Python 3.14 is managed by `uv`. Native
dependencies may require Visual Studio 2022 Build Tools with the **Desktop
development with C++** workload and Windows SDK when no compatible wheel is
available. In that case, run from a Developer PowerShell with the x64 compiler
environment enabled. The GitHub Windows runner provides these tools and the
workflow enables them.

For the installer, also install **Inno Setup 6** (6.3 or newer). The GitHub
Windows runner already includes it. The build script discovers `ISCC.exe`, or
accepts `--compiler 'C:\path\to\ISCC.exe'` / `INNO_SETUP_COMPILER`.

From the repository root, use PowerShell:

```powershell
uv sync --frozen --group build --python 3.14
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
uv run --frozen --group build python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
uv run --frozen --group build pyinstaller --noconfirm --clean lidar-hd.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
& .\dist\lidar-hd\lidar-hd.exe --self-test
if ($LASTEXITCODE -ne 0) { throw 'Frozen self-test failed' }
& .\dist\lidar-hd\lidar-hd.exe --help
if ($LASTEXITCODE -ne 0) { throw 'Frozen help failed' }
uv run --frozen --group build python scripts\build_installer.py --version v0.1.0
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
New-Item -ItemType Directory -Path artifacts -Force | Out-Null
uv run --frozen --group build python -m zipfile -c artifacts\lidar-hd-windows-x64.zip dist\lidar-hd
if ($LASTEXITCODE -ne 0) { throw 'Archive creation failed' }
$assets = @('lidar-hd-windows-x64.zip', 'lidar-hd-windows-x64-setup.exe')
$assets | ForEach-Object {
    $hash = (Get-FileHash "artifacts\$_" -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $_"
} | Set-Content artifacts\SHA256SUMS.txt -Encoding ascii
```

The installer version defaults to `pyproject.toml` locally; tagged builds use
the `vMAJOR.MINOR.PATCH` tag (for example `v0.1.0`). On a clean Windows machine,
`uv run --frozen --group build python scripts\test_installer.py` tests silent
installation, the installed executable, upgrade and uninstall, including data
and configuration preservation. It refuses to replace an existing installation.

The spec explicitly includes only the documentation and configuration example
as application data, never your `.env` or `data` directory. Package a fresh
build before running real downloads or adding credentials to `dist\lidar-hd`.
Distribute the setup executable or the ZIP, plus the checksum file; never
distribute the inner application executable alone. The installer only includes
the application executable and `_internal`, not adjacent personal files.

### Publish a GitHub release

The **Windows release** workflow installs locked dependencies, runs tests,
builds and smoke-tests the frozen application and installer, then uploads the
setup executable, portable ZIP and `SHA256SUMS.txt`.
Use **Run workflow** in GitHub Actions for a manual build:
manual runs produce workflow artifacts only, even when a tag is selected.

To publish, first commit and push the intended code, including `uv.lock` and
`lidar-hd.spec`, then create and push a new version tag (replace the example
version with your release version):

```powershell
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

Only pushes of `v*` tags publish releases. A separate job with repository
contents write permission verifies the tag and creates the release with
generated notes. Rerunning an existing tag's workflow replaces the three assets
on that release; ordinary branch pushes and manual runs do not publish.

## Command line

    python main.py --level commune --name Pessac --estimate-only
    python main.py --level commune --name Pessac --color --tiles
    python main.py --level departement --name Gironde --estimate-only
    python main.py --level commune --name Pessac --name Talence --estimate-only
    python main.py --level commune --name Pessac --no-download --ortho --no-clean
    python main.py --level commune --name Pessac --color --ortho --tiles --upload
    python main.py --level commune --name Pessac --no-download --tiles --no-clean
    python main.py --level epci --name "Bordeaux Métropole" --estimate-only
    python main.py --level commune --name Pessac --name Talence --merge-name "La CUB" --color --ortho --tiles --upload
    python main.py --level epci --name "Bordeaux Métropole" --no-download --upload --snowball --no-clean

Same code as the TUI, useful for scripting and for long unattended runs.

## Separate outputs or a named merged zone

Leave **Merge selection** unchecked to keep one output per selected area.
Check it and enter a name such as **La CUB**, or use `--merge-name "La CUB"`,
to process the union of the selected areas' 1 km tiles. Shared tiles are counted
and processed only once. With the corresponding stages enabled, this creates
one `3dtiles/tileset.json` and one `ortho/orthophoto.pmtiles`, locally under
`<data>/zone-la-cub/` and remotely under `<bucket>/<prefix>/zone-la-cub/`.
The selection panel shows the merged unique tile count and raw-data estimate.

Names become portable slugs (for example, `Bordeaux Métropole` becomes
`bordeaux-metropole`); paths and empty names are rejected. A local `zone.json`
binds the output folder to its selection and tile coverage. To change either,
choose another name rather than silently mixing old and new outputs.
Fusion runs the pipeline on a combined input set; it does not concatenate
existing 3D Tiles or PMTiles archives. With Download unchecked, inputs must
already exist in the **merged zone's** directory, not individual city folders.
As with individual exports, coverage follows tile footprints, not exact borders.

EPCI includes communautés de communes, communautés d'agglomération and
métropoles, so Bordeaux Métropole can be selected directly without manually
choosing its member communes. Select several EPCI, or combine them with other
levels in the TUI; named fusion removes overlapping tile coverage.
Names, SIREN codes and contours come from the public
[API Découpage administratif](https://geo.api.gouv.fr/decoupage-administratif/epcis).
Contours are reprojected from WGS84 to Lambert-93 before finding LiDAR tiles.

## Optional MinIO TAR batches

Enable **Upload to MinIO** and **Group small uploads (TAR)**, or use
`--upload --snowball`. Normal per-file uploads remain the default.
This uses MinIO's Snowball server-side extraction protocol, **not a ZIP stored
as an ordinary object**. Archive members carry the full destination keys so
`tileset.json` and its referenced files remain individually addressable with
the same directory layout as normal uploads. The TAR object's name is not
the destination prefix.

Use a MinIO server that supports Snowball extraction; a generic S3-compatible
endpoint need not support it. Extraction is checked before reporting success.
If it fails or is unsupported, the run reports errors and keeps local inputs;
it does not silently switch upload modes or delete remote objects.
Large files and PMTiles keep the normal upload path. Grouping targets the many
small 3D Tiles files, not compression of already-compressed imagery. Temporary
disk space is required for each TAR batch (at most 128 MiB or 256 files); the
Python SDK also buffers the single PUT in memory. Files of 16 MiB or more and
PMTiles are uploaded individually. Per-file MIME metadata is not carried by
Snowball; use normal uploads if your serving setup requires those explicit
content types. Resume and extraction verification compare sizes, not checksums.
Network/server limits still apply; a speed-up is not guaranteed.

## What each stage does

| Stage | Output | Notes |
|---|---|---|
| download | `<data>/<level>-<code>/raw/*.copc.laz` | resumable, skips complete files |
| colourise | `.../colorized/*.laz` | IGN BD ORTHO at 20 cm/px, +49% size |
| ortho | `.../ortho/orthophoto.pmtiles` | raster basemap from the same cached 20 cm source |
| convert | `.../3dtiles/` | one tileset over the whole area |
| upload | `<bucket>/<prefix>/<level>-<code>/<output>/` | keeps `3dtiles/`, `raw/` or `colorized/`, and `ortho/` separate |

Data lands under `LIDARHD_DATA`, defaulting to `data/` beside the project when
running from source, or beside `lidar-hd.exe` in the Windows package.

## Orthophoto basemap

Enable **Export ortho PMTiles**, or pass `--ortho`. It works independently of
LiDAR download and colourisation. Both colourisation and raster export reuse
the same 1 km IGN BD ORTHO JPEGs in `ortho-cache/`, requested at 5000 × 5000
pixels (20 cm sampling). The cache is retained for repeat processing; it and
the PMTiles archive are additional to the displayed LiDAR size estimates.

The export reprojects imagery to Web Mercator, builds a zoom pyramid, and
packages it as a range-readable raster PMTiles archive. Its display pixel
spacing is not additional source detail: 20 cm sampling cannot improve the
native imagery or its capture date. Coverage follows the selected 1 km tiles,
not a precisely clipped commune boundary. Large areas can take considerable
time and disk space; test a small commune first.

Enable Upload to send the archive beside your point-cloud output:

    <bucket>/<prefix>/<level>-<code>/ortho/orthophoto.pmtiles

Upload-only runs also discover an existing archive. Incomplete `.part` files
and conversion scratch directories are not uploaded. Unlike the old flattened
upload layout, output subdirectories are now preserved; update existing viewer
URLs to include `3dtiles/` where appropriate.

Use the PMTiles JavaScript protocol with a **raster** source in a compatible
client such as MapLibre. Mapbox does not directly understand `pmtiles://`;
use a PMTiles-to-XYZ tile server if you must keep a Mapbox client. Object storage
must permit HTTP Range GET requests and CORS from your map application's
origin (including the Range request header and exposed Content-Range/ETag
response headers). Keep IGN attribution visible. The existing `viewer.html`
is unchanged; this feature exports/uploads the basemap for your map application.

## Tests and demo

    uv run python -m unittest discover -s tests

The tests use small synthetic data and mocked network services, without real
MinIO credentials or large LiDAR downloads. `.junie/demo.md` documents the
confirmed xterm launch and a bounded small-commune demo. Review that file and
the VM Dockerfile, then run `/demo`; uploads are disabled in the demo environment.

## Cleanup

After a successful run the intermediates are deleted automatically:

| Outcome | Kept | Removed |
|---|---|---|
| convert succeeded | `3dtiles/` | `raw/`, `colorized/` |
| convert not requested | `colorized/` | `raw/` |
| convert requested but failed | everything | nothing |
| run stopped early | everything | scratch only |

A stage is only removed once what is derived from it has been **verified on
disk** — a tileset counts as finished when `tileset.json` exists, `.pnts` files
are present, and py3dtiles' `tmp/` scratch directory is gone. An interrupted
conversion therefore keeps its inputs, so it can be retried without
re-downloading.

Failed HTTP downloads remove their `.part` files. Cleanup sweeps remaining
scratch files, but a stopped or failed pipeline keeps its inputs. The shared
orthophoto cache and completed PMTiles output are retained, including when
LiDAR intermediates are cleaned.

Pass `--no-clean` (CLI) or untick "Clean intermediates" (TUI) to keep
everything — useful while iterating, since re-running a kept stage is instant
and re-downloading is not.

## Viewing

    python serve.py C:\lidar\data\commune-33318 --port 8103
    # http://localhost:8103/viewer.html?url=3dtiles/tileset.json

`serve.py` exists because a tileset needs HTTP range requests, `.pnts` MIME
types and CORS — `python -m http.server` provides none of those. Opening
`viewer.html` from the filesystem does not work: `file://` is a unique origin
and `fetch()` is blocked.

Keep the browser tab in the **foreground**. Chrome throttles
`requestAnimationFrame` in hidden tabs, and deck.gl drives both rendering and
tile traversal from it, so a backgrounded tab silently loads nothing.

## Design notes

**Everything network-facing is sequential.** IGN rate-limits concurrent
requests: parallel batches come back empty or 403, which looks like corruption
rather than throttling. Do not add a thread pool to the download loop.

**Block resolution is footprint-filtered.** A tile lives in exactly one of ~223
delivery blocks and its name does not say which. Probing blindly took 25 s per
tile; intersecting the area's bbox with each block's published footprint first
brings that to ~0.1 s.

**Tile coverage uses scanline fill**, not point-in-polygon per tile. A region is
~87k tiles against a boundary with tens of thousands of edges; the naive form
takes minutes, this takes 0.2 s.

**Estimates are approximate by design** — tile count × 115.7 MB, the mean of a
63-tile sample. True spread is about ±8% (tiles run 6 MB to 300 MB depending on
terrain). Exact sizes are only known once each tile is resolved, which is why
the estimate is instant.

**Source tiles are LAS point format 6**, which has no RGB fields. Colour means
rewriting every point into format 7, hence the +49%.

**Conversion is one py3dtiles call for the whole area.** Converting per-tile
would give unrelated tilesets whose LODs disagree at the seams.

## Known limits

- The orthophoto is nadir, so building facades inherit roof-edge colour and look
  streaky. Classification colouring in the viewer avoids this.
- Elevation colouring bands on coarse LOD tiles: they span ~1 m of local z while
  their origins differ by tens of metres, so each renders flat. Clears on zoom.
- Detail changes in the viewer only take effect once the camera moves — the
  tileset re-runs traversal on viewport change.
- A region-scale job is days of wall time. Estimate first.

## Layout

    main.py            TUI + CLI
    serve.py           dev server for the viewer
    viewer.html        deck.gl Tile3DLayer viewer
    lidar_hd/
      config.py        endpoints, measured constants, MinIO settings
      http.py          sequential GET/HEAD/download with retries
      areas.py         admin area search, tile grid
      catalog.py       delivery blocks, tile -> block resolution + cache
      pipeline.py      download / colourise / convert / upload
      jobs.py          independently selectable stages, shared by TUI and CLI
      ortho.py         shared 20 cm imagery cache and raster PMTiles export
      tui.tcss         split-pane terminal styling
