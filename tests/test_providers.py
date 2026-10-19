"""彩云解析与强度分级的单元测试（纯本地）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rain_monitor as rm  # noqa: E402


class ParseWeatherTests(unittest.TestCase):
    def _realtime(self, intensity: float = 0.0) -> dict:
        return {
            "status": "ok",
            "skycon": "RAIN",
            "precipitation": {
                "local": {"status": "ok", "datasource": "radar", "intensity": intensity},
                "nearest": {"status": "ok", "distance": 190.0, "intensity": 1.57},
            },
        }

    def test_prefers_minutely_when_present(self) -> None:
        payload = {
            "status": "ok",
            "result": {
                "realtime": self._realtime(),
                "minutely": {
                    "status": "ok",
                    "datasource": "radar",
                    "precipitation_2h": [0.42] + [0.0] * 119,
                    "description": "预计 20 分钟后转为小雨",
                },
                "forecast_keypoint": "预计 20 分钟后转为小雨",
            },
        }
        parsed = rm.parse_weather(payload)
        self.assertEqual(parsed["provider"], "minutely")
        self.assertEqual(parsed["intensity"], 0.42)
        self.assertEqual(parsed["forecast"], "")

    def test_without_minutely_uses_realtime_and_forecast(self) -> None:
        payload = {
            "status": "ok",
            "result": {
                "realtime": self._realtime(1.5),
                "forecast_keypoint": "多云，明天午夜 2 点后转阴",
            },
        }
        parsed = rm.parse_weather(payload)
        self.assertEqual(parsed["provider"], "realtime")
        self.assertEqual(parsed["intensity"], 1.5)
        self.assertEqual(parsed["description_label"], "彩云实况")
        self.assertEqual(parsed["forecast"], "多云，明天午夜 2 点后转阴")

    def test_without_realtime_raises(self) -> None:
        with self.assertRaises(rm.MonitorError):
            rm.parse_weather({"status": "ok", "result": {"primary": 0}})


class ParseMinutelyTests(unittest.TestCase):
    def test_missing_minutely_returns_none(self) -> None:
        payload = {"status": "ok", "result": {"primary": 0}}
        self.assertIsNone(rm.parse_minutely(payload))

    def test_parse_minutely_mmh(self) -> None:
        payload = {
            "status": "ok",
            "result": {
                "minutely": {
                    "status": "ok",
                    "datasource": "radar",
                    "precipitation_2h": [0.42] + [0.0] * 119,
                    "description": "预计 20 分钟后转为小雨",
                },
                "forecast_keypoint": "预计 20 分钟后转为小雨",
            },
        }
        parsed = rm.parse_minutely(payload)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["intensity"], 0.42)
        self.assertEqual(parsed["provider"], "minutely")
        self.assertEqual(parsed["description_label"], "彩云预报")
        self.assertEqual(parsed["description"], "预计 20 分钟后转为小雨")
        self.assertEqual(parsed["forecast_minutes"], 120)

    def test_description_falls_back_to_forecast_keypoint(self) -> None:
        payload = {
            "status": "ok",
            "result": {
                "minutely": {
                    "status": "ok",
                    "precipitation_2h": [0.0] * 120,
                },
                "forecast_keypoint": "未来两小时不会下雨",
            },
        }
        parsed = rm.parse_minutely(payload)
        assert parsed is not None
        self.assertEqual(parsed["description"], "未来两小时不会下雨")

    def test_failed_status_raises(self) -> None:
        with self.assertRaises(rm.MonitorError):
            rm.parse_minutely({"status": "failed", "error": "token is invalid"})


class ParseRealtimeTests(unittest.TestCase):
    def _payload(self, intensity: float, distance: float = 230.0) -> dict:
        return {
            "status": "ok",
            "result": {
                "realtime": {
                    "status": "ok",
                    "skycon": "RAIN",
                    "precipitation": {
                        "local": {
                            "status": "ok",
                            "datasource": "radar",
                            "intensity": intensity,
                        },
                        "nearest": {
                            "status": "ok",
                            "distance": distance,
                            "intensity": 2.5,
                        },
                    },
                }
            },
        }

    def test_parse_realtime_mmh(self) -> None:
        parsed = rm.parse_realtime(self._payload(1.57))
        self.assertEqual(parsed["intensity"], 1.57)
        self.assertEqual(parsed["provider"], "realtime")
        self.assertEqual(parsed["description_label"], "彩云实况")
        self.assertIn("本地降水 1.57 mm/h", parsed["description"])

    def test_dry_realtime_mentions_nearest_band(self) -> None:
        parsed = rm.parse_realtime(self._payload(0.0, distance=189.0))
        self.assertEqual(parsed["intensity"], 0.0)
        self.assertIn("本地无降水", parsed["description"])
        self.assertIn("最近降水带约 189 米外", parsed["description"])

    def test_missing_realtime_raises(self) -> None:
        with self.assertRaises(rm.MonitorError):
            rm.parse_realtime({"status": "ok", "result": {}})

    def test_invalid_intensity_raises(self) -> None:
        payload = self._payload(0.0)
        payload["result"]["realtime"]["precipitation"]["local"]["intensity"] = "abc"
        with self.assertRaises(rm.MonitorError):
            rm.parse_realtime(payload)


class IntensityLabelTests(unittest.TestCase):
    def test_thresholds(self) -> None:
        self.assertEqual(rm._intensity_label(0.0), "无雨")
        self.assertEqual(rm._intensity_label(0.08), "小雨")
        self.assertEqual(rm._intensity_label(3.43), "小雨")
        self.assertEqual(rm._intensity_label(3.44), "中雨")
        self.assertEqual(rm._intensity_label(11.33), "大雨")
        self.assertEqual(rm._intensity_label(51.30), "暴雨")


if __name__ == "__main__":
    unittest.main(verbosity=2)
