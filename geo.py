"""坐标系转换：WGS84 -> GCJ-02（中国大陆）。

Windows/macOS 原生定位和 IP 归属地返回的都是 WGS84；彩云天气在中国大陆
要求 GCJ-02（高德/腾讯地图坐标系）。两者在杭州一带相差约 500 米，
对“街道级”判断不可忽略，因此请求彩云前需要转换。

算法为公开的 GCJ-02 偏移算法（俗称“火星坐标”转换），使用常见的中国大陆
经纬度范围做粗略判断；香港/澳门/台湾等边境地区可能被包含在范围内，
如坐标有偏差，请直接用 config.json 指定坐标与坐标系。
"""

from __future__ import annotations

import math

_A = 6378245.0
_EE = 0.00669342162296594323


def out_of_china(latitude: float, longitude: float) -> bool:
    """粗略判断是否在中国大陆范围外（范围外不做偏移）。"""
    return not (
        72.004 <= longitude <= 137.8347
        and 0.8293 <= latitude <= 55.8271
    )


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
    ret += 0.2 * math.sqrt(abs(x))
    ret += (
        20.0 * math.sin(6.0 * x * math.pi)
        + 20.0 * math.sin(2.0 * x * math.pi)
    ) * 2.0 / 3.0
    ret += (
        20.0 * math.sin(y * math.pi)
        + 40.0 * math.sin(y / 3.0 * math.pi)
    ) * 2.0 / 3.0
    ret += (
        160.0 * math.sin(y / 12.0 * math.pi)
        + 320.0 * math.sin(y * math.pi / 30.0)
    ) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
    ret += 0.1 * math.sqrt(abs(x))
    ret += (
        20.0 * math.sin(6.0 * x * math.pi)
        + 20.0 * math.sin(2.0 * x * math.pi)
    ) * 2.0 / 3.0
    ret += (
        20.0 * math.sin(x * math.pi)
        + 40.0 * math.sin(x / 3.0 * math.pi)
    ) * 2.0 / 3.0
    ret += (
        150.0 * math.sin(x / 12.0 * math.pi)
        + 300.0 * math.sin(x / 30.0 * math.pi)
    ) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(
    latitude: float, longitude: float
) -> tuple[float, float]:
    """把 WGS84 坐标转换为 GCJ-02；中国大陆范围外原样返回。"""
    latitude = float(latitude)
    longitude = float(longitude)
    if out_of_china(latitude, longitude):
        return latitude, longitude

    delta_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    delta_lon = _transform_lon(longitude - 105.0, latitude - 35.0)

    rad_lat = latitude / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)

    delta_lat = (
        delta_lat * 180.0
        / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    )
    delta_lon = (
        delta_lon * 180.0
        / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    )
    return latitude + delta_lat, longitude + delta_lon
