"""服务器查询模块。

提供 MC 服务器状态拉取、motd 提取、延迟历史构建等函数。
静默轮询逻辑因依赖 self._store_lock / self._save_store 等状态，保留在 main.py 中。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mcstatus import JavaServer

from . import cache as _cache_mod
from . import store as _store_mod

MOTD_FORMAT_CODE_PATTERN = re.compile(r"§.")
MAX_PLAYER_SAMPLES = 50
MAX_PLAYER_NAME_LENGTH = 64
MAX_VERSION_LENGTH = 128
MAX_LATENCY_MS = 60_000
MAX_PLAYER_COUNT = 10_000_000
MAX_FAVICON_TEXT_LENGTH = 400_000
MAX_MOTD_NODES = 1_024
MAX_MOTD_FLATTENED_LENGTH = 4_096


class McServerConnectionError(RuntimeError):
    """MC server lookup/status request failed."""


class McServerTimeoutError(McServerConnectionError):
    """MC server status request timed out."""


class McServerInvalidAddressError(McServerConnectionError):
    """Server address is malformed or targets a non-public network."""


@dataclass
class ServerStatus:
    """标准化后的服务器状态结构。"""

    address: str
    latency: int
    version: str
    players_online: int
    players_max: int
    icon_base64: str | None
    players: list[dict[str, str]]
    motd: str


# ---- 服务端查询 ----


async def lookup_public_server(address: str) -> JavaServer:
    """Resolve a Java server and pin the connection to a public IP."""
    try:
        fallback_host, fallback_port = _store_mod.parse_server_address(address)
        server = await JavaServer.async_lookup(address)
    except _store_mod.InvalidServerAddressError as exc:
        raise McServerInvalidAddressError(str(exc)) from exc
    except ValueError as exc:
        raise McServerInvalidAddressError("server address is invalid") from exc

    server_address = getattr(server, "address", None)
    target_host = str(getattr(server_address, "host", fallback_host) or "")
    try:
        target_port = int(getattr(server_address, "port", fallback_port))
        public_ip, public_port = await _store_mod.resolve_public_server_target(
            target_host,
            target_port,
        )
    except (_store_mod.InvalidServerAddressError, TypeError, ValueError) as exc:
        raise McServerInvalidAddressError(str(exc)) from exc

    try:
        return JavaServer(
            public_ip,
            public_port,
            timeout=float(getattr(server, "timeout", 3)),
        )
    except (TypeError, ValueError) as exc:
        raise McServerInvalidAddressError("server address is invalid") from exc


async def fetch_server_status(
    address: str,
    *,
    need_players: bool,
    status_timeout: int,
) -> ServerStatus:
    """Fetch and normalize status within one end-to-end timeout budget."""
    timeout = max(float(status_timeout), 0.001)
    try:
        return await asyncio.wait_for(
            _fetch_server_status_once(
                address,
                need_players=need_players,
                status_timeout=timeout,
            ),
            timeout=timeout,
        )
    except McServerInvalidAddressError:
        raise
    except McServerTimeoutError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise McServerTimeoutError("server query timed out") from exc


async def _fetch_server_status_once(
    address: str,
    *,
    need_players: bool,
    status_timeout: float,
) -> ServerStatus:
    """Perform one server query; the caller owns the total timeout."""
    try:
        server = await lookup_public_server(address)
    except McServerInvalidAddressError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise McServerTimeoutError("server lookup timed out") from exc
    except OSError as exc:
        raise McServerConnectionError("server lookup failed") from exc
    except Exception as exc:
        raise McServerConnectionError("server lookup failed") from exc

    try:
        status = await asyncio.wait_for(server.async_status(), timeout=status_timeout)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise McServerTimeoutError("server status timed out") from exc
    except (OSError, ConnectionError) as exc:
        raise McServerConnectionError("server status failed") from exc
    except Exception as exc:
        raise McServerConnectionError("server status failed") from exc

    icon_base64 = None
    # mcstatus 14.x 将 favicon 改名为 icon；兼容新旧版本
    favicon = getattr(status, "favicon", None) or getattr(status, "icon", None)
    if favicon:
        candidate_icon = str(favicon)
        if len(candidate_icon) <= MAX_FAVICON_TEXT_LENGTH:
            icon_base64 = candidate_icon

    players: list[dict[str, str]] = []
    player_status = getattr(status, "players", None)
    if need_players:
        sample_players = getattr(player_status, "sample", None) or []
        if not isinstance(sample_players, (list, tuple)):
            sample_players = []
        for player in sample_players[:MAX_PLAYER_SAMPLES]:
            player_name = str(getattr(player, "name", "") or "")[
                :MAX_PLAYER_NAME_LENGTH
            ]
            player_uid = str(getattr(player, "id", "") or "")[:64]
            if not player_name:
                continue
            player_uid = _cache_mod.normalize_player_uid(player_uid, player_name)
            players.append({"name": player_name, "uid": player_uid})

    latency = _safe_nonnegative_int(
        getattr(status, "latency", 0),
        max_value=MAX_LATENCY_MS,
    )
    # async_ping() 测量轻量协议往返，比 async_status() 更接近游戏内显示
    try:
        ping_latency = await asyncio.wait_for(
            server.async_ping(), timeout=min(5, status_timeout)
        )
        ping_value = _safe_nonnegative_int(
            ping_latency,
            max_value=MAX_LATENCY_MS,
        )
        if ping_value > 0:
            latency = ping_value
    except Exception:
        pass
    version_status = getattr(status, "version", None)
    version = str(
        getattr(version_status, "name", "Unknown") if version_status else "Unknown"
    )[:MAX_VERSION_LENGTH]
    motd = extract_motd_text(getattr(status, "description", None))
    return ServerStatus(
        address=address,
        latency=max(latency, 0),
        version=version,
        players_online=_safe_nonnegative_int(
            getattr(player_status, "online", 0),
            max_value=MAX_PLAYER_COUNT,
        ),
        players_max=_safe_nonnegative_int(
            getattr(player_status, "max", 0),
            max_value=MAX_PLAYER_COUNT,
        ),
        icon_base64=icon_base64,
        players=players,
        motd=motd,
    )


def _safe_nonnegative_int(value: Any, *, max_value: int) -> int:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(numeric):
        return 0
    return min(max(int(round(numeric)), 0), max_value)


# ---- Motd 提取 ----


def extract_motd_text(description: Any) -> str:
    """提取并归一化服务端 Motd。"""
    if description is None:
        return ""
    try:
        to_plain = getattr(description, "to_plain", None)
        if callable(to_plain):
            text = strip_minecraft_format_codes(
                str(to_plain() or "")[:MAX_MOTD_FLATTENED_LENGTH]
            ).strip()
            if text:
                return text[:300]
    except Exception:
        pass
    text = strip_minecraft_format_codes(flatten_motd_node(description)).strip()
    return text[:300]


def strip_minecraft_format_codes(text: str) -> str:
    """移除 Minecraft Motd 文本中的格式控制码（§x）。"""
    if not text:
        return ""
    cleaned = MOTD_FORMAT_CODE_PATTERN.sub("", text)
    return cleaned.replace("§", "")


def flatten_motd_node(node: Any) -> str:
    """Flatten an untrusted MOTD tree within fixed work and output budgets."""
    stack: list[Any] = [node]
    parts: list[str] = []
    output_length = 0
    visited_nodes = 0

    while stack and visited_nodes < MAX_MOTD_NODES:
        current = stack.pop()
        visited_nodes += 1
        if current is None:
            continue
        if isinstance(current, str):
            text = current
        elif isinstance(current, dict):
            children: list[Any] = []
            if "text" in current:
                children.append(current.get("text"))
            if "extra" in current:
                children.append(current.get("extra"))
            if "translate" in current and not children:
                children.append(current.get("translate"))
            stack.extend(reversed(children))
            continue
        elif isinstance(current, (list, tuple)):
            remaining_nodes = MAX_MOTD_NODES - visited_nodes
            stack.extend(reversed(current[:remaining_nodes]))
            continue
        else:
            try:
                text = str(current)
            except Exception:
                continue

        remaining_chars = MAX_MOTD_FLATTENED_LENGTH - output_length
        if remaining_chars <= 0:
            break
        fragment = text[:remaining_chars]
        parts.append(fragment)
        output_length += len(fragment)

    return "".join(parts)


# ---- 延迟历史构建 ----


def build_render_history(
    history_points: list[dict[str, Any]],
    *,
    now_ts: int | None = None,
    history_limit: int,
    silent_query_interval_seconds: int,
) -> list[dict[str, int]]:
    """按时间槽构建固定长度序列，缺失点补零并保留断连间隔。"""
    limit = max(int(history_limit), 1)
    interval = max(int(silent_query_interval_seconds), 1)
    end_ts = int(now_ts if now_ts is not None else time.time())
    start_ts = end_ts - (limit - 1) * interval

    series = [
        {"timestamp": start_ts + index * interval, "latency": 0}
        for index in range(limit)
    ]
    normalized_points: list[tuple[int, int]] = []
    for point in history_points:
        try:
            ts = int(point.get("timestamp", 0) or 0)
            latency = int(point.get("latency", 0) or 0)
        except Exception:
            continue
        if ts <= 0 or ts < start_ts - interval or ts > end_ts + interval:
            continue

        normalized_points.append((ts, max(latency, 0)))

    slot_points: dict[int, tuple[int, int]] = {}
    for ts, latency in normalized_points:
        target_slot = int((ts - start_ts + interval // 2) // interval)
        target_slot = max(0, min(target_slot, limit - 1))
        existing = slot_points.get(target_slot)
        if existing is None or ts >= existing[0]:
            slot_points[target_slot] = (ts, latency)

    for slot, (ts, latency) in slot_points.items():
        series[slot]["timestamp"] = ts
        series[slot]["latency"] = latency

    return series


def build_history_status(
    history_points: list[dict[str, Any]],
    *,
    now_ts: int | None = None,
    window_seconds: int = 24 * 60 * 60,
) -> dict[str, Any]:
    """筛选时间窗口内的延迟历史并计算最大值、最小值。"""
    end_ts = int(now_ts if now_ts is not None else time.time())
    start_ts = end_ts - max(int(window_seconds), 1)
    history: list[dict[str, Any]] = []

    for point in history_points:
        try:
            timestamp = int(point.get("timestamp", 0) or 0)
            latency = max(int(point.get("latency", 0) or 0), 0)
        except Exception:
            continue
        if timestamp <= 0 or timestamp < start_ts or timestamp > end_ts:
            continue
        history.append(
            {
                "timestamp": timestamp,
                "time": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
                "latency": latency,
            }
        )

    history.sort(key=lambda point: point["timestamp"])
    latencies = [point["latency"] for point in history if point["latency"] > 0]
    return {
        "from_timestamp": start_ts,
        "to_timestamp": end_ts,
        "history": history,
        "total": len(history),
        "max_latency": max(latencies) if latencies else None,
        "min_latency": min(latencies) if latencies else None,
    }


def build_history_title(
    history_limit: int,
    silent_query_interval_seconds: int,
) -> str:
    """构建历史图标题文本。"""
    points = max(int(history_limit), 1)
    interval = max(int(silent_query_interval_seconds), 1)
    total_seconds = points * interval
    total_window = format_history_window(total_seconds)
    return f"历史延迟（{total_window} / {points}点）"


def format_history_window(window_seconds: int) -> str:
    """Format a history window for tool responses and render titles."""
    total_seconds = max(int(window_seconds), 1)
    if total_seconds % 3600 == 0:
        return f"{total_seconds // 3600}h"
    if total_seconds % 60 == 0:
        return f"{total_seconds // 60}m"
    return f"{total_seconds}s"
