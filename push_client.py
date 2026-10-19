#!/usr/bin/env python3
"""小鲁班消息推送客户端（独立、无第三方依赖）。

既可作为模块导入：

    from push_client import send_msg
    result = send_msg("你好", receiver="a00123456", auth="TOKEN")

也可作为命令行脚本被其他程序调用（推荐由 rain_monitor.py 以子进程方式调用）：

    echo '{"content":"你好","receiver":"a00123456","auth":"TOKEN"}' | python push_client.py --json
    python push_client.py --content "你好" --receiver a00123456 --auth TOKEN

命令行始终向 stdout 输出一行 JSON：
    成功：{"ok": true, "status": 200, "response": "..."}
    失败：{"ok": false, "status": 500, "error": "...", "response": "..."}
退出码：0 成功，1 发送失败，2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://xiaoluban.rnd.huawei.com:80/"
USER_AGENT = "rain-monitor/1.0 (+local)"


def send_msg(
    content: str,
    receiver: str,
    auth: str,
    sender: str | None = None,
    url: str = DEFAULT_URL,
    timeout: float = 10.0,
) -> dict:
    """发送一条小鲁班消息，返回结果字典（不抛网络异常）。

    content 支持换行与 HTML（颜色/加粗），与官方 demo 一致。
    """
    if not content:
        return {"ok": False, "error": "content 不能为空"}
    if not receiver:
        return {"ok": False, "error": "receiver 不能为空"}
    if not auth:
        return {"ok": False, "error": "auth 不能为空"}

    data: dict[str, str] = {"content": content, "receiver": receiver, "auth": auth}
    if sender:
        data["sender"] = sender

    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return {"ok": True, "status": response.status, "response": text.strip()}
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读取响应体失败不应掩盖原始错误
            text = ""
        return {
            "ok": False,
            "status": exc.code,
            "error": f"HTTP {exc.code} {exc.reason}",
            "response": text.strip()[:1000],
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return {"ok": False, "error": f"网络错误：{reason}"}
    except Exception as exc:  # noqa: BLE001 - 命令行工具需要兜底，避免栈信息污染 JSON
        return {"ok": False, "error": f"发送失败：{exc}"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过小鲁班发送一条消息（输出 JSON）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--content", help="消息内容，支持换行与 HTML")
    parser.add_argument("--receiver", help="接收人 uid，例如 a00123456")
    parser.add_argument("--auth", help="小鲁班认证 token")
    parser.add_argument("--sender", help="可选的发送人标识")
    parser.add_argument("--url", default=DEFAULT_URL, help="小鲁班推送地址")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP 超时（秒）")
    parser.add_argument(
        "--json",
        action="store_true",
        help="从 stdin 读取 JSON 参数（content/receiver/auth/sender/url），命令行参数优先",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # 避免 Windows 控制台默认编码导致中文/emoji 输出失败。
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = _build_parser().parse_args(argv)

    payload: dict = {}
    if args.json or not args.content:
        raw = ""
        if sys.stdin is not None:
            try:
                raw = sys.stdin.read()
            except Exception:  # noqa: BLE001
                raw = ""
        raw = raw.strip().lstrip("\ufeff")
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(
                    json.dumps(
                        {"ok": False, "error": f"stdin 不是合法 JSON：{exc}"},
                        ensure_ascii=False,
                    )
                )
                return 2
            if isinstance(parsed, dict):
                payload = parsed

    content = args.content or payload.get("content")
    receiver = args.receiver or payload.get("receiver")
    auth = args.auth or payload.get("auth")
    sender = args.sender or payload.get("sender")
    url = args.url or payload.get("url") or DEFAULT_URL
    timeout = args.timeout or float(payload.get("timeout") or 10.0)

    if not content or not receiver or not auth:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "缺少参数：需要 content、receiver、auth（命令行或 --json stdin）",
                },
                ensure_ascii=False,
            )
        )
        return 2

    result = send_msg(
        content=content,
        receiver=receiver,
        auth=auth,
        sender=sender,
        url=url,
        timeout=timeout,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
