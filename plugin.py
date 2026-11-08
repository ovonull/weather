"""天气查询：只给 AI 提供工具，不自己听消息、不自己发消息。

模型在对话里问天气时调用 ``weather_query``，插件返回整理好的中文报告，由 AI
按人设组织回复。清单里声明了 ``keywords``：消息里出现「天气」「气温」这类词时，
宿主会把这一轮强制交给 AI 并点名调这个工具——和视频解析靠 ``link_hosts`` 触发
是同一套机制，不需要用户带触发词格式，也不经过接话评分。

配置项：默认城市（用户没说城市时用）、预报天数、温度单位。出网走
host.http_request，城市解析用 geocoding-api.open-meteo.com，天气用
api.open-meteo.com，都不需要 Key。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.plugin_sdk import PluginCallError, on_tool

DEFAULT_FORECAST_DAYS = 3

MAX_FORECAST_DAYS = 7
MAX_CITY_LENGTH = 30

REQUEST_TIMEOUT = 10.0
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_TEXT = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴大冰雹",
}

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

def _text_config(host: Any, key: str, default: str) -> str:
    """读取文本型配置，缺失时返回默认值。"""
    value = host.config.get(key)
    text = str(value).strip() if value is not None else ""
    return text or default


def _choice(host: Any, key: str, default: str) -> str:
    """读取下拉型配置，空值按默认项处理。"""
    return _text_config(host, key, default)

def _forecast_days(host: Any) -> int:
    """
    读取预报天数。

    宿主给数字型配置项的初始值是 0，对「天数」来说 0 没有意义，所以这里把
    0 与空值都按「没填」处理并取默认 3 天，只显示实时天气用 -1 表示。
    """
    raw = host.config.get("forecast_days")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_FORECAST_DAYS
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_FORECAST_DAYS
    if value < 0:
        return 0
    if value == 0:
        return DEFAULT_FORECAST_DAYS
    return min(value, MAX_FORECAST_DAYS)

def _clean_city(raw: str) -> str:
    """清掉城市名两侧的标点与空白，并限制长度。"""
    text = raw.strip(" \t\r\n。．，,、；;：:！!？?~～\"'“”（）()")
    return text[:MAX_CITY_LENGTH]

def _unit_symbol(unit: str) -> str:
    """温度单位符号。"""
    return "℉" if unit == "华氏度" else "℃"


def _temperature(value: Any, unit: str) -> str:
    """把接口返回的温度格式化成带单位的整数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{round(number)}{_unit_symbol(unit)}"


def _weather_text(code: Any) -> str:
    """把天气代码翻成中文，未知代码回退成「未知天气」。"""
    try:
        return WEATHER_TEXT.get(int(code), "未知天气")
    except (TypeError, ValueError):
        return "未知天气"


def _value_at(values: Any, index: int) -> Any:
    """安全取数组里的第几个值，越界返回 None。"""
    if isinstance(values, list) and 0 <= index < len(values):
        return values[index]
    return None

async def _geocode(host: Any, city: str) -> dict[str, Any] | None:
    """
    把城市名解析成经纬度。

    Args:
        host: 插件宿主视图。
        city: 用户输入的城市名。
    Returns:
        含 label、latitude、longitude 的地点信息，找不到时返回 None。
    """
    response = await host.http_request(
        "GET",
        GEOCODE_URL,
        query={"name": city, "count": 1, "language": "zh", "format": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    if int(response.get("status") or 0) != 200:
        host.log(f"城市解析接口返回 {response.get('status')}", "warning")
        return None
    payload = response.get("json") or {}
    results = payload.get("results") or []
    if not results:
        return None
    place = results[0]
    name = str(place.get("name") or city)
    region = str(place.get("admin1") or "")
    country = str(place.get("country") or "")
    label = name
    # 「上海」与「上海市」这种只是行政后缀差异，不用重复显示。
    if region and region != name and not region.startswith(name):
        label = f"{name}（{region}）"
    elif country and country not in {"中国", "China"}:
        label = f"{name}（{country}）"
    return {
        "label": label,
        "latitude": place.get("latitude"),
        "longitude": place.get("longitude"),
    }


async def _fetch_weather(
    host: Any, place: dict[str, Any], days: int, unit: str
) -> dict[str, Any] | None:
    """
    拉取实时天气与预报。

    Args:
        host: 插件宿主视图。
        place: 城市解析结果。
        days: 预报天数，0 表示只要实时天气。
        unit: 温度单位，华氏度时让接口直接返回华氏度。
    Returns:
        接口原始 JSON，失败时返回 None。
    """
    query: dict[str, Any] = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "current": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "weather_code",
                "wind_speed_10m",
            ]
        ),
        "timezone": "auto",
    }
    if days > 0:
        query["daily"] = ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
            ]
        )
        query["forecast_days"] = days
    if unit == "华氏度":
        query["temperature_unit"] = "fahrenheit"
    response = await host.http_request(
        "GET", FORECAST_URL, query=query, timeout=REQUEST_TIMEOUT
    )
    if int(response.get("status") or 0) != 200:
        host.log(f"天气接口返回 {response.get('status')}", "warning")
        return None
    payload = response.get("json")
    return payload if isinstance(payload, dict) else None


def _daily_line(daily: dict[str, Any], index: int, day_text: str, unit: str) -> str:
    """拼一行某天的预报。"""
    try:
        weekday = WEEKDAYS[date.fromisoformat(str(day_text)).weekday()]
    except ValueError:
        weekday = ""
    desc = _weather_text(_value_at(daily.get("weather_code"), index))
    high = _temperature(_value_at(daily.get("temperature_2m_max"), index), unit)
    low = _temperature(_value_at(daily.get("temperature_2m_min"), index), unit)
    line = f"{str(day_text)[5:]} {weekday} {desc} {high}/{low}"
    rain = _value_at(daily.get("precipitation_probability_max"), index)
    if rain is not None:
        try:
            line += f" 降水 {round(float(rain))}%"
        except (TypeError, ValueError):
            pass
    return line


def _format_report(
    place: dict[str, Any], payload: dict[str, Any], days: int, unit: str
) -> str:
    """把接口数据拼成一条适合群聊阅读的纯文本。"""
    current = payload.get("current") or {}
    lines = [f"{place['label']} 实时天气"]
    lines.append(
        f"{_weather_text(current.get('weather_code'))} "
        f"{_temperature(current.get('temperature_2m'), unit)}"
    )
    extras: list[str] = []
    feels = current.get("apparent_temperature")
    if feels is not None:
        extras.append(f"体感 {_temperature(feels, unit)}")
    humidity = current.get("relative_humidity_2m")
    if humidity is not None:
        try:
            extras.append(f"湿度 {round(float(humidity))}%")
        except (TypeError, ValueError):
            pass
    wind = current.get("wind_speed_10m")
    if wind is not None:
        try:
            extras.append(f"风 {round(float(wind))} km/h")
        except (TypeError, ValueError):
            pass
    if extras:
        lines.append(" · ".join(extras))

    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    if days > 0 and times:
        lines.append("")
        lines.append(f"未来 {min(len(times), days)} 天")
        for index, day_text in enumerate(times[:days]):
            lines.append(_daily_line(daily, index, str(day_text), unit))
    return "\n".join(lines)

@on_tool("weather_query")
async def weather_query(arguments: dict, host: Any) -> str:
    """AI 工具：模型直接调用查询天气，返回报告文本（不自己发消息）。

    与事件触发共用同一套取数逻辑，但两点不同：
    1. 没有会话冷却——消息触发有冷却防刷屏，模型调用没有刷屏问题，冷掉反而让
       模型拿不到最新天气，所以工具入口不检查 _duplicated；
    2. 返回文本而不是发消息——模型拿到报告后自己组织回复，避免「工具发一条 +
       模型再回一条」的重复。

    Args:
        arguments: 模型给的入参，含 ``city``（必填）与可选 ``days``。
        host: 本插件的宿主视图。
    Returns:
        中文天气报告文本；任何失败都返回可读的错误文本，不抛裸异常，
        让模型知道发生了什么而不是拿到一串异常栈。
    """
    city = _clean_city(str(arguments.get("city") or ""))
    if not city:
        return "缺少城市名：请提供 city 参数，例如「北京」「上海」。"
    if not host.has("http.outbound"):
        return "当前机器人未获批「访问外网」能力，天气查询不可用。"

    # days 参数语义与配置一致：-1 只显示实时；0 或不传按插件配置（通常 3 天）；
    # 正数取 1~7 天。非法值按没传处理，不因一个坏参数让整次查询失败。
    days = _forecast_days(host)
    raw_days = arguments.get("days")
    if raw_days is not None:
        try:
            requested = int(float(str(raw_days)))
        except (TypeError, ValueError):
            requested = 0
        if requested < 0:
            days = 0
        elif requested > 0:
            days = min(requested, MAX_FORECAST_DAYS)

    unit = _choice(host, "temperature_unit", "摄氏度")
    try:
        place = await _geocode(host, city)
        if place is None:
            return f"没有找到「{city}」这个城市，请换个写法，例如「北京」「上海」。"
        payload = await _fetch_weather(host, place, days, unit)
        if payload is None:
            return "天气服务返回异常，请稍后再试。"
        report = _format_report(place, payload, days, unit)
        host.log(f"AI 工具已返回天气：{place['label']}")
        return report
    except PluginCallError as error:
        host.log(f"AI 工具出网请求被拒绝或失败：{error}", "error")
        return "天气服务暂时不可用，请稍后再试。"
    except Exception as error:  # 插件内部兜底，不让异常冒到宿主
        host.log(f"AI 工具异常：{type(error).__name__}: {error}", "error")
        return "查询天气时出错了，请稍后再试。"
