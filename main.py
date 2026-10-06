#!/usr/bin/env python
"""LiDAR HD — pick an area, see the size, run the pipeline.

    python main.py                  interactive TUI
    python main.py --help           command line usage
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque

from rich.markup import escape

# The Windows console defaults to cp1252, which cannot encode the characters
# used in the summaries (km², ≈, ·). Force UTF-8 for our own output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from lidar_hd import areas, jobs, pipeline                 # noqa: E402
from lidar_hd.areas import Area, Level                     # noqa: E402
from lidar_hd.catalog import Catalog                       # noqa: E402
from lidar_hd.config import data_root, minio_config        # noqa: E402

from textual import on, work                              # noqa: E402
from textual.app import App, ComposeResult                # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.theme import Theme                           # noqa: E402
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header,  # noqa: E402
                             Input, Label, Log, ProgressBar, RadioButton, RadioSet, Static)


class AreaTable(DataTable):
    BINDINGS = [("space", "select_cursor", "Toggle selection")]


class LidarApp(App):
    CSS_PATH = "lidar_hd/tui.tcss"
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+r", "run", "Run"),
                ("escape", "stop", "Stop"), ("slash", "search", "Search"),
                ("ctrl+f", "search", "Search")]
    LOCKED_WHILE_RUNNING = ("#level", "#term", "#search", "#results", "#opts", "#clear-selection",
                            "#batch-options", "#site-options")

    def __init__(self) -> None:
        super().__init__()
        self.catalog = Catalog.load(data_root() / "catalog.json")
        self.found: list[Area] = []
        self._stop = threading.Event()
        self._run_lock = threading.Event()
        self._search_generation = 0
        self._estimate_generation = 0
        self._search_timer = None
        self._browse_cache: dict[Level, list[Area]] = {}
        self._stages: list[str] = []
        self._stage_fractions: dict[str, float] = {}
        self._download_done = 0
        self._transfer_bytes = 0
        self._transfer_samples: deque[tuple[float, int]] = deque()
        self._transfers: dict[str, tuple[int, int | None]] = {}
        self._restarts = 0
        self._selection: dict[tuple[str, str], Area] = {}
        self._prepared: dict[tuple[str, str], tuple[Area, list[tuple[int, int]]]] = {}
        self._estimate_requests: dict[tuple[str, str], int] = {}
        self._estimate_lock = threading.Lock()
        self._batch_index = 0
        self._batch_total = 1
        self._batch_name = ""
        self._download_total = 0
        self._site: tuple[jobs.Site, list[tuple[int, int]]] | None = None
        self.register_theme(Theme(
            name="lidar-night", primary="#91d7c0", secondary="#c5afe8",
            accent="#91d7c0", foreground="#d8dbe8", background="#1c1d27",
            surface="#232530", panel="#232530", success="#91d7c0",
            warning="#e5c38e", error="#ef9aa4", dark=True,
        ))
        self.theme = "lidar-night"

    # --- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="workspace"):
            with Horizontal(id="top"):
                with VerticalScroll(id="navigation", classes="pane"):
                    yield Label("EXPLORE", classes="eyebrow")
                    with RadioSet(id="level"):
                        yield RadioButton("Region", value=True, id="region")
                        yield RadioButton("Department", id="departement")
                        yield RadioButton("Commune", id="commune")
                        yield RadioButton("EPCI", id="epci", tooltip="Intercommunalities: communautés de communes, agglomérations, métropoles")
                    yield Static("IGN LiDAR HD\nFrance · 1 km tiles", classes="dim", id="source-note")
                    yield Static("↑ ↓  Browse\nSpace  Toggle\nEnter  Toggle\n/  Search\nTab  Next pane", classes="dim", id="key-guide")
                with Vertical(id="browser", classes="pane"):
                    with Horizontal(id="search-row"):
                        yield Input(placeholder="Filter regions by name…", id="term")
                        yield Button("Search", id="search")
                    yield Static("Loading regions…", id="results-status", markup=False)
                    yield AreaTable(id="results", cursor_type="row", zebra_stripes=True)
                    with Vertical(id="site-options"):
                        yield Input(placeholder="Or an enriched file: path to a .las / .laz", id="site-file")
                        with Horizontal(id="site-row"):
                            yield Input(placeholder="LiDAR HD around, m", id="site-buffer",
                                        tooltip="Metres of LiDAR HD around the file. Empty = the file alone.")
                            yield Input(placeholder="EPSG", id="site-srs",
                                        tooltip="Only when the file declares no CRS and detection is wrong.")
                        yield Static("Paste a path to convert your own point cloud, alone or set into LiDAR HD.",
                                     id="site-note", classes="dim", markup=False)
                        yield Checkbox("Align altitude on LiDAR HD", id="z-align",
                                       tooltip="Shift the file by its measured altitude difference with LiDAR HD.")
                        yield Checkbox("Keep duplicated LiDAR HD", id="keep-overlap",
                                       tooltip="Keep the LiDAR HD points that share a 1 m cube with the file.")
                with Vertical(id="details", classes="pane"):
                    with VerticalScroll(id="details-scroll"):
                        yield Static("0 areas selected", id="selection-count")
                        yield Static("Selections stay while you search.", id="selected-areas", markup=False)
                        yield Button("Clear selection", id="clear-selection")
                        with Vertical(id="batch-options"):
                            yield Checkbox("Merge selection", id="merge")
                            yield Input(placeholder="Zone name, e.g. La CUB", id="zone-name", disabled=True)
                            yield Static("Separate outputs for each area.", id="output-note", classes="dim", markup=False)
                        yield Static("Choose an area\n\nSelect a place from the list to see its tile count and storage estimate.", id="summary")
                        yield Label("PIPELINE", classes="eyebrow")
                        yield Static("Unchecked stages reuse existing files.", classes="dim")
                        with Vertical(id="opts"):
                            yield Checkbox("Download LiDAR", id="do_download", value=True)
                            yield Checkbox("Colourise (20 cm ortho)", id="do_color")
                            yield Checkbox("Export ortho PMTiles", id="do_ortho")
                            yield Checkbox("Convert to 3D Tiles", id="do_tiles")
                            yield Checkbox("Upload to MinIO", id="do_upload")
                            yield Checkbox("Complete tileset TAR", id="do_snowball", disabled=True,
                                           tooltip="One streamed TAR including large tiles; extracted by MinIO. PMTiles stay separate.")
                            yield Checkbox("Clean intermediates", id="do_clean", value=True)
                        yield Static("", id="upload-note", classes="dim")
                    with Horizontal(id="actions"):
                        yield Button("Run", id="run", variant="primary", disabled=True)
                        yield Button("Stop", id="stop", disabled=True)
            with Horizontal(id="progress-row"):
                with Vertical(id="overall-pane", classes="pane"):
                    yield Static("Ready · select an area", id="overall-label", markup=False)
                    yield ProgressBar(total=100, show_eta=False, id="overall-progress")
                    yield Static("Enabled stages, equally weighted; not elapsed time.", id="stage-label", classes="dim", markup=False)
                with Vertical(id="download-pane", classes="pane"):
                    yield Static("No active transfer", id="download-label", markup=False)
                    yield ProgressBar(total=100, show_eta=False, id="download-progress")
                    yield Static("Bytes received · speed", id="download-detail", classes="dim", markup=False)
            yield Log(id="log", highlight=False, max_lines=1000)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "LiDAR HD"
        self.sub_title = "Explore · download · build"
        for selector, title in (("#navigation", "Area level"), ("#browser", "Places"),
                                ("#details", "Selection"), ("#overall-pane", "Overall progress"),
                                ("#download-pane", "Download"), ("#log", "Activity")):
            self.query_one(selector).border_title = title
        table = self.query_one("#results", DataTable)
        table.add_columns("", "Place", "Code")
        self.query_one("#term", Input).focus()

        cfg = minio_config()
        if not cfg.configured:
            self.query_one("#do_upload", Checkbox).disabled = True
            self.query_one("#upload-note", Static).update("Upload unavailable: configure MinIO in .env.")
        self.log_line(f"Data directory: {data_root()}")
        self.do_search()

    def on_resize(self, event) -> None:
        self.screen.set_class(event.size.width < 110, "narrow")

    def log_line(self, text: str) -> None:
        self.query_one("#log", Log).write_line(text)

    # --- search ------------------------------------------------------------

    @property
    def level(self) -> Level:
        pressed = self.query_one("#level", RadioSet).pressed_button
        return (pressed.id if pressed else "commune")  # type: ignore[return-value]

    def action_search(self) -> None:
        if not self._run_lock.is_set():
            self.query_one("#term", Input).focus()

    def clear_selection(self, *, area_preview: bool = False) -> None:
        # Textual calls this for text selection, including before widgets mount.
        if not area_preview:
            super().clear_selection()
            return
        self.query_one("#summary", Static).update("Choose an area\n\nSelect a place to see its estimate.")
        self.refresh_selection()

    def refresh_selection(self) -> None:
        count = len(self._selection)
        pending = count - len(self._prepared)
        suffix = f" · {pending} not ready" if pending else ""
        self.query_one("#selection-count", Static).update(f"{count} areas selected{suffix}")
        self.query_one("#selected-areas", Static).update(
            " · ".join(a.name for a in self._selection.values()) or "Selections stay while you search.")
        merging = self.query_one("#merge", Checkbox).value
        self.query_one("#zone-name", Input).disabled = not merging
        upload = self.query_one("#do_upload", Checkbox)
        self.query_one("#do_snowball", Checkbox).disabled = not upload.value or upload.disabled
        valid = True
        note = "Separate outputs for each area."
        if merging:
            try:
                code = jobs.zone_code(self.query_one("#zone-name", Input).value)
                tiles = {t for _, coverage in self._prepared.values() for t in coverage}
                note = f"zone-{code}/ · {len(tiles)} unique tiles"
                if pending:
                    note += " (waiting for estimates)"
                elif tiles:
                    note += f" · ≈ {pipeline.human(pipeline.estimate(len(tiles)).raw_bytes)} raw"
            except ValueError as e:
                note, valid = str(e), False
        self.query_one("#output-note", Static).update(note)
        self.refresh_site()
        ready = bool(self._site) if self.site_fields()[0] else bool(count and not pending and valid)
        self.query_one("#run", Button).disabled = (self._run_lock.is_set() or not ready
                                                   or not self.options().stages)
        table = self.query_one("#results", DataTable)
        for row, area in enumerate(self.found):
            if row < table.row_count:
                table.update_cell_at((row, 0), "[✓]" if (area.level, area.code) in self._selection else "[ ]")

    def site_fields(self) -> tuple[str, str, str]:
        return tuple(self.query_one(f"#site-{name}", Input).value.strip()      # type: ignore[return-value]
                     for name in ("file", "buffer", "srs"))

    def refresh_site(self) -> None:
        """Plan the enriched file from its header; an empty path means area mode."""
        path, buffer, srs = self.site_fields()
        self._site, note = None, "Paste a path to convert your own point cloud, alone or set into LiDAR HD."
        if path:
            try:
                self._site = jobs.plan_site(path, float(buffer) if buffer else None,
                                            int(srs) if srs else None)
                found, tiles = self._site
                lon, lat = found.centre
                note = (f"EPSG:{found.epsg} · centre {lat:.5f}, {lon:.5f}\n"
                        + (f"{len(tiles)} LiDAR HD tiles, cropped to the perimeter" if found.clip
                           else "File only, no LiDAR HD") + f" → site-{found.code}/")
            except Exception as e:                       # noqa: BLE001 - shown beside the field
                note = str(e)
        self.query_one("#site-note", Static).update(note)

    @on(Button.Pressed, "#clear-selection")
    def clear_queue(self) -> None:
        if not self._run_lock.is_set():
            self._selection.clear()
            self._prepared.clear()
            self._estimate_requests.clear()
            self.clear_selection(area_preview=True)

    @on(Checkbox.Changed)
    @on(Input.Changed, "#site-file,#site-buffer,#site-srs,#zone-name")
    def options_changed(self) -> None:
        self.refresh_selection()

    def options(self) -> jobs.Options:
        return jobs.Options(**{stage: self.query_one(f"#do_{suffix}", Checkbox).value
                               for stage, suffix in (("download", "download"), ("colorize", "color"),
                                                     ("ortho", "ortho"), ("convert", "tiles"),
                                                     ("upload", "upload"), ("cleanup", "clean"),
                                                     ("snowball", "snowball"))},
                            z_align=self.query_one("#z-align", Checkbox).value,
                            keep_overlap=self.query_one("#keep-overlap", Checkbox).value)

    @on(RadioSet.Changed, "#level")
    def change_level(self) -> None:
        if self._run_lock.is_set():
            return
        self.query_one("#term", Input).value = ""
        self.query_one("#term", Input).placeholder = (
            "Search communes, e.g. Pessac…" if self.level == "commune"
            else "Filter EPCI, e.g. Bordeaux…" if self.level == "epci"
            else f"Filter {areas.LEVELS[self.level].lower()}s by name…")
        self.do_search()

    @on(Input.Changed, "#term")
    def filter_changed(self) -> None:
        if self._run_lock.is_set():
            return
        self._search_generation += 1
        self.clear_selection(area_preview=True)
        self.found = []
        self.query_one("#results", DataTable).clear()
        if self._search_timer:
            self._search_timer.stop()
        self._search_timer = self.set_timer(0.35, self.do_search)

    @on(Button.Pressed, "#search")
    @on(Input.Submitted, "#term")
    def do_search(self) -> None:
        if self._run_lock.is_set():
            return
        if self._search_timer:
            self._search_timer.stop()
        self._search_generation += 1
        generation = self._search_generation
        self.clear_selection(area_preview=True)
        term = self.query_one("#term", Input).value.strip()
        self.found = []
        self.query_one("#results", DataTable).clear()
        if self.level == "commune" and not term:
            self.query_one("#results-status", Static).update("Type a commune name to search. Regions, departments and EPCI can be browsed directly.")
            return
        self.query_one("#results-status", Static).update("Loading places…")
        if self.level in self._browse_cache:
            self.show_results(self._browse_cache[self.level], generation, term)
        else:
            self.search_worker(self.level, term, generation)

    @work(thread=True, exclusive=True, group="search")
    def search_worker(self, level: Level, term: str, generation: int) -> None:
        try:
            found = areas.search(level, term) if level == "commune" else areas.browse(level)
        except Exception as e:                       # noqa: BLE001
            self.call_from_thread(self.search_failed, generation, str(e))
            return
        self.call_from_thread(self.show_results, found, generation, term, level)

    def search_failed(self, generation: int, error: str) -> None:
        if generation == self._search_generation:
            self.query_one("#results-status", Static).update("Could not load places. Press Search to retry.")
            self.log_line(f"Search failed: {error}")

    def show_results(self, found: list[Area], generation: int, term: str = "",
                     level: Level | None = None) -> None:
        if generation != self._search_generation or self._run_lock.is_set():
            return
        if level and level != "commune":
            self._browse_cache[level] = found
        if self.level != "commune":
            found = [a for a in found if term.casefold() in a.name.casefold() or term in a.code]
        self.found = found
        table = self.query_one("#results", DataTable)
        table.clear()
        for a in found:
            table.add_row("[✓]" if (a.level, a.code) in self._selection else "[ ]", a.name, a.code)
        count = f"{len(found)} places" if len(found) != 25 or self.level != "commune" else "First 25 matches; refine your search"
        self.query_one("#results-status", Static).update(
            f"{count} · Tab to list, Space / Enter to toggle" if found
            else "No places found. Try a different name.")

    @on(DataTable.RowSelected, "#results")
    def pick(self, event: DataTable.RowSelected) -> None:
        if not self._run_lock.is_set() and 0 <= event.cursor_row < len(self.found):
            area = self.found[event.cursor_row]
            key = (area.level, area.code)
            if key in self._selection:
                self._selection.pop(key)
                self._prepared.pop(key, None)
                self._estimate_requests.pop(key, None)
                self.clear_selection(area_preview=True)
                return
            self._selection[key] = area
            self._estimate_generation += 1
            self._estimate_requests[key] = self._estimate_generation
            self.refresh_selection()
            self.query_one("#summary", Static).update(f"Measuring {escape(area.name)}…\n\nFetching boundary and counting tiles.")
            self.estimate_worker(area, self._estimate_generation)

    # --- estimate ----------------------------------------------------------

    @work(thread=True, group="estimate")
    def estimate_worker(self, area: Area, generation: int) -> None:
        self.call_from_thread(self.log_line, f"Measuring {area.label}…")
        try:
            with self._estimate_lock:
                if self._estimate_requests.get((area.level, area.code)) != generation:
                    return
                geom = areas.geometry(area)
                km2 = areas.area_km2(geom)
                tiles = areas.tiles_for(geom)
        except Exception as e:                       # noqa: BLE001
            self.call_from_thread(self.estimate_failed, area, generation, str(e))
            return
        est = pipeline.estimate(len(tiles))
        self.call_from_thread(self.show_estimate, area, tiles, km2, est, generation)

    def estimate_failed(self, area: Area, generation: int, error: str) -> None:
        key = (area.level, area.code)
        if self._estimate_requests.get(key) == generation:
            self.query_one("#summary", Static).update(f"Estimate unavailable for {escape(area.name)}.\nToggle the place off and on to retry.")
            self.log_line(f"Estimate failed: {error}")

    def show_estimate(self, area: Area, tiles: list[tuple[int, int]],
                      km2: float, est: pipeline.Estimate, generation: int) -> None:
        key = (area.level, area.code)
        if self._estimate_requests.get(key) != generation or self._run_lock.is_set():
            return
        if tiles:
            self._prepared[key] = (area, tiles)
        h = pipeline.human
        self.query_one("#summary", Static).update(
            f"[b]{escape(area.name)}[/b] ({area.code}) · {km2:,.0f} km²\n\n".replace(",", " ")
            + f"[b]{len(tiles):,}[/b] tiles ≈ [b]{h(est.raw_bytes)}[/b] raw "
              .replace(",", " ")
            + f"(±8%)\n"
            + f"\n[dim]Colourised  {h(est.colorized_bytes)}\n"
              f"3D Tiles    {h(est.tiles3d_bytes)}\n"
              f"All LiDAR outputs  {h(est.total_bytes)}\n"
              f"Ortho cache and PMTiles are additional.[/dim]\n\n"
            + f"[dim]~{est.download_hours:.1f} h download"
              f" · ~{est.process_hours:.1f} h processing[/dim]"
        )
        self.refresh_selection()
        if not tiles:
            self.query_one("#summary", Static).update("No LiDAR tiles intersect this area.")
        self.log_line(f"{area.name}: {len(tiles)} tiles, ~{h(est.raw_bytes)}")

    # --- run ---------------------------------------------------------------

    @on(Button.Pressed, "#run")
    def action_run(self) -> None:
        options = self.options()
        if self._run_lock.is_set() or not options.stages or self.query_one("#run", Button).disabled:
            return
        try:
            batch = [self._site] if self._site else jobs.prepare_batch([self._prepared[key] for key in self._selection],
                                       self.query_one("#zone-name", Input).value
                                       if self.query_one("#merge", Checkbox).value else None)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        if self._site:                  # run_area converts regardless; show that stage
            options = jobs.replace(options, convert=True)
        self._run_lock.set()
        self._stop.clear()
        self._stages = options.stages
        self._batch_total = len(batch)
        self._batch_index = 0
        self.query_one("#overall-progress", ProgressBar).update(total=100, progress=0)
        self.query_one("#download-progress", ProgressBar).update(total=100, progress=0)
        self.query_one("#download-label", Static).update("Waiting for download" if options.download else "Download not selected")
        self.query_one("#download-detail", Static).update("Bytes received · speed")
        for selector in self.LOCKED_WHILE_RUNNING:
            self.query_one(selector).disabled = True
        self.query_one("#run", Button).disabled = True
        self.query_one("#stop", Button).disabled = False
        self.run_worker_thread(batch, options)

    @on(Button.Pressed, "#stop")
    def action_stop(self) -> None:
        if not self._run_lock.is_set():
            return
        self._stop.set()
        self.query_one("#stop", Button).disabled = True
        self.query_one("#overall-label", Static).update("Stopping after the current operation…")
        self.log_line("Stop requested. Downloads are cancelled now; a running conversion finishes first. "
                      "Complete files are kept.")

    def action_quit(self) -> None:
        if self._run_lock.is_set():
            self.action_stop()
            self.notify("Wait for Stopped, then quit again. Your inputs will be kept.")
        else:
            self.exit()

    def begin_area(self, index: int, area: Area | jobs.NamedZone) -> None:
        self._batch_index = index
        self._batch_name = area.name
        self._stage_fractions = dict.fromkeys(self._stages, 0.0)
        self._download_done = 0
        self._download_total = 0
        self._transfer_bytes = 0
        self._transfer_samples.clear()
        self._transfers = {}
        self._restarts = 0
        self.query_one("#overall-label", Static).update(f"Area {index + 1}/{self._batch_total} · {area.name}")
        self.query_one("#download-progress", ProgressBar).update(total=100, progress=0)

    def refresh_overall(self) -> None:
        fraction = sum(self._stage_fractions.values()) / max(1, len(self._stages))
        percent = 100 * (self._batch_index + fraction) / self._batch_total
        self.query_one("#overall-progress", ProgressBar).update(total=100, progress=percent)

    def update_progress(self, stage: str, done: int, total: int, detail: str) -> None:
        if stage not in self._stages:
            return
        fraction = min(1.0, max(0.0, done / total)) if total else 0.0
        self._stage_fractions[stage] = max(self._stage_fractions.get(stage, 0.0), fraction)
        if stage == "download":
            self._download_done, self._download_total = done, total
            if " retry: " in detail:
                self._restarts += 1
            elif done:                              # details start with the finished tile
                self._transfers.pop(detail.split(" ", 1)[0], None)
            self.refresh_download()
            if " retry: " in detail:                # the stage line keeps the last finished tile
                return
        if not self._stop.is_set():
            self.query_one("#overall-label", Static).update(
                f"Area {self._batch_index + 1}/{self._batch_total} · {self._batch_name} · {stage}")
        self.query_one("#stage-label", Static).update(f"{stage.capitalize()} {done}/{total} · {detail}")
        self.refresh_overall()

    def update_transfer(self, tile: str, received: int, total: int | None) -> None:
        previous = self._transfers.get(tile)
        if previous:
            self._transfer_bytes += max(0, received - previous[0])
        self._transfers[tile] = (received, total)
        self.refresh_download()

    def refresh_download(self) -> None:
        """Render the download pane from every transfer in flight.

        The bar is the download stage itself (finished tiles plus the fractions
        in flight), so it only moves forward. Speed is the last 5 seconds.
        """
        now = time.monotonic()
        samples = self._transfer_samples
        samples.append((now, self._transfer_bytes))
        while len(samples) > 2 and now - samples[0][0] > 5:
            samples.popleft()
        span = now - samples[0][0]
        speed = (self._transfer_bytes - samples[0][1]) / span if span > 0 else 0
        active = [(r, t) for r, t in self._transfers.values() if not t or r < t]
        if self._download_total:
            in_flight = sum(min(r / t, 1) for r, t in self._transfers.values() if t)
            fraction = min((self._download_done + in_flight) / self._download_total, 1)
            self._stage_fractions["download"] = max(self._stage_fractions.get("download", 0), fraction)
            self.refresh_overall()
        self.query_one("#download-progress", ProgressBar).update(
            total=100, progress=100 * self._stage_fractions.get("download", 0))
        transfers = f"{len(active)} transfer{'' if len(active) == 1 else 's'}"
        self.query_one("#download-label", Static).update(
            f"{transfers} · {self._download_done}/{self._download_total} tiles")
        h = pipeline.human
        restarts = f" · {self._restarts} slow or failed connection(s) restarted" if self._restarts else ""
        self.query_one("#download-detail", Static).update(
            f"{h(self._transfer_bytes)} received · {h(speed)}/s{restarts}")

    @work(thread=True, exclusive=True, group="pipeline")
    def run_worker_thread(self, batch: list[tuple[Area | jobs.NamedZone, list[tuple[int, int]]]],
                          options: jobs.Options) -> None:
        log = lambda s: self.call_from_thread(self.log_line, s)    # noqa: E731

        def progress(stage: str, done: int, total: int, detail: str) -> None:
            self.call_from_thread(self.update_progress, stage, done, total, detail)
            if " retry: " not in detail and (done % 10 == 0 or done == total or "FAIL" in detail):
                log(f"[{stage} {done}/{total}] {detail}")

        def transfer(tile: str, received: int, total: int | None) -> None:
            self.call_from_thread(self.update_transfer, tile, received, total)

        failures = 0
        try:
            for index, (area, tiles) in enumerate(batch):
                if self._stop.is_set():
                    break
                self.call_from_thread(self.begin_area, index, area)
                log(f"Starting {area.label}")
                try:
                    results = jobs.run_area(area, tiles, data_root(), self.catalog, options,
                                            progress, self._stop.is_set, transfer)
                    failures += sum(len(result.failed) for result in results.values())
                except InterruptedError:
                    self._stop.set()
                except Exception as e:               # noqa: BLE001
                    failures += 1
                    log(f"FAILED {area.name}: {e}")
        finally:
            self.call_from_thread(self._finish, failures)

    def _finish(self, failures: int = 0) -> None:
        self._run_lock.clear()
        for selector in self.LOCKED_WHILE_RUNNING:
            self.query_one(selector).disabled = False
        self.refresh_selection()
        self.query_one("#stop", Button).disabled = True
        if self._transfers:                         # cancelled transfers never report an end
            self._transfers = {}
            self.refresh_download()
        status = "Stopped · inputs kept" if self._stop.is_set() else (
            f"Finished with {failures} error(s) · see activity" if failures else "Complete · all selected areas")
        self.query_one("#overall-label", Static).update(status)
        if not failures and not self._stop.is_set():
            self.query_one("#overall-progress", ProgressBar).update(total=100, progress=100)
        self.log_line(status)


# --- command line ----------------------------------------------------------

def cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", choices=list(areas.LEVELS),
                    help="administrative level")
    ap.add_argument("--name", action="append", help="area name (exact or prefix); repeat for a batch")
    ap.add_argument("--merge-name", help="merge the selection into one named zone instead of separate outputs")
    ap.add_argument("--file", help="enriched .las/.laz to convert, alone or set into LiDAR HD (implies --tiles)")
    ap.add_argument("--buffer", type=float,
                    help="with --file: metres of LiDAR HD around the file (omit for the file alone)")
    ap.add_argument("--srs", type=int, help="with --file: EPSG code when the file declares none")
    ap.add_argument("--z-align", action="store_true",
                    help="with --file: shift the file by its measured altitude difference with LiDAR HD")
    ap.add_argument("--keep-overlap", action="store_true",
                    help="with --file: keep the LiDAR HD points that share a 1 m cube with the file")
    ap.add_argument("--estimate-only", action="store_true",
                    help="print the estimate and exit")
    ap.add_argument("--color", action="store_true", help="colourise after download")
    ap.add_argument("--ortho", action="store_true", help="export 20 cm orthophoto as raster PMTiles")
    ap.add_argument("--no-download", action="store_true", help="reuse local inputs instead of downloading LiDAR")
    ap.add_argument("--tiles", action="store_true", help="convert to 3D Tiles")
    ap.add_argument("--upload", action="store_true", help="upload to MinIO")
    ap.add_argument("--snowball", action="store_true", help="send the complete tileset in one server-extracted TAR, including large files (MinIO only)")
    ap.add_argument("--no-clean", action="store_true",
                    help="keep raw/ and colorized/ after a successful run")
    args = ap.parse_args(argv)
    if args.snowball and not args.upload:
        ap.error("--snowball requires --upload")
    if args.merge_name is not None:
        try:
            jobs.zone_code(args.merge_name)
        except ValueError as e:
            ap.error(str(e))
        if not args.level or not args.name:
            ap.error("--merge-name requires --level and --name")

    if not args.file and (not args.level or not args.name):
        LidarApp().run()
        return 0

    catalog = Catalog.load(data_root() / "catalog.json")
    options = jobs.Options(download=not args.no_download, colorize=args.color,
                           ortho=args.ortho, convert=args.tiles or bool(args.file), upload=args.upload,
                           cleanup=not args.no_clean, snowball=args.snowball,
                           z_align=args.z_align, keep_overlap=args.keep_overlap)

    def progress(stage: str, done: int, total: int, detail: str) -> None:
        print(f"[{stage} {done}/{total}] {detail}", flush=True)

    failed = False
    seen = set()
    batch = []
    if args.file:
        try:
            batch.append(jobs.plan_site(args.file, args.buffer, args.srs))
        except Exception as e:                       # noqa: BLE001
            print(f"FAILED {args.file}: {e}")
            return 1
        found, tiles = batch[0]
        lon, lat = found.centre
        print(f"{found.label}  centre {lat:.5f}, {lon:.5f}  {len(tiles)} LiDAR HD tiles → site-{found.code}/")
    for name in () if args.file else args.name:
        try:
            found = areas.search(args.level, name)
            if not found:
                print(f"No {args.level} matching '{name}'")
                failed = True
                continue
            area = found[0]
            if area.code in seen:
                continue
            seen.add(area.code)
            geom = areas.geometry(area)
            tiles = areas.tiles_for(geom)
            est = pipeline.estimate(len(tiles))
            h = pipeline.human
            print(f"{area.label}  {areas.area_km2(geom):,.0f} km²".replace(",", " "))
            print(f"  {len(tiles):,} tiles ≈ {h(est.raw_bytes)} raw (±8%)".replace(",", " "))
            print(f"  all LiDAR outputs: {h(est.total_bytes)}; ortho cache/PMTiles additional")
            print(f"  ~{est.download_hours:.1f} h download, ~{est.process_hours:.1f} h processing")
            if not tiles:
                raise ValueError("No tiles intersect this area")
            batch.append((area, tiles))
        except KeyboardInterrupt:
            print("Stopped. Inputs kept.")
            return 130
        except Exception as e:                       # noqa: BLE001
            print(f"FAILED {name}: {e}")
            failed = True
    if args.merge_name is not None and failed:
        print("Merge cancelled: every selected area must resolve successfully.")
        return 1
    for area, tiles in jobs.prepare_batch(batch, args.merge_name):
        if isinstance(area, jobs.NamedZone):
            print(f"{area.label}: {len(tiles)} unique tiles, "
                  f"≈ {pipeline.human(pipeline.estimate(len(tiles)).raw_bytes)} raw → zone-{area.code}/")
        if args.estimate_only:
            continue
        try:
            results = jobs.run_area(area, tiles, data_root(), catalog, options, progress)
            failed |= any(result.failed for result in results.values())
        except KeyboardInterrupt:
            print("Stopped. Inputs kept.")
            return 130
        except Exception as e:                       # noqa: BLE001
            print(f"FAILED {area.name}: {e}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(cli())
