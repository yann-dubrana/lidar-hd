import copy
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from lidar_hd import areas
from lidar_hd.http import HttpError


EPCI = areas.Area("epci", "Bordeaux Métropole", "243300316")
RECORDS = [
    {"nom": "Bordeaux Métropole", "code": "243300316"},
    {"nom": "CC de l'Île de Ré", "code": "241700459"},
]
RING = [[3, 46.5], [3.02, 46.5], [3.02, 46.52], [3, 46.52], [3, 46.5]]


def feature(geometry):
    return {"type": "Feature", "properties": RECORDS[0], "geometry": geometry}


def square(left, bottom, right, top):
    return [[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]


class EpciSelectionTests(unittest.TestCase):
    def test_registered_level(self):
        self.assertIn("epci", areas.LEVELS)
        self.assertIn("EPCI", areas.LEVELS["epci"])

    @patch("lidar_hd.areas.get_json")
    def test_browse_complete_list_not_search_default_limit(self, get_json):
        get_json.return_value = list(reversed(RECORDS))
        self.assertEqual(areas.browse("epci"), [EPCI, areas.Area("epci", "CC de l'Île de Ré", "241700459")])
        get_json.assert_called_once()
        url = urlparse(get_json.call_args.args[0])
        self.assertEqual((url.netloc, url.path), ("geo.api.gouv.fr", "/epcis"))
        self.assertEqual(parse_qs(url.query), {"fields": ["nom,code"]})

    @patch("lidar_hd.areas.get_json")
    def test_metadata_missing_duplicate_and_invalid_records(self, get_json):
        get_json.return_value = RECORDS + [RECORDS[0], {}, None,
            {"nom": "Missing code"}, {"code": "200000172"},
            {"nom": " ", "code": "200000172"}, {"nom": "Bad code", "code": "123"}]
        self.assertEqual(len(areas.browse("epci")), 2)

    @patch("lidar_hd.areas.get_json", return_value=[])
    def test_empty_results(self, get_json):
        self.assertEqual(areas.browse("epci"), [])
        self.assertEqual(areas.search("epci", "unknown"), [])

    @patch("lidar_hd.areas.get_json")
    def test_name_search_encoding_exact_match_and_limit(self, get_json):
        get_json.return_value = [
            {"nom": "CC de l'Île de Ré Nord", "code": "200000172"}, RECORDS[1], RECORDS[0]]
        result = areas.search("epci", "  CC de l'Île de Ré  ", 1)
        self.assertEqual(result, [areas.Area("epci", "CC de l'Île de Ré", "241700459")])
        query = parse_qs(urlparse(get_json.call_args.args[0]).query)
        self.assertEqual(query["nom"], ["CC de l'Île de Ré"])
        self.assertEqual(query["limit"], ["1"])
        self.assertEqual(query["fields"], ["nom,code"])

    @patch("lidar_hd.areas.get_json", return_value=[RECORDS[0]])
    def test_siren_search(self, get_json):
        self.assertEqual(areas.search("epci", " 243300316 "), [EPCI])
        query = parse_qs(urlparse(get_json.call_args.args[0]).query)
        self.assertEqual(query["code"], [EPCI.code])
        self.assertNotIn("nom", query)

    @patch("lidar_hd.areas.get_json")
    def test_blank_and_nonpositive_limit_do_not_request(self, get_json):
        for term, limit in [(" ", 25), ("Bordeaux", 0), ("Bordeaux", -1)]:
            self.assertEqual(areas.search("epci", term, limit), [])
        get_json.assert_not_called()

    @patch("lidar_hd.areas.get_json", return_value={"error": "unavailable"})
    def test_malformed_response_is_not_empty_success(self, get_json):
        with self.assertRaises(ValueError):
            areas.browse("epci")

    @patch("lidar_hd.areas.get_json", side_effect=HttpError("offline"))
    def test_network_errors_propagate(self, get_json):
        for operation in (lambda: areas.browse("epci"),
                          lambda: areas.search("epci", "Bordeaux"),
                          lambda: areas.geometry(EPCI)):
            with self.subTest(operation=operation), self.assertRaises(HttpError):
                operation()


class EpciGeometryTests(unittest.TestCase):
    @patch("lidar_hd.areas.get_json")
    def test_polygon_reprojection_longitude_first(self, get_json):
        source = feature({"type": "Polygon", "coordinates": [RING]})
        original = copy.deepcopy(source)
        get_json.return_value = source
        result = areas.geometry(EPCI)
        self.assertEqual(result["type"], "Polygon")
        # EPSG:2154 false origin: 3 degrees east, 46.5 degrees north.
        x, y = result["coordinates"][0][0]
        self.assertAlmostEqual(x, 700000, places=5)
        self.assertAlmostEqual(y, 6600000, places=5)
        self.assertEqual(result["coordinates"][0][0], result["coordinates"][0][-1])
        self.assertEqual(source, original)
        self.assertIn((700, 6601), areas.tiles_for(result))
        self.assertGreater(areas.area_km2(result), 3)
        self.assertLess(areas.area_km2(result), 4)
        url = urlparse(get_json.call_args.args[0])
        self.assertEqual(url.path, "/epcis/243300316")
        self.assertEqual(parse_qs(url.query)["geometry"], ["contour"])
        self.assertEqual(parse_qs(url.query)["format"], ["geojson"])

    @patch("lidar_hd.areas.get_json")
    def test_multipolygon_holes_and_ring_orientation(self, get_json):
        hole = square(3.004, 46.504, 3.008, 46.508)
        island = square(3.03, 46.5, 3.04, 46.51)
        coordinates = [[RING, list(reversed(hole))], [island]]
        get_json.return_value = feature({"type": "MultiPolygon", "coordinates": coordinates})
        result = areas.geometry(EPCI)
        self.assertEqual(result["type"], "MultiPolygon")
        self.assertEqual([len(poly) for poly in result["coordinates"]], [2, 1])
        get_json.return_value = feature({"type": "MultiPolygon", "coordinates": [
            [list(reversed(ring)) for ring in poly] for poly in coordinates]})
        reversed_result = areas.geometry(EPCI)
        self.assertAlmostEqual(areas.area_km2(result), areas.area_km2(reversed_result))
        self.assertEqual(areas.tiles_for(result), areas.tiles_for(reversed_result))

    @patch("lidar_hd.areas.get_json")
    def test_missing_geometry(self, get_json):
        for response in (None, {}, feature(None), feature({"type": "Polygon", "coordinates": []})):
            get_json.return_value = response
            with self.subTest(response=response), self.assertRaises(LookupError):
                areas.geometry(EPCI)

    @patch("lidar_hd.areas.get_json")
    def test_invalid_geometry_is_rejected(self, get_json):
        for geometry in (
            {"type": "Point", "coordinates": [3, 46.5]},
            {"type": "MultiPolygon", "coordinates": 42},
            {"type": "MultiPolygon", "coordinates": [None]},
            {"type": "Polygon", "coordinates": [[None] * 4]},
            {"type": "Polygon", "coordinates": [[]]},
            {"type": "Polygon", "coordinates": [[[3, 46.5]]]},
            {"type": "Polygon", "coordinates": [[[3, 46.5], [4, 46.5], [4, 47], [3, 47]]]},
            {"type": "Polygon", "coordinates": [[[3, 100]] * 4]},
            {"type": "Polygon", "coordinates": [[[float("nan"), 46.5]] * 4]},
        ):
            get_json.return_value = feature(geometry)
            with self.subTest(geometry=geometry), self.assertRaises(ValueError):
                areas.geometry(EPCI)

    @patch("lidar_hd.areas.get_json")
    def test_invalid_code_does_not_become_url(self, get_json):
        for code in ("", "123", "../communes", "243300316?fields=centre"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                areas.geometry(areas.Area("epci", "Invalid", code))
        get_json.assert_not_called()


class EpciTileUnionTests(unittest.TestCase):
    def test_exact_grid_rectangle_has_no_boundary_only_tiles(self):
        for left, bottom in ((400000, 6400000), (-2000, -3000)):
            ring = square(left, bottom, left + 1000, bottom + 1000)
            for oriented in (ring, list(reversed(ring))):
                with self.subTest(left=left, ring=oriented):
                    self.assertEqual(areas.tiles_for({"type": "Polygon", "coordinates": [oriented]}),
                                     [(left // 1000, bottom // 1000 + 1)])

    def test_thin_intersections_between_sample_lines(self):
        for ring, expected in (
            (square(400100, 6400000.001, 401100, 6400000.002), [(400, 6401), (401, 6401)]),
            (square(400999.999, 6400100, 401000.001, 6400900), [(400, 6401), (401, 6401)]),
            ([[400100, 6400001], [401001, 6400001], [400100, 6400002], [400100, 6400001]],
             [(400, 6401), (401, 6401)]),
            ([[400100, 6400100], [401000, 6400500], [400100, 6400900], [400100, 6400100]],
             [(400, 6401)]),
            ([[400100, 6400100], [401001, 6400500], [400100, 6400900], [400100, 6400100]],
             [(400, 6401), (401, 6401)]),
        ):
            with self.subTest(ring=ring):
                self.assertEqual(areas.tiles_for({"type": "Polygon", "coordinates": [ring]}), expected)

    def test_grid_aligned_hole_excludes_boundary_only_tile(self):
        shell = square(400000, 6400000, 403000, 6403000)
        hole = square(401000, 6401000, 402000, 6402000)
        expected = [(x, y) for x in range(400, 403) for y in range(6401, 6404)
                    if (x, y) != (401, 6402)]
        self.assertEqual(areas.tiles_for({"type": "Polygon", "coordinates": [shell, hole]}), expected)

    def test_thin_diagonal_across_rows_and_vertex_only_contacts(self):
        for ring, expected in (
            ([[400000, 6400000], [403000, 6403000], [402999, 6403000], [400000, 6400000]],
             [(400, 6401), (400, 6402), (401, 6402), (401, 6403), (402, 6403)]),
            ([[400000, 6401000], [401000, 6400000], [402000, 6401000],
              [401000, 6402000], [400000, 6401000]],
             [(400, 6401), (400, 6402), (401, 6401), (401, 6402)]),
        ):
            for oriented in (ring, list(reversed(ring))):
                with self.subTest(ring=oriented):
                    self.assertEqual(areas.tiles_for({"type": "Polygon", "coordinates": [oriented]}),
                                     expected)

    def test_zero_area_ring_has_no_tiles(self):
        ring = [[400000, 6400000], [401000, 6401000], [400000, 6400000]]
        self.assertEqual(areas.tiles_for({"type": "Polygon", "coordinates": [ring]}), [])

    def test_union_not_even_odd_cancellation_and_no_duplicate_tiles(self):
        first = [square(700100, 6600100, 703900, 6603900)]
        second = [square(701100, 6601100, 704900, 6604900)]
        parts = [first, second, first]
        expected = set().union(*(set(areas.tiles_for({"type": "Polygon", "coordinates": p})) for p in parts))
        result = areas.tiles_for({"type": "MultiPolygon", "coordinates": parts})
        self.assertEqual(result, sorted(expected))

    def test_hole_is_not_filled_but_other_polygon_can_cover_it(self):
        shell = square(700100, 6600100, 708900, 6608900)
        hole = square(701100, 6601100, 707900, 6607900)
        island = square(703100, 6603100, 704900, 6604900)
        polygon = {"type": "Polygon", "coordinates": [shell, hole]}
        self.assertNotIn((703, 6604), areas.tiles_for(polygon))
        result = areas.tiles_for({"type": "MultiPolygon", "coordinates": [[shell, hole], [island]]})
        self.assertIn((703, 6604), result)
        self.assertNotIn((705, 6606), result)


if __name__ == "__main__":
    unittest.main()