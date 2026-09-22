"""Shared, independently selectable stages for the TUI and command line."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import areas, pipeline
from .areas import Area
from .catalog import Catalog
from .config import minio_config


@dataclass(frozen=True)
class Options:
    download: bool = True
    colorize: bool = False
    ortho: bool = False
    convert: bool = False
    upload: bool = False
    cleanup: bool = True
    snowball: bool = False

    @property
    def stages(self) -> list[str]:
        return [name for name in ("download", "colorize", "ortho", "convert", "upload", "cleanup")
                if getattr(self, name)]


@dataclass(frozen=True)
class NamedZone:
    name: str
    code: str
    members: tuple[Area, ...]
    level: str = "zone"

    @property
    def label(self) -> str:
        return f"{self.name} ({len(self.members)} areas merged)"


def zone_code(name: str) -> str:
    """Make a portable directory/key component, never accept a path."""
    if any(c in name for c in '/\\:'):
        raise ValueError("Zone name must be a name, not a path.")
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    code = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not code or len(code) > 80:
        raise ValueError("Enter a zone name with letters or numbers (maximum 80 normalized characters).")
    return code


def prepare_batch(batch: list[tuple[Area, list[tuple[int, int]]]],
                  merge_name: str | None = None) -> list[tuple[Area | NamedZone, list[tuple[int, int]]]]:
    """Keep separate outputs by default, or union tile coverage before processing."""
    if merge_name is None:
        return list(batch)
    code = zone_code(merge_name)
    if not batch or any(not tiles for _, tiles in batch):
        raise ValueError("Every selected area must have tiles before merging.")
    members = tuple(sorted({(a.level, a.code): a for a, _ in batch}.values(),
                           key=lambda a: (a.level, a.code)))
    tiles = sorted({tile for _, coverage in batch for tile in coverage})
    return [(NamedZone(merge_name.strip(), code, members), tiles)]


def _bind_zone(root: Path, area: NamedZone, tiles: list[tuple[int, int]]) -> None:
    """Refuse stale outputs when a named zone is reused for another selection."""
    manifest = {"name": area.name, "members": [[a.level, a.code] for a in area.members],
                "tiles": [list(tile) for tile in sorted(set(tiles))]}
    path = root / "zone.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("This zone name already belongs to a different selection. Choose another name.")
        return
    if root.exists() and any(root.iterdir()):
        raise ValueError("This zone directory has no selection manifest. Choose another name.")
    root.mkdir(parents=True, exist_ok=True)
    partial = root / "zone.json.part"
    partial.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    partial.replace(path)


def run_area(area: Area | NamedZone, tiles: list[tuple[int, int]], base: Path, catalog: Catalog,
             options: Options, progress: pipeline.Progress = pipeline._noop,
             should_stop: Callable[[], bool] = lambda: False,
             transfer_progress=None) -> dict[str, pipeline.StageResult]:
    """Run selected stages sequentially, reusing on-disk inputs when unchecked."""
    root = base / f"{area.level}-{area.code}"
    if isinstance(area, NamedZone) and options.stages and not should_stop():
        _bind_zone(root, area, tiles)
    raw, col, tiles3d = root / "raw", root / "colorized", root / "3dtiles"
    names = areas.tile_names(tiles)
    results = {}
    source = col if not options.colorize and not any(raw.glob("*.copc.laz")) else raw

    for stage in options.stages:
        if should_stop():
            break
        progress(stage, 0, 1, f"Starting {stage}")
        if stage == "download":
            progress(stage, 0, len(names), "Resolving delivery blocks…")
            catalog.fetch_blocks()
            xs, ys = zip(*tiles)
            catalog.prioritise((min(xs) * 1000, (min(ys) - 1) * 1000,
                               (max(xs) + 1) * 1000, max(ys) * 1000))
            try:
                result = pipeline.download_tiles(names, raw, catalog, progress, should_stop,
                                                 transfer_progress=transfer_progress)
            finally:
                catalog.save(base / "catalog.json")
            source = raw
        elif stage == "colorize":
            if not any(raw.glob("*.copc.laz")):
                raise ValueError("Colourise needs raw LiDAR files. Enable Download first.")
            result = pipeline.colorize_all(raw, col, progress, should_stop,
                                           ortho_cache=root / "ortho-cache")
            source = col
        elif stage == "ortho":
            from . import ortho

            archive = ortho.export_pmtiles(tiles, root, progress, should_stop)
            result = pipeline.StageResult(ok=1, bytes_moved=archive.stat().st_size)
        elif stage == "convert":
            inputs = sorted(source.glob("*.laz"))
            if not inputs:
                raise ValueError("Convert needs raw or colourised LiDAR files. Enable Download first.")
            result = pipeline.convert_3dtiles(inputs, tiles3d, progress=progress)
        elif stage == "upload":
            cfg = minio_config()
            if not cfg.configured:
                raise ValueError("Upload needs MinIO credentials in .env.")
            target = tiles3d if pipeline.tileset_ok(tiles3d) else source
            targets = [target] if target.exists() and any(target.rglob("*.laz")) else []
            if target == tiles3d:
                targets = [target]
            if (root / "ortho" / "orthophoto.pmtiles").is_file():
                targets.append(root / "ortho")
            if not targets:
                raise ValueError("No completed outputs to upload. Run an export or download first.")
            result = pipeline.StageResult()
            for index, folder in enumerate(targets):
                if should_stop():
                    break

                def upload_progress(_stage, done, total, detail):
                    fraction = done / total if total else 0
                    progress("upload", int((index + fraction) * 1000), len(targets) * 1000, detail)

                prefix = "/".join(part for part in (cfg.prefix.strip("/"),
                                                  f"{area.level}-{area.code}", folder.name) if part)
                item = pipeline.upload_dir(folder, prefix, cfg, upload_progress, should_stop,
                                           snowball=options.snowball)
                result.ok += item.ok
                result.skipped += item.skipped
                result.failed.extend(item.failed)
                result.bytes_moved += item.bytes_moved
        else:
            if any(result.failed for result in results.values()):
                progress(stage, 1, 1, "Earlier stage had failures; keeping inputs")
                results[stage] = pipeline.StageResult(skipped=1)
                continue
            freed = pipeline.cleanup(root, expected_tiles=len(names),
                                     did_color=options.colorize or pipeline.colorized_ok(col, len(names)),
                                     did_tiles=options.convert or pipeline.tileset_ok(tiles3d),
                                     progress=progress)
            result = pipeline.StageResult(ok=1, bytes_moved=freed)
        results[stage] = result
        if not should_stop():
            detail = (f"{result.ok} done, {result.skipped} present, "
                      f"{len(result.failed)} failed · {pipeline.human(result.bytes_moved)}")
            progress(stage, 1, 1, detail)
    return results