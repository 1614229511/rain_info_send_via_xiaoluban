"""坐标系转换单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import geo  # noqa: E402
import rain_monitor as rm  # noqa: E402


class GeoConversionTests(unittest.TestCase):
    def test_hangzhou_offset_is_about_500m(self) -> None:
        lat, lon = 30.17731, 120.19265
        glat, glon = geo.wgs84_to_gcj02(lat, lon)
        self.assertAlmostEqual(glat, 30.174906, places=5)
        self.assertAlmostEqual(glon, 120.197226, places=5)

    def test_outside_china_is_unchanged(self) -> None:
        lat, lon = 35.6762, 139.6503  # Tokyo
        self.assertTrue(geo.out_of_china(lat, lon))
        self.assertEqual(geo.wgs84_to_gcj02(lat, lon), (lat, lon))


class ApiCoordinateTests(unittest.TestCase):
    def test_config_wgs84_is_converted(self) -> None:
        cfg = {"coordinate_system": "wgs84"}
        location = {
            "latitude": 30.17731,
            "longitude": 120.19265,
            "source": "config",
            "coordinate_system": "wgs84",
        }
        result = rm._to_api_coordinates(location, cfg)
        self.assertEqual(result["coordinate_system"], "gcj02")
        self.assertAlmostEqual(result["latitude"], 30.174906, places=5)
        self.assertAlmostEqual(result["longitude"], 120.197226, places=5)
        self.assertEqual(result["wgs84_latitude"], 30.17731)

    def test_config_gcj02_is_kept(self) -> None:
        cfg = {"coordinate_system": "gcj02"}
        location = {
            "latitude": 30.174906,
            "longitude": 120.197226,
            "source": "config",
            "coordinate_system": "gcj02",
        }
        result = rm._to_api_coordinates(location, cfg)
        self.assertEqual(result["latitude"], 30.174906)
        self.assertEqual(result["longitude"], 120.197226)

    def test_system_location_without_flag_is_treated_as_wgs84(self) -> None:
        cfg = {"coordinate_system": "gcj02"}
        location = {
            "latitude": 30.17731,
            "longitude": 120.19265,
            "source": "windows_geolocator",
        }
        result = rm._to_api_coordinates(location, cfg)
        self.assertEqual(result["coordinate_system"], "gcj02")
        self.assertAlmostEqual(result["latitude"], 30.174906, places=5)

    def test_old_cached_gcj02_is_not_converted_twice(self) -> None:
        cfg = {"coordinate_system": "gcj02"}
        location = {
            "latitude": 30.174906,
            "longitude": 120.197226,
            "source": "windows_geolocator",
            "coordinate_system": "gcj02",
        }
        result = rm._to_api_coordinates(location, cfg)
        self.assertEqual(result["latitude"], 30.174906)
        self.assertEqual(result["longitude"], 120.197226)


if __name__ == "__main__":
    unittest.main(verbosity=2)
