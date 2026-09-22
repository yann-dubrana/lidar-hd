"""PyInstaller entry point; dispatch workers before importing the TUI."""
import multiprocessing
import sys


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-py3dtiles":
        del sys.argv[1]
        from py3dtiles.command_line import main

        main()
    elif sys.argv[1:] == ["--self-test"]:
        from lidar_hd.packaging_check import run

        run()
    else:
        from main import cli

        raise SystemExit(cli())