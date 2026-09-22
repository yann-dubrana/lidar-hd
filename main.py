#!/usr/bin/env python
"""LiDAR HD — pick an area, see the size, run the pipeline.

    python main.py                  interactive TUI
    python main.py --help           command line usage
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# The Windows console defaults to cp1252, which cannot encode the characters
# used in the summaries (km², ≈, ·). Force UTF-8 for our own output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from lidar_hd import areas, pipeline                       # noqa: E402
from lidar_hd.areas import Area, Level                     # noqa: E402
from lidar_hd.catalog import Catalog                       # noqa: E402
from lidar_hd.config import data_root, minio_config        # noqa: E402

from textual import on, work                              # noqa: E402
from textual.app import App, ComposeResult                # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header,  # noqa: E402
                             Input, Label, Log, RadioButton, RadioSet, Static)


def bbox_of(tiles: list[tuple[int, int]]) -> tuple[float, float, float, float]:
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    return (min(xs) * 1000, (min(ys) - 1) * 1000, (max(xs) + 1) * 1000, max(ys) * 1000)


class LidarApp(App):
    CSS = """
    Screen { layout: vertical; }
    #top { height: auto; padding: 1 2; }
    #search-row { height: 3; }
    #term { width: 1fr; }
    #results { height: 12; margin: 1 0; }
    #summary { height: auto; padding: 1 2; background: $boost; }
    #summary.ready { border-left: thick $success; }
    #opts { height: auto; padding: 0 2; }
    #actions { height: 3; padding: 0 2; }
    #log { height: 1fr; margin: 1 2; border: round $primary; }
    .dim { color: $text-muted; }
    Button { margin-right: 1; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "run", "Run"), ("escape", "stop", "Stop")]

    def __init__(self) -> None:
        super().__init__()
        self.catalog = Catalog.load(data_root() / "catalog.json")
        self.found: list[Area] = []
        self.selected: Area | None = None
        self.tiles: list[tuple[int, int]] = []
        self.est: pipeline.Estimate | None = None
        self._stop = threading.Event()

    # --- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="top"):
            with RadioSet(id="level"):
                yield RadioButton("Commune", value=True, id="commune")
                yield RadioButton("Department", id="departement")
                yield RadioButton("Region", id="region")
            with Horizontal(id="search-row"):
                yield Input(placeholder="Type a name, e.g. Pessac — then Enter",
                            id="term")
                yield Button("Search", id="search", variant="primary")
        yield DataTable(id="results", cursor_type="row")
        yield Static("Select an area to see an estimate.", id="summary")
        with Horizontal(id="opts"):
            yield Checkbox("Colourise (ortho RGB)", id="do_color")
            yield Checkbox("Convert to 3D Tiles", id="do_tiles")
            yield Checkbox("Upload to MinIO", id="do_upload")
        with Horizontal(id="actions"):
            yield Button("Run", id="run", variant="success", disabled=True)
            yield Button("Stop", id="stop", variant="error", disabled=True)
        yield Log(id="log", highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "LiDAR HD"
        self.sub_title = "IGN Géoplateforme → COPC → 3D Tiles → MinIO"
        table = self.query_one("#results", DataTable)
        table.add_columns("Name", "Code", "Population")
        self.query_one("#term", Input).focus()

        cfg = minio_config()
        if not cfg.configured:
            self.query_one("#do_upload", Checkbox).disabled = True
            self.log_line("MinIO not configured — set MINIO_ENDPOINT / "
                          "MINIO_ACCESS_KEY / MINIO_SECRET_KEY in .env to enable upload.")
        self.log_line(f"Data directory: {data_root()}")

    def log_line(self, text: str) -> None:
        self.query_one("#log", Log).write_line(text)

    # --- search ------------------------------------------------------------

    @property
    def level(self) -> Level:
        pressed = self.query_one("#level", RadioSet).pressed_button
        return (pressed.id if pressed else "commune")  # type: ignore[return-value]

    @on(Button.Pressed, "#search")
    @on(Input.Submitted, "#term")
    def do_search(self) -> None:
        term = self.query_one("#term", Input).value.strip()
        if term:
            self.search_worker(self.level, term)

    @work(thread=True, exclusive=True)
    def search_worker(self, level: Level, term: str) -> None:
        self.call_from_thread(self.log_line, f"Searching {level} '{term}'…")
        try:
            found = areas.search(level, term)
        except Exception as e:                       # noqa: BLE001
            self.call_from_thread(self.log_line, f"Search failed: {e}")
            return
        self.call_from_thread(self.show_results, found)

    def show_results(self, found: list[Area]) -> None:
        self.found = found
        table = self.query_one("#results", DataTable)
        table.clear()
        for a in found:
            table.add_row(a.name, a.code,
                          f"{a.population:,}".replace(",", " ") if a.population else "—")
        self.log_line(f"{len(found)} match(es).")
        if found:
            table.focus()

    @on(DataTable.RowSelected, "#results")
    def pick(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.found):
            self.estimate_worker(self.found[event.cursor_row])

    # --- estimate ----------------------------------------------------------

    @work(thread=True, exclusive=True)
    def estimate_worker(self, area: Area) -> None:
        self.call_from_thread(self.log_line, f"Measuring {area.label}…")
        try:
            geom = areas.geometry(area)
            km2 = areas.area_km2(geom)
            tiles = areas.tiles_for(geom)
        except Exception as e:                       # noqa: BLE001
            self.call_from_thread(self.log_line, f"Failed: {e}")
            return
        est = pipeline.estimate(len(tiles))
        self.call_from_thread(self.show_estimate, area, tiles, km2, est)

    def show_estimate(self, area: Area, tiles: list[tuple[int, int]],
                      km2: float, est: pipeline.Estimate) -> None:
        self.selected, self.tiles, self.est = area, tiles, est
        h = pipeline.human
        self.query_one("#summary", Static).update(
            f"[b]{area.name}[/b] ({area.code}) · {km2:,.0f} km²\n".replace(",", " ")
            + f"[b]{len(tiles):,}[/b] tiles ≈ [b]{h(est.raw_bytes)}[/b] raw "
              .replace(",", " ")
            + f"(±8%)\n"
            + f"[dim]+ colourised {h(est.colorized_bytes)} "
              f"+ 3D Tiles {h(est.tiles3d_bytes)} = {h(est.total_bytes)} if all stages run[/dim]\n"
            + f"[dim]~{est.download_hours:.1f} h download"
              f" · ~{est.process_hours:.1f} h processing[/dim]"
        )
        self.query_one("#summary", Static).add_class("ready")
        self.query_one("#run", Button).disabled = False
        self.log_line(f"{area.name}: {len(tiles)} tiles, ~{h(est.raw_bytes)}")

    # --- run ---------------------------------------------------------------

    @on(Button.Pressed, "#run")
    def action_run(self) -> None:
        if not self.selected or not self.tiles:
            return
        self._stop.clear()
        self.query_one("#run", Button).disabled = True
        self.query_one("#stop", Button).disabled = False
        self.run_worker_thread(
            self.selected, self.tiles,
            self.query_one("#do_color", Checkbox).value,
            self.query_one("#do_tiles", Checkbox).value,
            self.query_one("#do_upload", Checkbox).value,
        )

    @on(Button.Pressed, "#stop")
    def action_stop(self) -> None:
        self._stop.set()
        self.log_line("Stopping after the current tile…")

    @work(thread=True, exclusive=True)
    def run_worker_thread(self, area: Area, tiles: list[tuple[int, int]],
                          do_color: bool, do_tiles: bool, do_upload: bool) -> None:
        log = lambda s: self.call_from_thread(self.log_line, s)    # noqa: E731
        root = data_root() / f"{area.level}-{area.code}"
        names = areas.tile_names(tiles)

        def progress(stage: str, done: int, total: int, detail: str) -> None:
            if done % 10 == 0 or done == total or "FAIL" in detail:
                log(f"[{stage} {done}/{total}] {detail}")

        # Narrow the block list to those covering this area: turns tile
        # resolution from ~25 s/tile into ~0.1 s/tile.
        log(f"Resolving delivery blocks for {area.name}…")
        self.catalog.fetch_blocks()
        n = self.catalog.prioritise(bbox_of(tiles))
        log(f"{n} block(s) cover this area.")

        raw = root / "raw"
        r = pipeline.download_tiles(names, raw, self.catalog,
                                    progress, self._stop.is_set)
        self.catalog.save(data_root() / "catalog.json")
        log(f"Download: {r.ok} new, {r.skipped} present, {len(r.failed)} failed "
            f"({pipeline.human(r.bytes_moved)} in {r.seconds / 60:.1f} min)")

        source = raw
        if do_color and not self._stop.is_set():
            col = root / "colorized"
            r = pipeline.colorize_all(raw, col, progress, self._stop.is_set)
            log(f"Colourise: {r.ok} done, {r.skipped} present, {len(r.failed)} failed "
                f"({r.seconds / 60:.1f} min)")
            source = col

        tiles3d = root / "3dtiles"
        if do_tiles and not self._stop.is_set():
            pattern = "*.laz" if source.name == "colorized" else "*.copc.laz"
            inputs = sorted(source.glob(pattern))
            r = pipeline.convert_3dtiles(inputs, tiles3d, progress=progress)
            if r.failed:
                log(f"Convert FAILED: {r.failed[0]}")
            else:
                log(f"Convert: {r.ok} tiles -> {pipeline.human(r.bytes_moved)} "
                    f"({r.seconds / 60:.1f} min)")

        if do_upload and not self._stop.is_set():
            cfg = minio_config()
            target = tiles3d if tiles3d.exists() else source
            prefix = f"{cfg.prefix}/{area.level}-{area.code}"
            log(f"Uploading {target.name} → {cfg.bucket}/{prefix}")
            r = pipeline.upload_dir(target, prefix, cfg, progress, self._stop.is_set)
            log(f"Upload: {r.ok} objects, {r.skipped} present, {len(r.failed)} failed "
                f"({pipeline.human(r.bytes_moved)})")

        log("Done." if not self._stop.is_set() else "Stopped.")
        self.call_from_thread(self._finish)

    def _finish(self) -> None:
        self.query_one("#run", Button).disabled = False
        self.query_one("#stop", Button).disabled = True


# --- command line ----------------------------------------------------------

def cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", choices=["commune", "departement", "region"],
                    help="administrative level")
    ap.add_argument("--name", help="area name (exact or prefix)")
    ap.add_argument("--estimate-only", action="store_true",
                    help="print the estimate and exit")
    ap.add_argument("--color", action="store_true", help="colourise after download")
    ap.add_argument("--tiles", action="store_true", help="convert to 3D Tiles")
    ap.add_argument("--upload", action="store_true", help="upload to MinIO")
    args = ap.parse_args(argv)

    if not args.level or not args.name:
        LidarApp().run()
        return 0

    found = areas.search(args.level, args.name)
    if not found:
        print(f"No {args.level} matching '{args.name}'")
        return 1

    area = found[0]
    geom = areas.geometry(area)
    tiles = areas.tiles_for(geom)
    est = pipeline.estimate(len(tiles))
    h = pipeline.human
    print(f"{area.label}  {areas.area_km2(geom):,.0f} km²".replace(",", " "))
    print(f"  {len(tiles):,} tiles ≈ {h(est.raw_bytes)} raw (±8%)".replace(",", " "))
    print(f"  all stages: {h(est.total_bytes)}")
    print(f"  ~{est.download_hours:.1f} h download, ~{est.process_hours:.1f} h processing")
    if args.estimate_only:
        return 0

    root = data_root() / f"{area.level}-{area.code}"
    catalog = Catalog.load(data_root() / "catalog.json")
    catalog.fetch_blocks()
    catalog.prioritise(bbox_of(tiles))

    def progress(stage: str, done: int, total: int, detail: str) -> None:
        print(f"[{stage} {done}/{total}] {detail}", flush=True)

    r = pipeline.download_tiles(areas.tile_names(tiles), root / "raw",
                                catalog, progress)
    catalog.save(data_root() / "catalog.json")
    print(f"Download: {r.ok} new, {r.skipped} present, {len(r.failed)} failed")

    source = root / "raw"
    if args.color:
        r = pipeline.colorize_all(source, root / "colorized", progress)
        print(f"Colourise: {r.ok} done, {len(r.failed)} failed")
        source = root / "colorized"
    if args.tiles:
        pattern = "*.laz" if source.name == "colorized" else "*.copc.laz"
        r = pipeline.convert_3dtiles(sorted(source.glob(pattern)),
                                     root / "3dtiles", progress=progress)
        print(f"Convert: {'failed: ' + r.failed[0] if r.failed else 'ok'}")
    if args.upload:
        cfg = minio_config()
        if not cfg.configured:
            print("MinIO not configured (see .env.example)")
            return 1
        target = root / "3dtiles" if (root / "3dtiles").exists() else source
        r = pipeline.upload_dir(target, f"{cfg.prefix}/{area.level}-{area.code}",
                                cfg, progress)
        print(f"Upload: {r.ok} objects, {len(r.failed)} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
