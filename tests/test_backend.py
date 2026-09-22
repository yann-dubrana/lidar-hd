import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from lidar_hd import areas, http, pipeline


ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {} if length is None else {"Content-Length": str(length)}


class BrowseTests(unittest.TestCase):
    @patch("lidar_hd.areas.get_json")
    def test_browse_all_levels_and_pagination(self, get_json):
        for level in ("region", "departement"):
            with self.subTest(level=level):
                get_json.reset_mock()
                get_json.side_effect = [
                    {"numberMatched": 3, "features": [
                        {"properties": {"nom_officiel": "Zulu", "code_insee": "3"}},
                        {"properties": {"nom_officiel": "alpha", "code_insee": "1"}},
                    ]},
                    {"numberMatched": 3, "features": [
                        {"properties": {"nom_officiel": "Beta", "code_insee": "2"}},
                    ]},
                ]
                result = areas.browse(level)
                self.assertEqual([a.name for a in result], ["alpha", "Beta", "Zulu"])
                self.assertTrue(all(a.level == level for a in result))
                queries = [parse_qs(urlparse(c.args[0]).query) for c in get_json.call_args_list]
                self.assertEqual(queries[0]["PROPERTYNAME"], ["nom_officiel,code_insee"])
                self.assertNotIn("CQL_FILTER", queries[0])
                self.assertEqual(queries[1]["STARTINDEX"], ["2"])

    @patch("lidar_hd.areas.get_json")
    def test_no_bulk_communes(self, get_json):
        self.assertEqual(areas.browse("commune"), [])
        get_json.assert_not_called()

    @patch("lidar_hd.areas.get_json", return_value={"features": []})
    def test_empty_browse(self, get_json):
        self.assertEqual(areas.browse("region"), [])

    @patch("lidar_hd.areas.get_json", return_value={"features": []})
    def test_search_unchanged(self, get_json):
        self.assertEqual(areas.search("commune", "  L'Isle  ", 7), [])
        query = parse_qs(urlparse(get_json.call_args.args[0]).query)
        self.assertEqual(query["CQL_FILTER"], ["nom_officiel ILIKE 'L''Isle%'"])
        self.assertEqual(query["COUNT"], ["7"])
        self.assertIn("population", query["PROPERTYNAME"][0])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / "tile.laz"

    @patch("lidar_hd.http.time.monotonic", return_value=0)
    @patch("lidar_hd.http.urllib.request.urlopen")
    def test_cumulative_throttled_and_chunks(self, urlopen, clock):
        urlopen.return_value = Response(b"abcdef", 6)
        events, chunks = [], []
        count = http.download("https://example.test/tile", self.dest, 6, 1, 2,
                              chunks.append, on_progress=lambda n, t: events.append((n, t)))
        self.assertEqual(count, 6)
        self.assertEqual(self.dest.read_bytes(), b"abcdef")
        self.assertEqual(events, [(0, 6), (6, 6)])
        self.assertEqual(chunks, [2, 2, 2])
        self.assertFalse(self.dest.with_suffix(".laz.part").exists())

    @patch("lidar_hd.http.time.monotonic", side_effect=[0, .11, .22, .33])
    @patch("lidar_hd.http.urllib.request.urlopen")
    def test_unknown_size_and_intermediate_updates(self, urlopen, clock):
        urlopen.return_value = Response(b"abcdef")
        events = []
        http.download("https://example.test/tile", self.dest, chunk=2,
                      on_progress=lambda n, t: events.append((n, t)))
        self.assertEqual(events[0], (0, None))
        self.assertEqual(events[-1], (6, None))
        self.assertIn((2, None), events)
        self.assertIn((4, None), events)

    @patch("lidar_hd.http.urllib.request.urlopen")
    def test_header_size(self, urlopen):
        urlopen.return_value = Response(b"abc", 3)
        events = []
        http.download("https://example.test/tile", self.dest,
                      on_progress=lambda n, t: events.append((n, t)))
        self.assertEqual(events[0][0], 0)
        self.assertEqual(events[-1], (3, 3))

    @patch("lidar_hd.http.time.sleep")
    @patch("lidar_hd.http.time.monotonic", return_value=0)
    @patch("lidar_hd.http.urllib.request.urlopen")
    def test_retry_resets_and_failed_attempt_ends(self, urlopen, clock, sleep):
        urlopen.side_effect = [Response(b"ab", 2), Response(b"abcd", 4)]
        events = []
        http.download("https://example.test/tile", self.dest, expected=4, retries=2,
                      on_progress=lambda n, t: events.append((n, t)))
        self.assertEqual(events, [(0, 4), (2, 4), (0, 4), (4, 4)])
        self.assertEqual(self.dest.read_bytes(), b"abcd")

    @patch("lidar_hd.http.time.sleep")
    @patch("lidar_hd.http.urllib.request.urlopen", side_effect=OSError("offline"))
    def test_connection_failures_report_zero_and_leave_no_file(self, urlopen, sleep):
        events = []
        with self.assertRaises(http.HttpError):
            http.download("https://example.test/tile", self.dest, retries=2,
                          on_progress=lambda n, t: events.append((n, t)))
        self.assertGreaterEqual(events.count((0, None)), 2)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix(".laz.part").exists())


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name)

    @patch("lidar_hd.pipeline.download")
    def test_forwarding_skips_errors_and_initial_stage(self, download):
        (self.dest / "present").write_bytes(b"abc")
        (self.dest / "unknown").write_bytes(b"abcd")
        stage, transfer = [], []
        catalog = Mock()

        def resolve(tile):
            self.assertTrue(stage)
            self.assertEqual(stage[0][:3], ("download", 0, 5))
            if tile == "missing":
                return None
            return {"block": "block", "bytes": None if tile == "unknown" else 3}

        catalog.resolve.side_effect = resolve
        catalog.url.side_effect = lambda tile, block: tile

        def fetch(url, dest, expected=None, *, on_progress=None):
            on_progress(0, expected)
            if url == "broken":
                raise http.HttpError("offline")
            on_progress(3, expected)
            return 3

        download.side_effect = fetch
        result = pipeline.download_tiles(
            ["present", "unknown", "new", "missing", "broken"], self.dest, catalog,
            lambda *args: stage.append(args), lambda: False,
            transfer_progress=lambda *args: transfer.append(args))
        self.assertEqual((result.ok, result.skipped, result.bytes_moved), (1, 2, 3))
        self.assertEqual(result.failed, ["missing", "broken"])
        self.assertIn(("present", 3, 3), transfer)
        self.assertIn(("unknown", 4, 4), transfer)
        self.assertIn(("new", 0, 3), transfer)
        self.assertIn(("new", 3, 3), transfer)
        self.assertEqual([c.args[0] for c in download.call_args_list], ["new", "broken"])

    def test_stop_and_empty(self):
        for tiles in ([], ["tile"]):
            catalog = Mock()
            result = pipeline.download_tiles(tiles, self.dest, catalog, should_stop=lambda: True)
            self.assertEqual(result.ok, 0)
            catalog.resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()