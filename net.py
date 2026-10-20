"""网络辅助：显式控制 urllib 的代理行为。

Windows 上 urllib 默认会读取注册表里的系统代理（WinINET 设置）。计划任务
在代理客户端没有授权的上下文里运行时，可能被代理拒绝创建套接字。
因此这里的默认策略是“直连”，只有明确配置 proxy=system 或手动代理时才走代理。

proxy 取值：
    "" / "none" / "direct"   直连（默认）
    "system"                 使用系统代理（注册表/环境变量）
    "http://host:port"       手动 HTTP 代理
    "https://host:port"      手动 HTTPS 代理

proxy_bypass 支持精确域名、子域名和 *.domain：
    "localhost"、"127.0.0.1"、"*.huawei.com"
"""

from __future__ import annotations

from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

_DIRECT_VALUES = {"", "none", "direct", "off", "false", "no"}


def normalize_proxy(value: object) -> str:
    """把配置值规范成 ""（直连）、"system" 或代理 URL。"""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in _DIRECT_VALUES:
        return ""
    if lowered == "system":
        return "system"
    return text


def validate_proxy(value: object) -> str:
    """校验 proxy 配置；返回规范值（直连时返回 "none"）。"""
    mode = normalize_proxy(value)
    if not mode:
        return "none"
    if mode == "system":
        return mode
    parts = urlsplit(mode)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(
            "proxy 仅支持 none / system / http://host:port / https://host:port"
        )
    return mode


def parse_bypass(value: object) -> list[str]:
    """把 proxy_bypass 规范成字符串列表，支持列表或逗号分隔字符串。"""
    if value is None:
        return []
    if isinstance(value, str):
        items: list[object] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]

    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def host_bypassed(host: str, patterns: object) -> bool:
    """判断 host 是否命中直连列表。"""
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return False

    for raw in parse_bypass(patterns):
        pattern = raw.lower().rstrip(".")
        if pattern.startswith("*."):
            pattern = pattern[1:]
        pattern = pattern.lstrip(".")
        if not pattern:
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def build_opener_for(url: str, proxy: object = "", bypass: object = None):
    """为指定 URL 构造 opener：默认直连，按配置决定是否走代理。"""
    mode = normalize_proxy(proxy)
    if not mode:
        return build_opener(ProxyHandler({}))

    host = urlsplit(url).hostname or ""
    if host_bypassed(host, bypass):
        return build_opener(ProxyHandler({}))

    if mode == "system":
        # ProxyHandler() 不带参数时读取系统/环境代理设置。
        return build_opener(ProxyHandler())

    return build_opener(ProxyHandler({"http": mode, "https": mode}))


def proxy_label(proxy: object) -> str:
    mode = normalize_proxy(proxy)
    if not mode:
        return "直连"
    if mode == "system":
        return "系统代理"
    return "手动代理"
