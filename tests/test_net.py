"""代理与直连策略单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.request import ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import net  # noqa: E402


class ProxyConfigTests(unittest.TestCase):
    def test_normalize(self) -> None:
        for value in (None, "", "none", "direct", "off", "FALSE"):
            self.assertEqual(net.normalize_proxy(value), "")
        self.assertEqual(net.normalize_proxy("system"), "system")
        self.assertEqual(net.normalize_proxy(" SYSTEM "), "system")
        self.assertEqual(
            net.normalize_proxy("http://127.0.0.1:7890"),
            "http://127.0.0.1:7890",
        )

    def test_validate(self) -> None:
        self.assertEqual(net.validate_proxy(""), "none")
        self.assertEqual(net.validate_proxy("system"), "system")
        self.assertEqual(
            net.validate_proxy("http://127.0.0.1:7890"),
            "http://127.0.0.1:7890",
        )
        with self.assertRaises(ValueError):
            net.validate_proxy("socks5://127.0.0.1:1080")

    def test_parse_bypass(self) -> None:
        self.assertEqual(
            net.parse_bypass("localhost, 127.0.0.1,*.huawei.com"),
            ["localhost", "127.0.0.1", "*.huawei.com"],
        )
        self.assertEqual(net.parse_bypass(["a", "a", " b "]), ["a", "b"])
        self.assertEqual(net.parse_bypass(None), [])


class BypassTests(unittest.TestCase):
    def test_huawei_domains_bypass(self) -> None:
        patterns = ["localhost", "127.0.0.1", "*.huawei.com"]
        self.assertTrue(net.host_bypassed("xiaoluban.rnd.huawei.com", patterns))
        self.assertTrue(net.host_bypassed("huawei.com", patterns))
        self.assertFalse(net.host_bypassed("api.caiyunapp.com", patterns))

    def test_localhost(self) -> None:
        self.assertTrue(net.host_bypassed("localhost", ["localhost"]))
        self.assertTrue(net.host_bypassed("127.0.0.1", ["127.0.0.1"]))
        self.assertFalse(net.host_bypassed("localhost.example.com", ["localhost"]))


class OpenerTests(unittest.TestCase):
    def _proxy_handler(self, opener) -> ProxyHandler | None:  # noqa: ANN001
        for handler in opener.handlers:
            if isinstance(handler, ProxyHandler):
                return handler
        # urllib 会把空的 ProxyHandler 丢弃；没有 ProxyHandler 即直连。
        return None

    def _assert_direct(self, opener) -> None:  # noqa: ANN001
        handler = self._proxy_handler(opener)
        self.assertTrue(handler is None or handler.proxies == {})

    def test_default_is_direct(self) -> None:
        opener = net.build_opener_for("http://xiaoluban.rnd.huawei.com/", "none")
        self._assert_direct(opener)

    def test_manual_proxy(self) -> None:
        opener = net.build_opener_for(
            "https://api.caiyunapp.com/",
            "http://127.0.0.1:7890",
        )
        handler = self._proxy_handler(opener)
        self.assertIsNotNone(handler)
        assert handler is not None
        self.assertEqual(
            handler.proxies,
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )

    def test_bypass_forces_direct(self) -> None:
        opener = net.build_opener_for(
            "http://xiaoluban.rnd.huawei.com/",
            "http://127.0.0.1:7890",
            ["*.huawei.com"],
        )
        self._assert_direct(opener)

    def test_proxy_label(self) -> None:
        self.assertEqual(net.proxy_label("none"), "直连")
        self.assertEqual(net.proxy_label("system"), "系统代理")
        self.assertEqual(net.proxy_label("http://127.0.0.1:7890"), "手动代理")


if __name__ == "__main__":
    unittest.main(verbosity=2)
