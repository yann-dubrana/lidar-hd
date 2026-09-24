"""Run one py3dtiles conversion without passing its inputs through the OS command line."""
from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path


def run_manifest(path: str) -> None:
    arguments = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(arguments, list) or not arguments or arguments[0] != "convert"
            or not all(isinstance(arg, str) for arg in arguments)):
        raise ValueError("Invalid conversion argument manifest")

    from py3dtiles.command_line import main

    original = sys.argv
    try:
        # argparse reads this in memory; no subprocess receives the expanded list.
        sys.argv = ["py3dtiles", *arguments]
        main()
    finally:
        sys.argv = original


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run_manifest(sys.argv[1])