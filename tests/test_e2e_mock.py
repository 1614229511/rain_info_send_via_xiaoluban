"""端到端联调：本地 mock 彩云接口 + 本地 mock 小鲁班，验证完整链路。

不访问公网、不依赖真实 token。覆盖：
    * 经纬度按“经度,纬度”顺序拼进 URL
    * 开始下雨推送 / 持续降雨不重复推送
    * 连续 3 次无雨后才推送雨停
    * 冷却期内开始通知被抑制，且雨停也不误报
    * 推送失败时状态回滚，下一轮可重试
    * token 无 minutely 权限时自动降级到 realtime，并缓存 6 小时
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import state_machine as sm  # noqa: E402


class MockHandler(BaseHTTPRequestHandler):
    intensity = 0.5
    description = "模拟：预计未来两小时不会下雨"
    forecast_keypoint = "模拟预报：未来两小时不会下雨"
    minutely_available = True
    realtime_intensity = 0.0
    realtime_skycon = "RAIN"
    pushes: list[dict] = []
    requests: list[str] = []

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        MockHandler.requests.append(self.path)

        if "/weather" in self.path:
            result: dict = {
                "realtime": {
                    "status": "ok",
                    "skycon": MockHandler.realtime_skycon,
                    "precipitation": {
                        "local": {
                            "status": "ok",
                            "datasource": "radar",
                            "intensity": MockHandler.realtime_intensity,
                        },
                        "nearest": {
                            "status": "ok",
                            "distance": 230.0,
                            "intensity": 2.5,
                        },
                    },
                },
                "forecast_keypoint": MockHandler.forecast_keypoint,
            }
            if MockHandler.minutely_available:
                result["minutely"] = {
                    "status": "ok",
                    "datasource": "radar",
                    "precipitation_2h": [MockHandler.intensity] + [0.0] * 119,
                    "description": MockHandler.description,
                }
            self._send_json({"status": "ok", "result": result})
            return

        if "/minutely" in self.path:
            if not MockHandler.minutely_available:
                # 彩云在 token 无分钟级权限时的真实响应：只有 result.primary。
                self._send_json({"status": "ok", "result": {"primary": 0}})
                return
            self._send_json(
                {
                    "status": "ok",
                    "result": {
                        "minutely": {
                            "status": "ok",
                            "datasource": "radar",
                            "precipitation_2h": [MockHandler.intensity] + [0.0] * 119,
                            "description": MockHandler.description,
                        }
                    },
                }
            )
            return

        if "/realtime" in self.path:
            self._send_json(
                {
                    "status": "ok",
                    "result": {
                        "realtime": {
                            "status": "ok",
                            "skycon": MockHandler.realtime_skycon,
                            "precipitation": {
                                "local": {
                                    "status": "ok",
                                    "datasource": "radar",
                                    "intensity": MockHandler.realtime_intensity,
                                },
                                "nearest": {
                                    "status": "ok",
                                    "distance": 230.0,
                                    "intensity": 2.5,
                                },
                            },
                        }
                    },
                }
            )
            return

        self._send_json({"status": "failed", "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        MockHandler.pushes.append(json.loads(raw))
        self._send_json({"ok": True})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockServer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def __enter__(self) -> "MockServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.server.shutdown()
        self.server.server_close()


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        MockHandler.intensity = 0.5
        MockHandler.description = "模拟：预计未来两小时不会下雨"
        MockHandler.forecast_keypoint = "模拟预报：未来两小时不会下雨"
        MockHandler.minutely_available = True
        MockHandler.realtime_intensity = 0.0
        MockHandler.realtime_skycon = "RAIN"
        MockHandler.pushes.clear()
        MockHandler.requests.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "runtime_state.json"
        self.log_dir = self.root / "logs"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_config(self, port: int, push_url: str | None = None) -> None:
        config = {
            "weather_token": "test-token",
            "latitude": 30.1,
            "longitude": 120.1,
            "receiver": "a00123456",
            "auth": "test-auth",
            "interval_minutes": 5,
            "rain_threshold": 0.08,
            "dry_confirm_count": 3,
            "min_push_interval_minutes": 30,
            "request_timeout_seconds": 5,
            "location_cache_minutes": 60,
            "api_base": f"http://127.0.0.1:{port}",
            "push_url": push_url or f"http://127.0.0.1:{port}/",
            "log_level": "INFO",
        }
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def run_monitor(self, *extra: str) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(ROOT / "rain_monitor.py"),
            "--once",
            "--config",
            str(self.config_path),
            "--state",
            str(self.state_path),
            "--log-dir",
            str(self.log_dir),
            "--quiet",
            *extra,
        ]
        return subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def read_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_full_lifecycle(self) -> None:
        with MockServer() as server:
            self.write_config(server.port)

            # 1) 开始下雨
            result = self.run_monitor()
            self.assertEqual(result.returncode, 0, result.stderr)
            state = self.read_state()
            self.assertEqual(state["status"], "RAINING")
            self.assertEqual(len(MockHandler.pushes), 1)
            self.assertIn("开始下雨", MockHandler.pushes[0]["content"])
            self.assertIn("0.50 mm/h", MockHandler.pushes[0]["content"])
            self.assertEqual(MockHandler.pushes[0]["receiver"], "a00123456")
            self.assertTrue(
                any(
                    "/v2.6/test-token/120.100000,30.100000/weather" in path
                    and "unit=metric:v2" in path
                    for path in MockHandler.requests
                ),
                MockHandler.requests,
            )

            # 2) 持续下雨：不重复推送
            result = self.run_monitor()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(MockHandler.pushes), 1)

            # 3) 连续 3 次无雨：第 3 次才推送雨停
            MockHandler.intensity = 0.01
            MockHandler.description = "模拟：雨已停"
            for index in range(3):
                result = self.run_monitor()
                self.assertEqual(result.returncode, 0, result.stderr)
                state = self.read_state()
                if index < 2:
                    self.assertEqual(state["status"], "RAINING")
                    self.assertEqual(state["dry_counter"], index + 1)
                    self.assertEqual(len(MockHandler.pushes), 1)
                else:
                    self.assertEqual(state["status"], "DRY")
                    self.assertEqual(state["dry_counter"], 0)
                    self.assertEqual(len(MockHandler.pushes), 2)
                    self.assertIn("雨停了", MockHandler.pushes[1]["content"])

            # 4) 冷却期内开始通知被抑制，雨停也不误报
            cooling_state = {
                "status": "DRY",
                "last_event_time": int(time.time()),
                "dry_counter": 0,
                "rain_announced": False,
            }
            self.state_path.write_text(
                json.dumps(cooling_state, ensure_ascii=False), encoding="utf-8"
            )
            MockHandler.intensity = 0.5
            result = self.run_monitor()
            self.assertEqual(result.returncode, 0, result.stderr)
            state = self.read_state()
            self.assertEqual(state["status"], "RAINING")
            self.assertFalse(state["rain_announced"])
            self.assertEqual(len(MockHandler.pushes), 2)

            MockHandler.intensity = 0.0
            for _ in range(3):
                self.run_monitor()
            state = self.read_state()
            self.assertEqual(state["status"], "DRY")
            self.assertEqual(len(MockHandler.pushes), 2)

    def test_weather_without_minutely_uses_realtime(self) -> None:
        with MockServer() as server:
            MockHandler.minutely_available = False
            MockHandler.realtime_intensity = 1.5
            MockHandler.realtime_skycon = "RAIN"
            self.write_config(server.port)

            result = self.run_monitor()
            self.assertEqual(result.returncode, 0, result.stderr)
            state = self.read_state()
            self.assertEqual(state["status"], "RAINING")
            self.assertEqual(state["last_provider"], "realtime")
            self.assertNotIn("minutely_blocked_until", state)
            self.assertEqual(len(MockHandler.requests), 1)
            self.assertIn("/weather", MockHandler.requests[0])
            self.assertIn("unit=metric:v2", MockHandler.requests[0])
            self.assertEqual(len(MockHandler.pushes), 1)
            content = MockHandler.pushes[0]["content"]
            self.assertIn("开始下雨", content)
            self.assertIn("彩云实况", content)
            self.assertIn("1.50 mm/h", content)
            self.assertIn("彩云预报：模拟预报", content)

            # 第二轮仍然只发一次 /weather 请求；持续降雨不重复推送
            result = self.run_monitor()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(MockHandler.requests), 2)
            self.assertTrue(all("/weather" in p for p in MockHandler.requests))
            self.assertEqual(len(MockHandler.pushes), 1)

    def test_push_failure_reverts_and_retries(self) -> None:
        with MockServer() as server:
            dead_port = _free_port()
            self.write_config(server.port, push_url=f"http://127.0.0.1:{dead_port}/")

            result = self.run_monitor()
            self.assertEqual(result.returncode, 1)
            state = self.read_state()
            self.assertEqual(state["status"], "DRY")
            self.assertEqual(state["rain_announced"], False)
            self.assertTrue(state.get("last_error"))

            # 推送恢复后，同一场雨会重新触发开始通知
            self.write_config(server.port)
            result = self.run_monitor()
            self.assertEqual(result.returncode, 0, result.stderr)
            state = self.read_state()
            self.assertEqual(state["status"], "RAINING")
            self.assertEqual(len(MockHandler.pushes), 1)
            self.assertIn("开始下雨", MockHandler.pushes[0]["content"])

    def test_status_command_does_not_require_credentials(self) -> None:
        config = {
            "weather_token": "",
            "receiver": "",
            "auth": "",
            "latitude": 30.1,
            "longitude": 120.1,
        }
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        command = [
            sys.executable,
            str(ROOT / "rain_monitor.py"),
            "--status",
            "--config",
            str(self.config_path),
            "--state",
            str(self.state_path),
            "--log-dir",
            str(self.log_dir),
        ]
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：无雨", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
