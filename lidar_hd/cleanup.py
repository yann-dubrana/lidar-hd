"""Best-effort removal of empty point directories, without deleting files."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def _safe_directory(path: Path) -> bool:
    for ancestor in reversed((path, *path.parents)):
        try:
            metadata = ancestor.lstat()
        except OSError:
            return False
        if not stat.S_ISDIR(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return False
    return True


def prune_empty_points(tileset: Path) -> int:
    """Return the number of removed directories, preserving the points root.

    Links and Windows reparse points (including junctions) are never entered.
    Access errors and concurrent filesystem changes are ignored; only successful
    rmdir calls count. Ancestors are rechecked before scanning or removing a
    directory, rather than trusting metadata cached during traversal.
    """
    try:
        points = tileset.absolute() / "points"
    except OSError:
        return 0
    removed = 0
    pending = [(points, False)]
    while pending:
        directory, visited = pending.pop()
        if not _safe_directory(directory):
            continue
        if visited:
            if directory != points:
                try:
                    directory.rmdir()
                except OSError:
                    continue
                removed += 1
            continue
        try:
            with os.scandir(directory) as entries:
                children = [
                    directory / entry.name for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                ]
        except OSError:
            continue
        pending.append((directory, True))
        pending.extend((child, False) for child in children)
    return removed