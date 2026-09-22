# lidarhd

Download IGN LiDAR HD for French communes, departments or regions; optionally
drape orthophoto colour, export a 20 cm raster basemap, convert to 3D Tiles,
and mirror to MinIO.

    uv run python main.py

Browse regions and departments immediately, or search communes by name.
Use **Space** or **Enter** on a row to toggle it in the selection queue. Select
multiple places, even across searches and administrative levels; they run
sequentially, each in its own output directory. The estimate appears before
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

## Command line

    python main.py --level commune --name Pessac --estimate-only
    python main.py --level commune --name Pessac --color --tiles
    python main.py --level departement --name Gironde --estimate-only
    python main.py --level commune --name Pessac --name Talence --estimate-only
    python main.py --level commune --name Pessac --no-download --ortho --no-clean
    python main.py --level commune --name Pessac --color --ortho --tiles --upload
    python main.py --level commune --name Pessac --no-download --tiles --no-clean

Same code as the TUI, useful for scripting and for long unattended runs.

## What each stage does

| Stage | Output | Notes |
|---|---|---|
| download | `<data>/<level>-<code>/raw/*.copc.laz` | resumable, skips complete files |
| colourise | `.../colorized/*.laz` | IGN BD ORTHO at 20 cm/px, +49% size |
| ortho | `.../ortho/orthophoto.pmtiles` | raster basemap from the same cached 20 cm source |
| convert | `.../3dtiles/` | one tileset over the whole area |
| upload | `<bucket>/<prefix>/<level>-<code>/<output>/` | keeps `3dtiles/`, `raw/` or `colorized/`, and `ortho/` separate |

Data lands under `LIDARHD_DATA`, defaulting to `data/` beside the project.

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
