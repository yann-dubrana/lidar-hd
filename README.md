# lidarhd

Download IGN LiDAR HD for any French commune, department or region; optionally
drape orthophoto colour, convert to 3D Tiles, and mirror to MinIO.

    python main.py

Pick a level, type a name, select a row. The size estimate appears immediately —
before anything is downloaded.

```
Bordeaux (33063) · 50 km²
73 tiles ≈ 8.2 GB raw (±8%)
+ colourised 12.3 GB + 3D Tiles 28.0 GB = 48.6 GB if all stages run
~0.3 h download · ~1.0 h processing
```

Only the download runs by default. Colourise / convert / upload are opt-in
checkboxes, so nothing surprising happens to a terabyte.

## Install

    pip install -r requirements.txt

`numpy` is pinned to 2.3.4 because py3dtiles requires `<2.4` and Python 3.14
otherwise resolves 2.5. PDAL is **not** needed — and cannot pip-install on
Windows anyway.

For uploads, copy `.env.example` to `.env` and fill in the MinIO keys. Without
them the upload checkbox stays disabled.

## Command line

    python main.py --level commune --name Pessac --estimate-only
    python main.py --level commune --name Pessac --color --tiles
    python main.py --level departement --name Gironde --estimate-only

Same code as the TUI, useful for scripting and for long unattended runs.

## What each stage does

| Stage | Output | Notes |
|---|---|---|
| download | `<data>/<level>-<code>/raw/*.copc.laz` | resumable, skips complete files |
| colourise | `.../colorized/*.laz` | IGN BD ORTHO at 20 cm/px, +49% size |
| convert | `.../3dtiles/` | one tileset over the whole area |
| upload | `<bucket>/<prefix>/<level>-<code>/` | skips objects already present |

Data lands under `LIDARHD_DATA` (default `C:\lidar\data`).

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
    lidarhd/
      config.py        endpoints, measured constants, MinIO settings
      http.py          sequential GET/HEAD/download with retries
      areas.py         admin area search, tile grid
      catalog.py       delivery blocks, tile -> block resolution + cache
      pipeline.py      download / colourise / convert / upload
