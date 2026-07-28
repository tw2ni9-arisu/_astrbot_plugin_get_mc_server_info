"""插件持久化存储模块。

提供会话初始化、服务器名称查找/去重、延迟历史追加、地址标准化等纯函数。
"""

from __future__ import annotations

import hashlib
from typing import Any

DEFAULT_PORT = 25565
DEFAULT_TEMPLATE_NAME = "default_method"


def get_or_create_session(
    store: dict[str, Any], session_key: str
) -> dict[str, Any]:
    """获取或初始化会话对象。"""
    sessions = store.setdefault("sessions", {})
    session_obj = sessions.setdefault(session_key, {})
    session_obj.setdefault("servers", {})
    session_obj.setdefault("template", DEFAULT_TEMPLATE_NAME)
    return session_obj


def find_server_addresses_by_name(
    servers: dict[str, dict[str, Any]],
    query_name: str,
) -> list[str]:
    """按显示名称匹配会话内已添加服务器，返回命中的地址列表。"""
    target = query_name.strip()
    if not target:
        return []
    addresses: list[str] = []
    for address, server_obj in servers.items():
        server_name = str(server_obj.get("name", "")).strip()
        if server_name == target:
            addresses.append(address)
    return addresses


def resolve_unique_server_name(
    desired_name: str,
    servers: dict[str, dict[str, Any]],
    *,
    exclude_address: str | None = None,
) -> tuple[str, bool]:
    """会话内服务器名称去重，必要时自动追加序号后缀。"""
    base = desired_name.strip()
    if not base:
        base = "未命名服务器"
    existing_names: set[str] = set()
    for address, server_obj in servers.items():
        if exclude_address and address == exclude_address:
            continue
        existing_name = str(server_obj.get("name", "")).strip()
        if existing_name:
            existing_names.add(existing_name)
    if base not in existing_names:
        return base, False
    index = 1
    while True:
        candidate = f"{base}({index})"
        if candidate not in existing_names:
            return candidate, True
        index += 1


def append_latency(
    server_obj: dict[str, Any],
    latency: int,
    now_ts: int,
    history_limit: int,
) -> None:
    """追加延迟历史并裁剪到固定长度。"""
    history = server_obj.setdefault("latency_history", [])
    history.append({"timestamp": now_ts, "latency": int(latency)})
    if len(history) > history_limit:
        server_obj["latency_history"] = history[-history_limit:]


def normalize_address(
    address: str,
    auto_append_default_port: bool,
) -> str:
    """标准化服务器地址。"""
    address = address.strip()
    if not address:
        return address
    if not auto_append_default_port:
        return address
    if ":" not in address:
        return f"{address}:{DEFAULT_PORT}"
    host, port_str = address.rsplit(":", 1)
    if not port_str.isdigit():
        return f"{host}:{DEFAULT_PORT}"
    return f"{host}:{int(port_str)}"


def address_hash(address: str) -> str:
    """将地址映射为稳定哈希，用作缓存目录名。"""
    return hashlib.sha1(address.encode("utf-8")).hexdigest()
