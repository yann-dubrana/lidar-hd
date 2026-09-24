import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lidar_hd import pipeline


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_large_selection_uses_short_command_and_complete_manifest(self):
        inputs = [self.root / "zone CUB été" / "colorized" /
                  f"LHD_FXX_{i:04d}_6433_PTS_LAMB93_IGN69.copc.laz" for i in range(677)]
        self.assertGreater(len(subprocess.list2cmdline([str(p) for p in inputs])), 32767)
        for frozen in (False, True):
            with self.subTest(frozen=frozen):
                manifests = []

                def launch(cmd, **kwargs):
                    self.assertLess(len(subprocess.list2cmdline(cmd)), 4096)
                    manifest = Path(cmd[-1])
                    manifests.append(manifest)
                    args = json.loads(manifest.read_text(encoding="utf-8"))
                    self.assertEqual(args, ["convert", *map(str, inputs),
                                           "--out", str(self.root / "output"),
                                           "--srs_in", "2154", "--srs_out", "4978",
                                           "--extra-fields", "classification", "--jobs", "2"])
                    return Mock(returncode=0)

                with patch.object(sys, "frozen", frozen, create=True), \
                        patch.object(pipeline.subprocess, "run", side_effect=launch) as run:
                    result = pipeline.convert_3dtiles(iter(inputs), self.root / "output", jobs=2)
                self.assertEqual(result.ok, 677)
                run.assert_called_once()
                self.assertFalse(manifests[0].parent.exists())

    def test_failure_cleans_manifest_and_preserves_input(self):
        source = self.root / "input.laz"
        source.write_bytes(b"keep")
        for failure in (Mock(returncode=1, stderr="details\nconversion failed"), OSError("launch failed")):
            manifests = []

            def launch(cmd, **kwargs):
                manifests.append(Path(cmd[-1]))
                args = json.loads(manifests[-1].read_text(encoding="utf-8"))
                self.assertNotIn("--extra-fields", args)
                self.assertNotIn("--jobs", args)
                if isinstance(failure, Exception):
                    raise failure
                return failure

            with patch.object(pipeline.subprocess, "run", side_effect=launch):
                if isinstance(failure, Exception):
                    with self.assertRaisesRegex(OSError, "launch failed"):
                        pipeline.convert_3dtiles([source], self.root / "out", keep_classification=False)
                else:
                    result = pipeline.convert_3dtiles([source], self.root / "out", keep_classification=False)
                    self.assertEqual(result.failed, ["conversion failed"])
            self.assertFalse(manifests[0].parent.exists())
            self.assertEqual(source.read_bytes(), b"keep")

    def test_empty_selection_does_not_launch(self):
        with patch.object(pipeline.subprocess, "run") as run:
            result = pipeline.convert_3dtiles([], self.root / "out")
        run.assert_not_called()
        self.assertEqual(result.ok, 0)

    def test_frozen_entry_reads_manifest_before_calling_py3dtiles(self):
        args = ["convert", str(self.root / "été space.laz"), "--out", str(self.root / "out")]
        manifest = self.root / "inputs.json"
        manifest.write_text(json.dumps(args), encoding="utf-8")
        received = []
        with patch.object(sys, "argv", ["lidar-hd.exe", "--internal-py3dtiles", str(manifest)]), \
                patch("multiprocessing.freeze_support"), \
                patch("py3dtiles.command_line.main", side_effect=lambda: received.append(sys.argv[1:])):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "frozen_entry.py"),
                           run_name="__main__")
        self.assertEqual(received, [args])

    def test_real_worker_converts_multiple_inputs_to_one_tileset(self):
        import laspy
        import numpy as np

        inputs = []
        for index in range(2):
            header = laspy.LasHeader(point_format=3, version="1.2")
            header.offsets = [420000, 6439000, 0]
            header.scales = [0.01, 0.01, 0.01]
            points = laspy.LasData(header)
            points.x = 420000 + index * 100 + np.arange(32, dtype=float)
            points.y = 6439000 + np.arange(32, dtype=float)
            points.z = np.full(32, 50.0)
            points.red = np.full(32, 65535, dtype=np.uint16)
            points.classification = np.full(32, 2, dtype=np.uint8)
            source = self.root / f"échantillon {index}.laz"
            points.write(source)
            inputs.append(source)
        output = self.root / "nested" / "3dtiles"
        result = pipeline.convert_3dtiles(inputs, output, jobs=2)
        self.assertFalse(result.failed, result.failed)
        self.assertEqual(result.ok, 2)
        self.assertEqual(len(list(output.rglob("tileset.json"))), 1)
        self.assertTrue(json.loads((output / "tileset.json").read_text())["root"]["boundingVolume"])
        self.assertTrue(list(output.rglob("*.pnts")))
        self.assertTrue(all(source.is_file() for source in inputs))
        self.assertFalse(list(output.parent.glob(".conversion-*")))


if __name__ == "__main__":
    unittest.main()