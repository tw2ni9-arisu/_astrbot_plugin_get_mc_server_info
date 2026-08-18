"""服务器查询模块。

提供 MC 服务器状态拉取、motd 提取、延迟历史构建等函数。
静默轮询逻辑因依赖 self._store_lock / self._save_store 等状态，保留在 main.py 中。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

from mcstatus import JavaServer

MOTD_FORMAT_CODE_PATTERN = re.compile(r"§.")


class McServerConnectionError(RuntimeError):
    """MC server lookup/status request failed."""


class McServerTimeoutError(McServerConnectionError):
    """MC server status request timed out."""


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

async def fetch_server_status(
    address: str,
    *,
    need_players: bool,
    status_timeout: int,
) -> ServerStatus:
    """请求并标准化服务器状态。"""
    try:
        server = await JavaServer.async_lookup(address)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise McServerTimeoutError("server lookup timed out") from exc
    except OSError as exc:
        raise McServerConnectionError("server lookup failed") from exc
    except Exception as exc:
        raise McServerConnectionError("server lookup failed") from exc

    try:
        status = await asyncio.wait_for(
            server.async_status(), timeout=status_timeout
        )
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise McServerTimeoutError("server status timed out") from exc
    except (OSError, ConnectionError) as exc:
        raise McServerConnectionError("server status failed") from exc
    except Exception as exc:
        raise McServerConnectionError("server status failed") from exc

    icon_base64 = None
    if getattr(status, "favicon", None):
        icon_base64 = str(status.favicon)

    players: list[dict[str, str]] = []
    if need_players:
        sample_players = getattr(status.players, "sample", None) or []
        for player in sample_players:
            player_name = getattr(player, "name", "") or ""
            player_uid = getattr(player, "id", "") or ""
            if not player_name:
                continue
            if not player_uid:
                player_uid = hashlib.md5(player_name.encode("utf-8")).hexdigest()
            players.append({"name": player_name, "uid": player_uid})

    latency = int(round(getattr(status, "latency", 0) or 0))
    # async_ping() 测量轻量协议往返，比 async_status() 更接近游戏内显示
    try:
        ping_latency = await asyncio.wait_for(
            server.async_ping(), timeout=min(5, status_timeout)
        )
        if ping_latency > 0:
            latency = int(round(ping_latency))
    except Exception:
        pass
    version = (
        getattr(status.version, "name", "Unknown") if status.version else "Unknown"
    )
    motd = extract_motd_text(getattr(status, "description", None))
    return ServerStatus(
        address=address,
        latency=max(latency, 0),
        version=version,
        players_online=int(getattr(status.players, "online", 0) or 0),
        players_max=int(getattr(status.players, "max", 0) or 0),
        icon_base64=icon_base64,
        players=players,
        motd=motd,
    )


# ---- Motd 提取 ----

def extract_motd_text(description: Any) -> str:
    """提取并归一化服务端 Motd。"""
    if description is None:
        return ""
    try:
        to_plain = getattr(description, "to_plain", None)
        if callable(to_plain):
            text = strip_minecraft_format_codes(str(to_plain() or "")).strip()
            if text:
                return text
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
    """递归展平 Motd 节点为纯文本。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts: list[str] = []
        if "text" in node:
            parts.append(flatten_motd_node(node.get("text")))
        if "extra" in node:
            parts.append(flatten_motd_node(node.get("extra")))
        if "translate" in node and not parts:
            parts.append(flatten_motd_node(node.get("translate")))
        return "".join(parts)
    if isinstance(node, (list, tuple)):
        return "".join(flatten_motd_node(item) for item in node)
    return str(node)


# ---- 延迟历史构建 ----

def build_render_history(
    history_points: list[dict[str, Any]],
    *,
    now_ts: int | None = None,
    history_limit: int,
    silent_query_interval_seconds: int,
) -> list[dict[str, int]]:
    """按历史点顺序右对齐构建固定长度序列，缺失点补零。"""
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
        if ts <= 0 or ts > end_ts + interval:
            continue

        normalized_points.append((ts, max(latency, 0)))

    normalized_points.sort(key=lambda item: item[0])
    normalized_points = normalized_points[-limit:]
    first_slot = limit - len(normalized_points)
    for index, (ts, latency) in enumerate(normalized_points):
        slot = first_slot + index
        series[slot]["timestamp"] = ts
        series[slot]["latency"] = latency

    return series


def build_history_title(
    history_limit: int,
    silent_query_interval_seconds: int,
) -> str:
    """构建历史图标题文本。"""
    points = max(int(history_limit), 1)
    interval = max(int(silent_query_interval_seconds), 1)
    total_seconds = points * interval
    if total_seconds % 3600 == 0:
        total_window = f"{total_seconds // 3600}h"
    else:
        total_window = f"{total_seconds // 60}m"
    return f"历史延迟（{total_window} / {points}点）"
