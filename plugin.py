"""
天气查询插件。

功能：
1. 群里或私聊发「天气 北京」查询实时天气，只发「天气」时查配置里的默认城市；
2. 支持「北京天气」「今天北京天气怎么样」这几种写法，触发词可在配置里改；
3. 默认带未来 3 天预报，天数可调，-1 表示只显示实时天气；
4. 温度单位、生效范围、同一会话冷却都由安装配置控制。

约定：
- 城市解析走 geocoding-api.open-meteo.com，天气数据走 api.open-meteo.com，都不需要 API Key；
- 出网一律走 host.http_request，域名受清单白名单约束；
- 任何失败都回一条中文提示，不向宿主抛裸异常，避免整条事件判定为失败；
- 不直接调用 OneBot，发消息一律通过 host 完成。
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from app.services.plugin_sdk import PluginCallError, on_message


DEFAULT_TRIGGERS = "天气"
DEFAULT_FORECAST_DAYS = 3
DEFAULT_COOLDOWN = 20

MAX_TRIGGER_COUNT = 10
MAX_FORECAST_DAYS = 7
MAX_COOLDOWN = 300
MAX_CITY_LENGTH = 30

REQUEST_TIMEOUT = 10.0
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO 天气代码，Open-Meteo 的 weather_code 就是这套编码。
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

# 口语里常跟在城市前面的时间/语气词，取城市名时先剥掉，避免「今天北京」查不到。
CITY_PREFIXES = (
    "今天",
    "明天",
    "后天",
    "现在",
    "当前",
    "今日",
    "请问",
    "帮我查一下",
    "帮我查",
    "查一下",
    "查查",
    "看一下",
    "看看",
    "我想知道",
)

# 会话级冷却：同一会话在冷却时间内查同一个城市会被忽略，避免刷屏。
_last_reply: dict[str, tuple[float, str]] = {}


def _text_config(host: Any, key: str, default: str) -> str:
    """读取文本型配置，缺失时返回默认值。"""
    value = host.config.get(key)
    text = str(value).strip() if value is not None else ""
    return text or default


def _choice(host: Any, key: str, default: str) -> str:
    """读取下拉型配置，空值按默认项处理。"""
    return _text_config(host, key, default)


def _int_config(host: Any, key: str, default: int, low: int, high: int) -> int:
    """读取数字型配置，空值取默认值，非法或越界时夹到允许范围内。"""
    raw = host.config.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


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


def _message_text(event: dict[str, Any]) -> str:
    """从事件里取出纯文本，数组消息只拼接 text 段。"""
    message = event.get("message")
    if isinstance(message, str):
        text = message
    elif isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                data = segment.get("data") or {}
                parts.append(str(data.get("text") or ""))
        text = "".join(parts)
    else:
        text = str(event.get("raw_message") or "")
    # NapCat 在 @ 之后可能插入特殊空格，统一成普通空格再处理。
    return text.replace("\u2005", " ").replace("\xa0", " ").strip()


def _parse_triggers(raw: str) -> list[str]:
    """把配置里的触发词拆开，兼容中英文逗号与分号，长的优先匹配。"""
    normalized = raw.replace("，", ",").replace("；", ",").replace(";", ",")
    words = [item.strip() for item in normalized.split(",") if item.strip()]
    if not words:
        words = [DEFAULT_TRIGGERS]
    # 「天气查询」要排在「天气」前面，否则永远匹配到短的那个。
    words.sort(key=len, reverse=True)
    return words[:MAX_TRIGGER_COUNT]


def _clean_city(raw: str) -> str:
    """清掉城市名两侧的标点与空白，并限制长度。"""
    text = raw.strip(" \t\r\n。．，,、；;：:！!？?~～\"'“”（）()")
    return text[:MAX_CITY_LENGTH]


def _strip_prefixes(text: str) -> str:
    """反复剥掉「今天」「帮我查」这类前缀，剩下的当城市名。"""
    current = text
    changed = True
    while changed and current:
        changed = False
        for prefix in CITY_PREFIXES:
            if current.startswith(prefix) and len(current) > len(prefix):
                current = current[len(prefix):]
                changed = True
                break
    return _clean_city(current)


def _split_city(text: str, triggers: list[str]) -> str | None:
    """
    从消息里取出城市名。

    Returns:
        城市名；返回空串表示只发了触发词（查默认城市）；None 表示这条消息不是查询天气。
    """
    clean = text.strip(" \t\r\n。．，,！!？?~～")
    for word in triggers:
        if clean == word:
            return ""
        if clean.startswith(word):
            return _strip_prefixes(_clean_city(clean[len(word):]))
        if clean.endswith(word):
            return _strip_prefixes(_clean_city(clean[: -len(word)]))
    # 「今天北京天气怎么样」这类口语：触发词夹在中间，取它前面那截。
    for word in triggers:
        index = clean.find(word)
        if index > 0:
            candidate = _strip_prefixes(_clean_city(clean[:index]))
            if candidate:
                return candidate
    return None


def _chat_key(host: Any, event: dict[str, Any]) -> str:
    """会话标识：群聊按群号，私聊按用户号。"""
    chat_id = (
        event.get("group_id")
        if event.get("message_type") == "group"
        else event.get("user_id")
    )
    return f"{host.bot_qq}:{chat_id}"


def _duplicated(host: Any, event: dict[str, Any], city: str, seconds: int) -> bool:
    """同一会话在冷却时间内重复查同一个城市时直接忽略。"""
    if seconds <= 0:
        return False
    key = _chat_key(host, event)
    now = time.time()
    last = _last_reply.get(key)
    if last and now - last[0] < seconds and last[1] == city:
        return True
    _last_reply[key] = (now, city)
    return False


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


def _as_text(value: str) -> dict[str, Any]:
    """构造文本消息段。"""
    return {"type": "text", "data": {"text": value}}


async def _reply(host: Any, event: dict[str, Any], message: Any) -> None:
    """按会话类型把消息发回去。"""
    if event.get("message_type") == "group":
        await host.send_group_msg(event.get("group_id"), message)
    else:
        await host.send_private_msg(event.get("user_id"), message)


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


@on_message
async def handle_weather(event: dict[str, Any], host: Any) -> None:
    """收到消息时判断是不是查天气，命中就回一条实时天气加预报。"""
    if event.get("post_type") != "message":
        return
    if str(event.get("user_id")) == str(event.get("self_id")):
        # 机器人自己发的消息不处理，避免自我循环。
        return

    scope = _choice(host, "reply_scope", "群聊与私聊")
    is_group = event.get("message_type") == "group"
    if (scope == "仅群聊" and not is_group) or (scope == "仅私聊" and is_group):
        return

    text = _message_text(event)
    if not text:
        return

    triggers = _parse_triggers(_text_config(host, "trigger_words", DEFAULT_TRIGGERS))
    city = _split_city(text, triggers)
    if city is None:
        return

    if not host.has("msg.send"):
        host.log("缺少 msg.send 能力，无法回复", "error")
        return
    if not host.has("http.outbound"):
        host.log("缺少 http.outbound 能力，无法查询天气", "error")
        await _reply(host, event, "本机器人未获批「访问外网」能力，天气查询不可用。")
        return

    if not city:
        city = _clean_city(_text_config(host, "default_city", ""))
        if not city:
            await _reply(
                host,
                event,
                f"用法：{triggers[0]} 北京，或先在插件配置里填写默认城市。",
            )
            return

    cooldown = _int_config(host, "cooldown_seconds", DEFAULT_COOLDOWN, 0, MAX_COOLDOWN)
    if _duplicated(host, event, city, cooldown):
        host.log(f"同一城市在冷却中，忽略：{city}")
        return

    unit = _choice(host, "temperature_unit", "摄氏度")
    days = _forecast_days(host)
    try:
        place = await _geocode(host, city)
        if place is None:
            await _reply(
                host,
                event,
                f"没有找到「{city}」这个城市，换个写法试试，比如「{triggers[0]} 北京」。",
            )
            return
        payload = await _fetch_weather(host, place, days, unit)
        if payload is None:
            await _reply(host, event, "天气服务返回异常，请稍后再试。")
            return
        await _reply(host, event, _as_text(_format_report(place, payload, days, unit)))
        host.log(f"已回复天气：{place['label']}")
    except PluginCallError as error:
        host.log(f"出网请求被拒绝或失败：{error}", "error")
        await _reply(host, event, "天气服务暂时不可用，请稍后再试。")
    except Exception as error:  # 插件内部兜底，不让异常冒到宿主
        host.log(f"天气插件异常：{type(error).__name__}: {error}", "error")
        await _reply(host, event, "查询天气时出错了，请稍后再试。")
