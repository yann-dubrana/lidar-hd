import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Button, Checkbox, DataTable, Input, ProgressBar, RadioButton, Static

from lidar_hd.areas import Area
from lidar_hd import pipeline
from main import LidarApp, cli


REGIONS = [Area("region", "Bretagne", "53"), Area("region", "Nouvelle-Aquitaine", "75")]
GEOMETRY = {"type": "Polygon", "coordinates": [[[400000, 6400000], [401000, 6400000],
                                               [401000, 6401000], [400000, 6401000],
                                               [400000, 6400000]]]}


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch("main.Catalog.load"),
                        patch("main.areas.browse", return_value=REGIONS),
                        patch("main.areas.geometry", return_value=GEOMETRY),
                        patch("main.minio_config")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def ready(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def test_text_selection_uses_framework_method(self):
        app = LidarApp()
        app.selected = REGIONS[0]
        with patch.object(App, "clear_selection") as clear_text, \
                patch.object(app, "query_one") as query:
            app.clear_selection()
        clear_text.assert_called_once_with()
        query.assert_not_called()
        self.assertEqual(app.selected, REGIONS[0])

    async def test_input_cursor_preserves_area_preview_and_queue(self):
        app = LidarApp()
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            term = app.query_one("#term", Input)
            term.value = "Bret"
            await pilot.pause(0.5)
            await self.ready(app, pilot)
            app.query_one("#results", DataTable).focus()
            await pilot.press("space")
            await self.ready(app, pilot)
            self.assertEqual(app.selected, REGIONS[0])
            selected, tiles, estimate = app.selected, app.tiles, app.est
            summary = str(app.query_one("#summary", Static).content)
            prepared = app._prepared.copy()
            term.focus()
            await pilot.press("home", "shift+right", "end")
            self.assertEqual(term.cursor_position, len(term.value))
            self.assertEqual(app.selected, selected)
            self.assertEqual(app.tiles, tiles)
            self.assertIs(app.est, estimate)
            self.assertEqual(str(app.query_one("#summary", Static).content), summary)
            self.assertEqual(app._prepared, prepared)
            self.assertEqual(list(app._selection), [("region", "53")])
            self.assertFalse(app.query_one("#run", Button).disabled)

    async def test_browse_multiselect_filter_and_deselect(self):
        app = LidarApp()
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one("#results", DataTable)
            self.assertEqual(table.row_count, 2)
            table.focus()
            await pilot.press("space", "down", "enter")
            await self.ready(app, pilot)
            self.assertEqual(len(app._prepared), 2)
            self.assertFalse(app.query_one("#run", Button).disabled)
            app.query_one("#term", Input).value = "Bret"
            await pilot.pause(0.5)
            self.assertEqual(table.row_count, 1)
            self.assertEqual(len(app._selection), 2)
            table.focus()
            await pilot.press("space")
            self.assertEqual(len(app._selection), 1)
            self.assertIn(("region", "75"), app._selection)
            app.clear_queue()
            self.assertFalse(app._selection)
            self.assertTrue(app.query_one("#run", Button).disabled)

    async def test_stale_results_and_estimates_are_ignored(self):
        app = LidarApp()
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            app.show_results([], app._search_generation - 1)
            self.assertEqual(len(app.found), 2)
            app.show_estimate(REGIONS[0], [(400, 6401)], 1, pipeline.estimate(1), -1)
            self.assertFalse(app._prepared)
            app.search_failed(app._search_generation, "offline")
            self.assertIn("retry", str(app.query_one("#results-status", Static).content))

    async def test_progress_stop_and_independent_options(self):
        app = LidarApp()
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            for checkbox in app.query(Checkbox):
                checkbox.value = False
            app.query_one("#do_ortho", Checkbox).value = True
            self.assertEqual(app.options().stages, ["ortho"])
            app._stages = ["download", "ortho"]
            app._batch_total = 2
            app.begin_area(0, REGIONS[0])
            app.update_progress("download", 0, 2, "Resolving")
            app.update_transfer("tile.laz", 50, 100)
            self.assertEqual(app.query_one("#download-progress", ProgressBar).percentage, 0.5)
            self.assertAlmostEqual(app.query_one("#overall-progress", ProgressBar).progress, 6.25)
            app.update_transfer("tile.laz", 0, 100)
            self.assertEqual(app.query_one("#download-progress", ProgressBar).progress, 0)
            app.update_transfer("unknown.laz", 50, None)
            self.assertIsNone(app.query_one("#download-progress", ProgressBar).total)
            app._run_lock.set()
            app.action_stop()
            app._finish()
            self.assertIn("Stopped", str(app.query_one("#overall-label", Static).content))
            self.assertLess(app.query_one("#overall-progress", ProgressBar).progress, 100)

    async def test_batch_runs_sequentially_and_recovers_from_failure(self):
        app = LidarApp()
        calls = []

        def run(area, tiles, base, catalog, options, progress, stop, transfer):
            calls.append(area.code)
            if len(calls) == 1:
                raise RuntimeError("example failure")
            progress("download", 1, 1, "complete")
            return {"download": pipeline.StageResult(ok=1)}

        with patch("main.jobs.run_area", side_effect=run):
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                app.query_one("#results", DataTable).focus()
                await pilot.press("space", "down", "space")
                await self.ready(app, pilot)
                app.action_run()
                app.action_run()
                await self.ready(app, pilot)
                self.assertEqual(calls, ["53", "75"])
                self.assertFalse(app._run_lock.is_set())
                self.assertFalse(app.query_one("#run", Button).disabled)
                self.assertIn("1 error", str(app.query_one("#overall-label", Static).content))

    async def test_narrow_layout_and_empty_search(self):
        app = LidarApp()
        async with app.run_test(size=(80, 30)) as pilot:
            await self.ready(app, pilot)
            self.assertTrue(app.screen.has_class("narrow"))
            app.query_one("#term", Input).value = "no-such-place"
            await pilot.pause(0.5)
            self.assertEqual(app.query_one("#results", DataTable).row_count, 0)
            self.assertIn("No places", str(app.query_one("#results-status", Static).content))

    async def test_named_merge_validation_and_single_job(self):
        app = LidarApp()
        with patch("main.jobs.run_area", return_value={}) as run:
            async with app.run_test(size=(140, 44)) as pilot:
                await self.ready(app, pilot)
                app.query_one("#results", DataTable).focus()
                await pilot.press("space", "down", "space")
                await self.ready(app, pilot)
                app.query_one("#merge", Checkbox).value = True
                await pilot.pause()
                self.assertTrue(app.query_one("#run", Button).disabled)
                name = app.query_one("#zone-name", Input)
                self.assertFalse(name.disabled)
                name.value = "La CUB"
                await pilot.pause()
                self.assertFalse(app.query_one("#run", Button).disabled)
                self.assertIn("1 unique tiles", str(app.query_one("#output-note", Static).content))
                self.assertEqual(len(app._selection), 2)
                app.action_run()
                await self.ready(app, pilot)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0].code, "la-cub")
                self.assertEqual(len(run.call_args.args[1]), 1)
                self.assertEqual(app._batch_total, 1)
                self.assertFalse(app.query_one("#batch-options").disabled)
                app.query_one("#merge", Checkbox).value = False
                await pilot.pause()
                self.assertTrue(name.disabled)
                self.assertIn("Separate", str(app.query_one("#output-note", Static).content))

    async def test_epci_browsing_keeps_commune_selection(self):
        app = LidarApp()
        epci = Area("epci", "Bordeaux Métropole", "243300316")
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            app.query_one("#results", DataTable).focus()
            await pilot.press("space")
            await self.ready(app, pilot)
            with patch("main.areas.browse", return_value=[epci]) as browse:
                app.query_one("#epci", RadioButton).value = True
                await self.ready(app, pilot)
                browse.assert_called_with("epci")
                app.query_one("#term", Input).value = "Bordeaux"
                await pilot.pause(0.5)
                app.query_one("#results", DataTable).focus()
                await pilot.press("space")
                await self.ready(app, pilot)
            self.assertIn(("epci", "243300316"), app._prepared)
            self.assertIn(("region", "53"), app._prepared)
            self.assertEqual(len(app._selection), 2)

    async def test_actions_and_progress_stay_visible(self):
        app = LidarApp()
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            for selector in ("#run", "#stop", "#overall-progress", "#download-progress"):
                widget = app.query_one(selector)
                self.assertGreater(widget.region.width, 0)
                self.assertLessEqual(widget.region.bottom, 43)
            app.query_one("#details-scroll").scroll_end(animate=False)
            await pilot.pause()
            self.assertLessEqual(app.query_one("#run").region.bottom, 43)


class CliTests(unittest.TestCase):
    @patch("main.Catalog.load")
    @patch("main.areas.geometry", return_value=GEOMETRY)
    @patch("main.areas.search", side_effect=[[REGIONS[0]], [REGIONS[1]]])
    @patch("main.jobs.run_area", return_value={})
    def test_named_merge(self, run, search, geometry, catalog):
        result = cli(["--level", "region", "--name", "Bretagne", "--name", "Nouvelle",
                      "--merge-name", "La CUB", "--no-download", "--ortho", "--no-clean",
                      "--upload", "--snowball"])
        self.assertEqual(result, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0].code, "la-cub")
        self.assertEqual(len(run.call_args.args[1]), 1)
        self.assertTrue(run.call_args.args[4].snowball)

    @patch("main.Catalog.load")
    @patch("main.areas.geometry", return_value=GEOMETRY)
    @patch("main.areas.search", side_effect=[[REGIONS[0]], []])
    @patch("main.jobs.run_area")
    def test_incomplete_merge_does_not_run_partial_selection(self, run, search, geometry, catalog):
        result = cli(["--level", "region", "--name", "Bretagne", "--name", "Missing",
                      "--merge-name", "La CUB"])
        self.assertEqual(result, 1)
        run.assert_not_called()

    @patch("main.Catalog.load")
    @patch("main.areas.geometry", return_value=GEOMETRY)
    @patch("main.areas.search", return_value=[REGIONS[0]])
    @patch("main.jobs.run_area")
    def test_merged_estimate_does_not_run(self, run, search, geometry, catalog):
        self.assertEqual(cli(["--level", "region", "--name", "Bretagne",
                              "--merge-name", "La CUB", "--estimate-only"]), 0)
        run.assert_not_called()
    @patch("main.Catalog.load")
    @patch("main.areas.geometry", return_value=GEOMETRY)
    @patch("main.areas.search", side_effect=[[REGIONS[0]], [REGIONS[1]]])
    @patch("main.jobs.run_area", return_value={})
    def test_batch_and_independent_flags(self, run, search, geometry, catalog):
        result = cli(["--level", "region", "--name", "Bretagne", "--name", "Nouvelle",
                      "--no-download", "--ortho", "--no-clean"])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[4].stages, ["ortho"])


if __name__ == "__main__":
    unittest.main()