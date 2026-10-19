#!/usr/bin/env python3
"""街道级即时降雨监控与起止状态通知。

每个周期依次执行：
    1. 读取 config.json / runtime_state.json
    2. 位置解析：显式坐标 -> 系统原生定位 -> 公网 IP 兜底
    3. 请求彩云天气 minutely 接口（公里级网格，未来两小时分钟级降水）
    4. 状态机推导 + 防抖判定
    5. 仅在有效跳变时调用 push_client.py 推送
    6. 原子回写 runtime_state.json

用法：
    python rain_monitor.py --once          # 执行一轮（计划任务用这个）
    python rain_monitor.py --loop          # 常驻循环，每 5 分钟一轮
    python rain_monitor.py --status        # 查看当前状态，不联网
    python rain_monitor.py --test-push     # 发送一条测试消息
    python rain_monitor.py --dry-run       # 只拉取和评估，不推送、不写状态
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import state_machine as sm
import geo

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "runtime_state.json"
LOG_DIR = BASE_DIR / "logs"
PUSH_CLIENT = BASE_DIR / "push_client.py"

USER_AGENT = "rain-monitor/1.0 (+local)"
IP_API_URL = "http://ip-api.com/json?lang=zh-CN"

SOURCE_LABELS = {
    "config": "手动配置",
    "windows_geolocator": "Windows 定位",
    "macos_corelocation": "macOS 定位",
    "ip": "IP 定位（粗精度）",
}

DEFAULT_CONFIG: dict = {
    "weather_token": "",
    "latitude": None,
    "longitude": None,
    "coordinate_system": "gcj02",
    "receiver": "",
    "auth": "",
    "sender": "",
    "interval_minutes": 5,
    "rain_threshold": 0.08,
    "dry_confirm_count": 3,
    "min_push_interval_minutes": 30,
    "request_timeout_seconds": 10,
    "location_cache_minutes": 60,
    "api_base": "https://api.caiyunapp.com",
    "push_url": "http://xiaoluban.rnd.huawei.com:80/",
    "log_level": "INFO",
}

ENV_OVERRIDES = {
    "weather_token": ("CAIYUN_TOKEN", "CAIYUN_WEATHER_TOKEN"),
    "receiver": ("XIAOLUBAN_RECEIVER",),
    "auth": ("XIAOLUBAN_AUTH",),
    "sender": ("XIAOLUBAN_SENDER",),
    "latitude": ("RAIN_LATITUDE", "RAIN_LAT"),
    "longitude": ("RAIN_LONGITUDE", "RAIN_LON"),
    "api_base": ("CAIYUN_API_BASE",),
    "push_url": ("XIAOLUBAN_URL",),
}


class ConfigError(Exception):
    """配置缺失或非法。"""


class MonitorError(Exception):
    """单轮监控过程中的可预期错误（网络、接口、定位等）。

    status_code 保存 HTTP 状态码（若有），便于区分 429 限流、401 鉴权失败等。
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# 日志与通用工具
# --------------------------------------------------------------------------- #
def configure_std_streams() -> None:
    """让 Windows 控制台/管道稳定输出 UTF-8（pythonw 下 stdout 可能为 None）。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def setup_logging(level: str, log_dir: Path, console: bool = True) -> logging.Logger:
    logger = logging.getLogger("rain_monitor")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        log_dir / "monitor.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console and sys.stderr is not None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.propagate = False
    return logger


def mask_secret(value: object) -> str:
    text = str(value or "")
    if not text:
        return "（未配置）"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-2:]}"


def _subprocess_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _console_python() -> str:
    """pythonw.exe 没有控制台，调用子脚本时优先换成同目录的 python.exe。"""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        candidate = executable.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)


def _sanitize(text: object, secrets: tuple[str, ...] = ()) -> str:
    result = str(text)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    return result


class InstanceLock:
    """基于排他创建锁文件，避免计划任务重叠执行。"""

    def __init__(self, path: Path, stale_seconds: int = 900):
        self.path = path
        self.stale_seconds = stale_seconds
        self.owned = False
        self._removed_stale = False

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(str(os.getpid()))
                self.owned = True
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self.stale_seconds and not self._removed_stale:
                    self._removed_stale = True
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                return False
        return False

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self.owned:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.owned = False


def _fmt_time(timestamp: object) -> str:
    try:
        value = int(float(timestamp))
    except (TypeError, ValueError):
        return "从未"
    if value <= 0:
        return "从未"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config(path: Path, logger: logging.Logger) -> dict:
    if not path.exists():
        raise ConfigError(
            f"未找到配置文件 {path}。请复制 config.example.json 为 config.json 并填写。"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件 {path} 的顶层必须是 JSON 对象。")

    cfg = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in ("latitude", "longitude"):
            if key in raw:
                cfg[key] = raw[key]
            continue
        if key in raw and raw[key] is not None:
            cfg[key] = raw[key]

    for key, names in ENV_OVERRIDES.items():
        for name in names:
            value = os.environ.get(name)
            if value:
                cfg[key] = value
                logger.debug("配置项 %s 被环境变量 %s 覆盖", key, name)
                break

    return cfg


def _to_float(value: object, key: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 必须是数字，当前为 {value!r}") from exc
    if not math.isfinite(number):
        raise ConfigError(f"配置项 {key} 必须是有限数字，当前为 {value!r}")
    return number


def validate_config(
    cfg: dict,
    require_credentials: bool = True,
    require_weather_token: bool = True,
) -> dict:
    cfg["interval_minutes"] = _to_float(cfg.get("interval_minutes"), "interval_minutes")
    if not 1 <= cfg["interval_minutes"] <= 1440:
        raise ConfigError("interval_minutes 必须在 1~1440 之间")

    cfg["rain_threshold"] = _to_float(cfg.get("rain_threshold"), "rain_threshold")
    if cfg["rain_threshold"] < 0:
        raise ConfigError("rain_threshold 不能为负数")

    dry_confirm = _to_float(cfg.get("dry_confirm_count"), "dry_confirm_count")
    if dry_confirm < 1:
        raise ConfigError("dry_confirm_count 必须 >= 1")
    cfg["dry_confirm_count"] = int(dry_confirm)

    cfg["min_push_interval_minutes"] = _to_float(
        cfg.get("min_push_interval_minutes"), "min_push_interval_minutes"
    )
    if cfg["min_push_interval_minutes"] < 0:
        raise ConfigError("min_push_interval_minutes 不能为负数")

    cfg["request_timeout_seconds"] = _to_float(
        cfg.get("request_timeout_seconds"), "request_timeout_seconds"
    )
    if not 1 <= cfg["request_timeout_seconds"] <= 120:
        raise ConfigError("request_timeout_seconds 必须在 1~120 之间")

    cfg["location_cache_minutes"] = _to_float(
        cfg.get("location_cache_minutes"), "location_cache_minutes"
    )
    if cfg["location_cache_minutes"] < 0:
        raise ConfigError("location_cache_minutes 不能为负数")

    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if (lat is None) != (lon is None):
        raise ConfigError("latitude 与 longitude 必须同时配置或同时留空")
    if lat is not None and lon is not None:
        cfg["latitude"] = _to_float(lat, "latitude")
        cfg["longitude"] = _to_float(lon, "longitude")
        if not -90 <= cfg["latitude"] <= 90:
            raise ConfigError("latitude 必须在 -90~90 之间")
        if not -180 <= cfg["longitude"] <= 180:
            raise ConfigError("longitude 必须在 -180~180 之间")

    for key in ("weather_token", "receiver", "auth", "sender", "api_base", "push_url", "log_level"):
        cfg[key] = str(cfg.get(key) or "").strip()

    cfg["coordinate_system"] = str(cfg.get("coordinate_system") or "gcj02").strip().lower()
    if cfg["coordinate_system"] not in ("gcj02", "wgs84"):
        raise ConfigError("coordinate_system 只能是 gcj02 或 wgs84")

    cfg["api_base"] = (cfg["api_base"] or DEFAULT_CONFIG["api_base"]).rstrip("/")
    cfg["push_url"] = cfg["push_url"] or DEFAULT_CONFIG["push_url"]
    cfg["log_level"] = (cfg["log_level"] or "INFO").upper()

    if require_weather_token and not cfg["weather_token"]:
        raise ConfigError(
            "缺少 weather_token：请在 config.json 填写彩云天气 token，"
            "或设置环境变量 CAIYUN_TOKEN。"
        )
    if require_credentials:
        if not cfg["receiver"]:
            raise ConfigError(
                "缺少 receiver：请在 config.json 填写小鲁班接收人 uid（如 a00123456），"
                "或设置环境变量 XIAOLUBAN_RECEIVER。"
            )
        if not cfg["auth"]:
            raise ConfigError(
                "缺少 auth：请在 config.json 填写小鲁班认证 token，"
                "或设置环境变量 XIAOLUBAN_AUTH。"
            )

    return cfg


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get_json(
    url: str,
    timeout: float,
    logger: logging.Logger,
    *,
    secrets: tuple[str, ...] = (),
    retries: int = 2,
    backoff: float = 1.0,
) -> dict:
    last_error: MonitorError | None = None

    for attempt in range(retries):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                },
            )
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise MonitorError("接口返回的 JSON 顶层不是对象")
            return payload
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                detail = ""
            message = _sanitize(
                f"HTTP {exc.code} {exc.reason}" + (f"：{detail[:300]}" if detail else ""),
                secrets,
            )
            # 429 限流：本轮直接放弃，等下一个 5 分钟周期，避免越重试越糟。
            if int(exc.code) == 429:
                raise MonitorError(
                    message + "（请求过于频繁，本轮跳过，下个周期再试）",
                    status_code=429,
                ) from None
            # 其他 4xx 通常重试无意义，直接失败。
            if 400 <= int(exc.code) < 500:
                raise MonitorError(message, status_code=int(exc.code)) from None
            last_error = MonitorError(message, status_code=int(exc.code))
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            last_error = MonitorError(_sanitize(f"网络错误：{reason}", secrets))
        except json.JSONDecodeError as exc:
            last_error = MonitorError(f"接口返回的不是合法 JSON：{exc}")

        if attempt + 1 < retries:
            logger.debug("请求失败，%.1f 秒后重试：%s", backoff * (attempt + 1), last_error)
            time.sleep(backoff * (attempt + 1))

    raise last_error or MonitorError("请求失败（未知原因）")


# --------------------------------------------------------------------------- #
# 位置解析：三级降级
# --------------------------------------------------------------------------- #
def _valid_lat_lon(latitude: object, longitude: object) -> bool:
    try:
        lat = float(latitude)  # type: ignore[arg-type]
        lon = float(longitude)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def _normalize_location(
    data: dict, source: str, now: int, extra: dict | None = None
) -> dict | None:
    latitude = data.get("latitude", data.get("lat"))
    longitude = data.get("longitude", data.get("lon", data.get("lng")))
    if not _valid_lat_lon(latitude, longitude):
        return None
    location = {
        "latitude": round(float(latitude), 6),
        "longitude": round(float(longitude), 6),
        "source": source,
        "fetched_at": int(now),
    }
    if data.get("accuracy") is not None:
        try:
            location["accuracy_m"] = round(float(data["accuracy"]), 1)
        except (TypeError, ValueError):
            pass
    if extra:
        location.update(extra)
    return location


def _cached_location(state: dict, cache_minutes: float, now: float) -> dict | None:
    if cache_minutes <= 0:
        return None
    cached = state.get("location")
    if not isinstance(cached, dict):
        return None
    if not _valid_lat_lon(cached.get("latitude"), cached.get("longitude")):
        return None
    try:
        fetched_at = float(cached.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return None
    # IP 定位精度粗，缓存时间额外收紧到 30 分钟。
    ttl = cache_minutes
    if cached.get("source") == "ip":
        ttl = min(ttl, 30.0)
    if now - fetched_at > ttl * 60:
        return None
    return dict(cached)


def _geolocate_windows(logger: logging.Logger) -> dict | None:
    script = BASE_DIR / "helpers" / "geolocate_windows.ps1"
    if not script.exists():
        logger.debug("未找到 Windows 定位脚本 %s", script)
        return None
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        logger.debug("未找到 powershell.exe，跳过系统定位")
        return None

    command = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=35,
            creationflags=_subprocess_flags(),
        )
    except subprocess.TimeoutExpired:
        logger.info("Windows 原生定位超时，降级到 IP 定位")
        return None
    except OSError as exc:
        logger.info("Windows 原生定位无法执行（%s），降级到 IP 定位", exc)
        return None

    stdout = completed.stdout.decode("utf-8-sig", "replace").strip()
    line = stdout.splitlines()[-1] if stdout else ""
    if not line:
        logger.info(
            "Windows 原生定位无输出（退出码 %s），降级到 IP 定位", completed.returncode
        )
        return None

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        logger.info("Windows 原生定位返回内容无法解析，降级到 IP 定位")
        logger.debug("定位原始输出：%s", stdout[-500:])
        return None

    if not payload.get("ok"):
        logger.info(
            "Windows 原生定位不可用（%s），降级到 IP 定位", payload.get("error") or "未知原因"
        )
        return None

    location = _normalize_location(
        payload,
        "windows_geolocator",
        int(time.time()),
        {"accuracy_m": payload.get("accuracy")},
    )
    if location:
        logger.info(
            "Windows 原生定位成功：%.5f, %.5f（精度约 %s 米）",
            location["latitude"],
            location["longitude"],
            location.get("accuracy_m", "未知"),
        )
    return location


def _geolocate_macos(logger: logging.Logger) -> dict | None:
    # 优先使用 CoreLocationCLI（自带 Info.plist，权限提示可靠）。
    cli = shutil.which("CoreLocationCLI")
    if cli:
        try:
            completed = subprocess.run(
                [cli, "-json", "-once"],
                capture_output=True,
                timeout=25,
            )
            stdout = completed.stdout.decode("utf-8", "replace").strip()
            if stdout:
                payload = json.loads(stdout.splitlines()[-1])
                location = _normalize_location(
                    payload, "macos_corelocation", int(time.time())
                )
                if location:
                    logger.info(
                        "CoreLocationCLI 定位成功：%.5f, %.5f",
                        location["latitude"],
                        location["longitude"],
                    )
                    return location
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            logger.info("CoreLocationCLI 定位失败（%s）", exc)

    # 退而求其次：用 swift 直接跑 CoreLocation 脚本。
    swift = shutil.which("swift")
    script = BASE_DIR / "helpers" / "geolocate_macos.swift"
    if swift and script.exists():
        try:
            completed = subprocess.run(
                [swift, str(script)],
                capture_output=True,
                timeout=40,
            )
            stdout = completed.stdout.decode("utf-8", "replace").strip()
            if stdout:
                payload = json.loads(stdout.splitlines()[-1])
                if payload.get("ok"):
                    location = _normalize_location(
                        payload, "macos_corelocation", int(time.time())
                    )
                    if location:
                        logger.info(
                            "macOS CoreLocation 定位成功：%.5f, %.5f",
                            location["latitude"],
                            location["longitude"],
                        )
                        return location
                else:
                    logger.info("macOS CoreLocation 不可用（%s）", payload.get("error"))
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            logger.info("macOS CoreLocation 定位失败（%s）", exc)

    return None


def _geolocate_ip(logger: logging.Logger, timeout: float) -> dict | None:
    try:
        payload = http_get_json(
            IP_API_URL, timeout, logger, retries=2, backoff=1.0
        )
    except MonitorError as exc:
        logger.info("IP 归属地查询失败：%s", exc)
        return None

    if payload.get("status") != "success":
        logger.info(
            "IP 归属地查询未成功：%s", payload.get("message") or payload.get("status")
        )
        return None

    extra = {
        key: payload.get(key)
        for key in ("country", "regionName", "city", "district", "isp", "query")
        if payload.get(key)
    }
    location = _normalize_location(payload, "ip", int(time.time()), extra)
    if location:
        logger.info(
            "IP 定位成功：%.5f, %.5f（%s，精度约公里级）",
            location["latitude"],
            location["longitude"],
            "".join(
                str(location.get(key) or "")
                for key in ("country", "regionName", "city", "district")
            )
            or "未知位置",
        )
    return location


def _to_api_coordinates(
    location: dict, cfg: dict, logger: logging.Logger | None = None
) -> dict:
    """把位置统一成彩云要求的 GCJ-02（中国大陆）。

    系统/IP 定位返回 WGS84，必须转换；config 坐标按 coordinate_system 解释。
    已标记为 GCJ-02 的缓存不会被重复转换。
    """
    result = dict(location)
    try:
        latitude = float(result["latitude"])
        longitude = float(result["longitude"])
    except (KeyError, TypeError, ValueError):
        return result

    source = result.get("source")
    system = str(result.get("coordinate_system") or "").lower()
    if not system:
        system = cfg.get("coordinate_system", "gcj02") if source == "config" else "wgs84"

    if system == "gcj02":
        result["coordinate_system"] = "gcj02"
        return result

    if geo.out_of_china(latitude, longitude):
        result["coordinate_system"] = "wgs84"
        return result

    gcj_latitude, gcj_longitude = geo.wgs84_to_gcj02(latitude, longitude)
    result["wgs84_latitude"] = round(latitude, 6)
    result["wgs84_longitude"] = round(longitude, 6)
    result["latitude"] = round(gcj_latitude, 6)
    result["longitude"] = round(gcj_longitude, 6)
    result["coordinate_system"] = "gcj02"
    if logger is not None:
        logger.debug(
            "坐标已由 WGS84 转为 GCJ-02：%.6f,%.6f -> %.6f,%.6f",
            latitude,
            longitude,
            gcj_latitude,
            gcj_longitude,
        )
    return result


def resolve_location(
    cfg: dict, state: dict, logger: logging.Logger
) -> dict:
    """按 显式配置 -> 系统定位 -> IP 兜底 的顺序解析位置。"""
    if cfg.get("latitude") is not None and cfg.get("longitude") is not None:
        location = {
            "latitude": round(float(cfg["latitude"]), 6),
            "longitude": round(float(cfg["longitude"]), 6),
            "source": "config",
            "coordinate_system": cfg.get("coordinate_system", "gcj02"),
            "fetched_at": int(time.time()),
        }
        logger.debug("使用 config.json 中配置的坐标")
        return _to_api_coordinates(location, cfg, logger)

    now = time.time()
    cached = _cached_location(state, cfg["location_cache_minutes"], now)
    if cached:
        logger.debug(
            "使用缓存位置（%s，来源 %s）", _fmt_time(cached.get("fetched_at")), cached.get("source")
        )
        return _to_api_coordinates(cached, cfg, logger)

    system_location = None
    if os.name == "nt":
        system_location = _geolocate_windows(logger)
    elif sys.platform == "darwin":
        system_location = _geolocate_macos(logger)

    if system_location:
        return _to_api_coordinates(system_location, cfg, logger)

    ip_location = _geolocate_ip(logger, cfg["request_timeout_seconds"])
    if ip_location:
        return _to_api_coordinates(ip_location, cfg, logger)

    raise MonitorError(
        "无法获取位置：config.json 未配置坐标，系统定位不可用，IP 归属地查询也失败。"
        "请在 config.json 中填写 latitude / longitude。"
    )


# --------------------------------------------------------------------------- #
# 彩云天气
# --------------------------------------------------------------------------- #
# 彩云降水单位：metric:v2 才返回 mm/h（metric 返回 0~1 的雷达强度）。
CAIYUN_UNIT = "metric:v2"

SKYCON_LABELS = {
    "CLEAR_DAY": "晴",
    "CLEAR_NIGHT": "晴",
    "PARTLY_CLOUDY_DAY": "多云",
    "PARTLY_CLOUDY_NIGHT": "多云",
    "CLOUDY": "阴",
    "LIGHT_HAZE": "轻度雾霾",
    "MODERATE_HAZE": "中度雾霾",
    "HEAVY_HAZE": "重度雾霾",
    "LIGHT_RAIN": "小雨",
    "MODERATE_RAIN": "中雨",
    "HEAVY_RAIN": "大雨",
    "STORM_RAIN": "暴雨",
    "FOG": "雾",
    "LIGHT_SNOW": "小雪",
    "MODERATE_SNOW": "中雪",
    "HEAVY_SNOW": "大雪",
    "STORM_SNOW": "暴雪",
    "DUST": "浮尘",
    "SAND": "沙尘",
    "WIND": "大风",
}


def _request_caiyun(
    cfg: dict, location: dict, endpoint: str, logger: logging.Logger
) -> dict:
    token = cfg["weather_token"]
    if not token:
        raise ConfigError("缺少 weather_token")

    latitude = location["latitude"]
    longitude = location["longitude"]
    # 注意：彩云 URL 路径参数顺序是 经度,纬度。
    url = (
        f"{cfg['api_base']}/v2.6/{token}/"
        f"{longitude:.6f},{latitude:.6f}/{endpoint}?unit={CAIYUN_UNIT}"
    )
    logger.debug("请求彩云 %s 接口（%.5f, %.5f）", endpoint, latitude, longitude)

    return http_get_json(
        url,
        cfg["request_timeout_seconds"],
        logger,
        secrets=(token,),
        retries=2,
        backoff=1.0,
    )


def fetch_precipitation(
    cfg: dict, location: dict, logger: logging.Logger
) -> dict:
    """用彩云 /weather 综合接口获取当前降水强度（mm/h）。

    /weather 一次请求即可返回 realtime；如果 token 有分钟级权限，
    同一响应里还会带 minutely。这样基础 token 不会因为“先试 minutely
    再试 realtime”而触发限流。
    """
    payload = _request_caiyun(cfg, location, "weather", logger)

    result = payload.get("result")
    if isinstance(result, dict) and not isinstance(result.get("minutely"), dict):
        logger.debug("彩云响应无 minutely 块（token 无分钟级权限），使用 realtime 实况")

    return parse_weather(payload)


def parse_weather(payload: dict) -> dict:
    """解析 /weather 综合响应：有 minutely 用 minutely，否则用 realtime。"""
    _check_caiyun_status(payload)

    result = payload.get("result")
    if not isinstance(result, dict):
        raise MonitorError("彩云接口返回缺少 result 字段")

    if isinstance(result.get("minutely"), dict):
        parsed = parse_minutely(payload)
        if parsed is not None:
            return parsed

    parsed = parse_realtime(payload)
    parsed["forecast"] = str(result.get("forecast_keypoint") or "").strip()
    return parsed


def _check_caiyun_status(payload: dict) -> None:
    status = payload.get("status")
    if status and status != "ok":
        raise MonitorError(f"彩云接口返回失败：{payload.get('error') or status}")


def parse_minutely(payload: dict) -> dict | None:
    """解析 minutely；token 无权限时彩云不返回 minutely 块，此时返回 None。"""
    _check_caiyun_status(payload)

    result = payload.get("result")
    if not isinstance(result, dict):
        raise MonitorError("彩云接口返回缺少 result 字段")

    minutely = result.get("minutely")
    if not isinstance(minutely, dict):
        return None

    minutely_status = minutely.get("status")
    if minutely_status and minutely_status != "ok":
        raise MonitorError(f"彩云分钟级数据状态异常：{minutely_status}")

    series = minutely.get("precipitation_2h")
    if not isinstance(series, list) or not series:
        series = minutely.get("precipitation")
    if not isinstance(series, list) or not series:
        raise MonitorError("彩云接口返回缺少 precipitation_2h 数据")

    first = series[0]
    if isinstance(first, dict):
        first = first.get("value", first.get("intensity"))

    try:
        intensity = float(first)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"无法解析第 1 分钟降水强度：{first!r}") from exc
    if not math.isfinite(intensity) or intensity < 0:
        intensity = 0.0

    description = str(
        minutely.get("description") or result.get("forecast_keypoint") or ""
    ).strip()

    return {
        "intensity": round(intensity, 3),
        "description": description,
        "description_label": "彩云预报",
        "datasource": str(minutely.get("datasource") or ""),
        "forecast_minutes": len(series),
        "provider": "minutely",
        "forecast": "",
    }


def parse_realtime(payload: dict) -> dict:
    """解析 realtime 实况降水，强度单位为 mm/h（请求时使用 metric:v2）。"""
    _check_caiyun_status(payload)

    result = payload.get("result")
    if not isinstance(result, dict):
        raise MonitorError("彩云接口返回缺少 result 字段")

    realtime = result.get("realtime")
    if not isinstance(realtime, dict):
        raise MonitorError("彩云接口返回缺少 result.realtime 字段")

    precipitation = realtime.get("precipitation")
    if not isinstance(precipitation, dict):
        precipitation = {}
    local = precipitation.get("local")
    if not isinstance(local, dict):
        local = {}

    raw_intensity = local.get("intensity")
    if raw_intensity is None:
        raw_intensity = precipitation.get("intensity")

    try:
        intensity = float(raw_intensity)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise MonitorError(
            f"彩云实况缺少可解析的降水强度：{raw_intensity!r}"
        ) from exc
    if not math.isfinite(intensity) or intensity < 0:
        intensity = 0.0

    nearest = precipitation.get("nearest")
    if not isinstance(nearest, dict):
        nearest = {}

    return {
        "intensity": round(intensity, 3),
        "description": _realtime_description(realtime, intensity, nearest),
        "description_label": "彩云实况",
        "datasource": str(local.get("datasource") or ""),
        "forecast_minutes": 0,
        "provider": "realtime",
        "skycon": str(realtime.get("skycon") or ""),
        "nearest_distance_m": _as_float(nearest.get("distance")),
        "nearest_intensity": _as_float(nearest.get("intensity")),
        "forecast": "",
    }


def _as_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _realtime_description(
    realtime: dict, intensity: float, nearest: dict
) -> str:
    parts: list[str] = []
    skycon = str(realtime.get("skycon") or "")
    if skycon:
        parts.append(f"实况 {SKYCON_LABELS.get(skycon, skycon)}")

    if intensity > 0:
        parts.append(f"本地降水 {intensity:.2f} mm/h")
    else:
        parts.append("本地无降水")

    # 只有本地没下雨时才提示最近的降水带，帮助判断“雨是不是就在隔壁”。
    distance = _as_float(nearest.get("distance"))
    if intensity <= 0 and distance is not None and 0 <= distance <= 20000:
        nearest_intensity = _as_float(nearest.get("intensity"))
        text = f"最近降水带约 {distance:.0f} 米外"
        if nearest_intensity is not None and nearest_intensity > 0:
            text += f"（{nearest_intensity:.2f} mm/h）"
        parts.append(text)

    return "；".join(parts)


# --------------------------------------------------------------------------- #
# 通知内容
# --------------------------------------------------------------------------- #
def _intensity_label(intensity: float) -> str:
    # 按彩云“分钟级降水量”对照表（mm/h）划分。
    if intensity < 0.08:
        return "无雨"
    if intensity < 3.44:
        return "小雨"
    if intensity < 11.33:
        return "中雨"
    if intensity < 51.30:
        return "大雨"
    return "暴雨"


def _format_place(location: dict) -> str:
    parts: list[str] = []
    for key in ("country", "regionName", "city", "district"):
        value = str(location.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    coords = f"{location['latitude']:.5f}, {location['longitude']:.5f}"
    label = SOURCE_LABELS.get(location.get("source", ""), str(location.get("source") or "未知来源"))
    coordinate_system = str(location.get("coordinate_system") or "").lower()
    if coordinate_system == "gcj02":
        label += "，GCJ-02"
    elif coordinate_system == "wgs84":
        label += "，WGS84"
    prefix = "·".join(parts)
    if prefix:
        return f"{prefix}（{coords}，{label}）"
    return f"{coords}（{label}）"


def build_message(
    event: str,
    intensity: float,
    description: str,
    location: dict,
    now: int,
    cfg: dict,
    description_label: str = "彩云预报",
    forecast: str = "",
) -> str:
    time_text = datetime.fromtimestamp(now).strftime("%m-%d %H:%M")

    if event == sm.EVENT_RAIN_START:
        title = '<span style="color:#e67e22;font-weight:bold;">开始下雨</span>'
        lines = [
            f"🌧️ {title}",
            f"当前强度：{intensity:.2f} mm/h（{_intensity_label(intensity)}）",
        ]
    else:
        title = '<span style="color:#27ae60;font-weight:bold;">雨停了</span>'
        window_minutes = int(cfg["interval_minutes"] * cfg["dry_confirm_count"])
        lines = [
            f"🌤️ {title}",
            f"已连续 {cfg['dry_confirm_count']} 次检测无雨（约 {window_minutes} 分钟）",
            f"最后强度：{intensity:.2f} mm/h",
        ]

    if description:
        lines.append(f"{description_label}：{description}")
    if forecast:
        lines.append(f"彩云预报：{forecast}")
    lines.append(f"位置：{_format_place(location)}")
    lines.append(f"时间：{time_text}")
    return "\n".join(lines)


def call_push_client(
    cfg: dict, content: str, logger: logging.Logger, timeout: float = 25.0
) -> tuple[bool, dict]:
    """以子进程调用 push_client.py，返回 (是否成功, 结果详情)。"""
    payload = {
        "content": content,
        "receiver": cfg["receiver"],
        "auth": cfg["auth"],
    }
    if cfg.get("sender"):
        payload["sender"] = cfg["sender"]

    command = [
        _console_python(),
        str(PUSH_CLIENT),
        "--json",
        "--url",
        cfg["push_url"],
        "--timeout",
        str(max(3.0, timeout - 15)),
    ]
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"

    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
            env=environment,
            creationflags=_subprocess_flags(),
        )
    except subprocess.TimeoutExpired:
        return False, {"error": f"推送子进程超时（>{timeout:.0f}s）"}
    except OSError as exc:
        return False, {"error": f"无法启动推送子进程：{exc}"}

    stdout = completed.stdout.decode("utf-8", "replace").strip()
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    result: dict = {}
    if stdout:
        try:
            parsed = json.loads(stdout.splitlines()[-1])
            if isinstance(parsed, dict):
                result = parsed
        except json.JSONDecodeError:
            result = {"raw_stdout": stdout[-500:]}

    if completed.returncode == 0 and result.get("ok"):
        return True, result

    error = (
        result.get("error")
        or stderr[-500:]
        or f"push_client.py 退出码 {completed.returncode}"
    )
    return False, {
        "error": error,
        "returncode": completed.returncode,
        "stdout": result or stdout[-500:],
    }


# --------------------------------------------------------------------------- #
# 单轮流程
# --------------------------------------------------------------------------- #
def run_cycle(
    cfg: dict,
    dry_run: bool,
    logger: logging.Logger,
    state_path: Path,
) -> int:
    try:
        state = sm.load_state(state_path)
    except json.JSONDecodeError as exc:
        backup = state_path.with_name(
            f"{state_path.stem}.corrupt-{int(time.time())}{state_path.suffix}"
        )
        try:
            state_path.replace(backup)
            logger.warning("状态文件损坏（%s），已备份为 %s 并重建", exc, backup.name)
        except OSError:
            logger.warning("状态文件损坏（%s），将直接重建", exc)
        state = sm.new_state()

    previous_state = copy.deepcopy(state)

    location = resolve_location(cfg, state, logger)
    weather = fetch_precipitation(cfg, location, logger)

    now = int(time.time())
    decision = sm.evaluate(
        state,
        weather["intensity"],
        now,
        threshold=cfg["rain_threshold"],
        dry_confirm=cfg["dry_confirm_count"],
        min_push_interval_minutes=cfg["min_push_interval_minutes"],
    )
    new_state = decision["state"]
    new_state["location"] = location
    new_state["last_description"] = weather["description"]
    new_state["last_forecast"] = weather.get("forecast", "")
    new_state["last_provider"] = weather["provider"]
    new_state["last_error"] = ""

    logger.info(
        "数据源 %s | 强度 %.3f mm/h | 状态 %s→%s | 事件 %s | %s",
        weather["provider"],
        weather["intensity"],
        sm.status_label(decision["previous_status"]),
        sm.status_label(new_state["status"]),
        decision["event"] or "无",
        _format_place(location),
    )
    if weather["description"]:
        logger.info("%s：%s", weather["description_label"], weather["description"])
    if weather.get("forecast"):
        logger.info("彩云预报：%s", weather["forecast"])

    exit_code = 0

    if decision["event"] and decision["push"]:
        content = build_message(
            decision["event"],
            weather["intensity"],
            weather["description"],
            location,
            now,
            cfg,
            description_label=weather["description_label"],
            forecast=weather.get("forecast", ""),
        )
        if dry_run:
            logger.info("[dry-run] 未推送，拟发送内容：\n%s", content)
        else:
            ok, detail = call_push_client(cfg, content, logger)
            if ok:
                logger.info("推送成功（%s）", decision["event"])
            else:
                exit_code = 1
                logger.error("推送失败：%s", detail.get("error"))
                new_state = sm.revert_transition(new_state, previous_state)
                new_state["location"] = location
                new_state["last_description"] = weather["description"]
                new_state["last_forecast"] = weather.get("forecast", "")
                new_state["last_provider"] = weather["provider"]
                new_state["last_error"] = str(detail.get("error") or "推送失败")[:500]
    elif decision["event"]:
        logger.info("状态已切换，但按防抖规则不推送：%s", decision["reason"])

    if dry_run:
        logger.info("[dry-run] 不写入状态文件")
    else:
        sm.save_state(state_path, new_state)

    return exit_code


def run_once_locked(
    cfg: dict,
    dry_run: bool,
    logger: logging.Logger,
    state_path: Path,
    lock_path: Path,
) -> int:
    stale_seconds = max(900, int(cfg["interval_minutes"] * 60 * 2))
    with InstanceLock(lock_path, stale_seconds=stale_seconds) as acquired:
        if not acquired:
            logger.warning("上一轮仍在执行（锁文件 %s），跳过本轮", lock_path.name)
            return 0
        return run_cycle(cfg, dry_run, logger, state_path)


def run_loop(
    cfg: dict,
    dry_run: bool,
    logger: logging.Logger,
    state_path: Path,
    lock_path: Path,
) -> int:
    interval = max(60, int(cfg["interval_minutes"] * 60))
    logger.info("循环模式启动，每 %d 分钟执行一轮", cfg["interval_minutes"])
    try:
        while True:
            started = time.monotonic()
            try:
                run_once_locked(cfg, dry_run, logger, state_path, lock_path)
            except (ConfigError, MonitorError) as exc:
                logger.error("%s", exc)
            except Exception:  # noqa: BLE001 - 常驻进程不能因单次异常退出
                logger.exception("本轮执行出现未预期异常")

            elapsed = time.monotonic() - started
            wait_seconds = max(5.0, interval - elapsed)
            logger.info("下一轮在 %d 秒后", int(wait_seconds))
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        logger.info("收到中断信号，退出循环模式")
    return 0


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def print_status(cfg: dict, state_path: Path) -> int:
    try:
        state = sm.load_state(state_path)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"状态文件无法解析：{exc}")
        return 1

    location = state.get("location") if isinstance(state.get("location"), dict) else None
    intensity = state.get("last_intensity")
    intensity_text = "（暂无）" if intensity is None else f"{intensity} mm/h"

    def _trim(number: object) -> object:
        value = float(number)  # type: ignore[arg-type]
        return int(value) if value.is_integer() else value

    lines = [
        f"状态：{sm.status_label(state['status'])}（{state['status']}）",
        f"连续无雨计数：{state['dry_counter']} / {cfg['dry_confirm_count']}",
        f"上次推送：{_fmt_time(state.get('last_event_time'))}",
        f"最近强度：{intensity_text}",
        f"最近更新：{_fmt_time(state.get('last_update_time'))}",
        f"最近描述：{state.get('last_description') or '（无）'}",
    ]
    if location:
        lines.append(f"最近位置：{_format_place(location)}")
    if state.get("last_provider"):
        lines.append(f"最近数据源：{state['last_provider']}")
    if state.get("last_forecast"):
        lines.append(f"最近预报：{state['last_forecast']}")

    missing = []
    if not cfg.get("weather_token"):
        missing.append("weather_token（彩云 token）")
    if not cfg.get("receiver"):
        missing.append("receiver（小鲁班 uid）")
    if not cfg.get("auth"):
        missing.append("auth（小鲁班 token）")
    if missing:
        lines.append("待填写：" + "、".join(missing))
    else:
        lines.append("凭据：已齐备")
    if cfg.get("latitude") is None:
        lines.append("坐标：未配置，将使用自动定位（建议填 latitude / longitude）")

    lines.extend(
        [
            "",
            "配置摘要：",
            f"  坐标：{cfg.get('latitude')}, {cfg.get('longitude')}"
            if cfg.get("latitude") is not None
            else "  坐标：未配置（自动定位）",
            f"  彩云 token：{mask_secret(cfg.get('weather_token'))}",
            f"  小鲁班 receiver：{mask_secret(cfg.get('receiver'))}",
            f"  小鲁班 auth：{mask_secret(cfg.get('auth'))}",
            f"  周期：{_trim(cfg['interval_minutes'])} 分钟；雨停确认：{cfg['dry_confirm_count']} 次",
            f"  开始通知冷却：{_trim(cfg['min_push_interval_minutes'])} 分钟",
        ]
    )
    if state.get("last_error"):
        lines.append(f"最近错误：{state['last_error']}")
    print("\n".join(lines))
    return 0


def test_push(cfg: dict, logger: logging.Logger) -> int:
    now = int(time.time())
    content = (
        '🧪 <span style="color:#3498db;font-weight:bold;">降雨监控推送测试</span>\n'
        f"时间：{datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}\n"
        "收到这条消息说明小鲁班推送链路正常。"
    )
    ok, detail = call_push_client(cfg, content, logger)
    if ok:
        logger.info("测试消息发送成功")
        print("测试消息发送成功")
        return 0
    logger.error("测试消息发送失败：%s", detail.get("error"))
    print(f"测试消息发送失败：{detail.get('error')}")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="街道级即时降雨监控：彩云 minutely + 小鲁班推送",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="只执行一轮（默认，计划任务使用）")
    mode.add_argument("--loop", action="store_true", help="常驻循环执行")
    parser.add_argument("--status", action="store_true", help="打印当前状态并退出（不联网）")
    parser.add_argument("--test-push", action="store_true", help="发送一条小鲁班测试消息")
    parser.add_argument(
        "--dry-run", action="store_true", help="只拉取并评估，不推送、不写状态文件"
    )
    parser.add_argument("--config", type=Path, help=f"配置文件路径（默认 {CONFIG_PATH}）")
    parser.add_argument("--state", type=Path, help=f"状态文件路径（默认 {STATE_PATH}）")
    parser.add_argument("--log-dir", type=Path, help=f"日志目录（默认 {LOG_DIR}）")
    parser.add_argument("--api-base", help="覆盖彩云 API 根地址（调试用）")
    parser.add_argument("--push-url", help="覆盖小鲁班推送地址（调试用）")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台，只写日志文件")
    parser.add_argument("--version", action="version", version="rain-monitor 1.0")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_std_streams()
    args = _build_parser().parse_args(argv)

    log_dir = args.log_dir if args.log_dir else LOG_DIR
    logger = setup_logging(
        "DEBUG" if args.verbose else "INFO", Path(log_dir), console=not args.quiet
    )

    config_path = args.config if args.config else CONFIG_PATH
    state_path = args.state if args.state else STATE_PATH

    try:
        cfg = load_config(Path(config_path), logger)
        if args.api_base:
            cfg["api_base"] = args.api_base
        if args.push_url:
            cfg["push_url"] = args.push_url
        cfg = validate_config(
            cfg,
            # --dry-run 不推送，因此不需要小鲁班凭据；--status 两者都不需要。
            require_credentials=not (args.status or args.dry_run),
            require_weather_token=not (args.status or args.test_push),
        )
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

    if args.verbose:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)
    elif cfg["log_level"] != "INFO":
        logger.setLevel(getattr(logging, cfg["log_level"], logging.INFO))
        for handler in logger.handlers:
            handler.setLevel(getattr(logging, cfg["log_level"], logging.INFO))

    if args.status:
        return print_status(cfg, state_path)

    if args.test_push:
        return test_push(cfg, logger)

    lock_path = Path(log_dir) / ".monitor.lock"

    if args.loop:
        return run_loop(cfg, args.dry_run, logger, state_path, lock_path)

    try:
        return run_once_locked(cfg, args.dry_run, logger, state_path, lock_path)
    except (ConfigError, MonitorError) as exc:
        logger.error("%s", exc)
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("本轮执行出现未预期异常")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
