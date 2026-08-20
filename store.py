"""插件持久化存储模块。

提供会话初始化、服务器名称查找/去重、延迟历史追加、地址标准化等纯函数。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PORT = 25565
DEFAULT_TEMPLATE_NAME = "default_method"


class InvalidServerAddressError(ValueError):
    """The server address is malformed or resolves to a non-public network."""


def get_or_create_session(store: dict[str, Any], session_key: str) -> dict[str, Any]:
    """获取或初始化会话对象。"""
    sessions = store.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
        store["sessions"] = sessions
    session_obj = sessions.get(session_key)
    if not isinstance(session_obj, dict):
        session_obj = {}
        sessions[session_key] = session_obj
    if not isinstance(session_obj.get("servers"), dict):
        session_obj["servers"] = {}
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
    raw = address.strip()
    if not raw:
        return raw
    if not auto_append_default_port:
        return raw

    try:
        literal_ip = ipaddress.ip_address(raw)
    except ValueError:
        literal_ip = None
    if isinstance(literal_ip, ipaddress.IPv6Address):
        return f"[{literal_ip}]:{DEFAULT_PORT}"
    if isinstance(literal_ip, ipaddress.IPv4Address):
        return f"{literal_ip}:{DEFAULT_PORT}"

    if raw.startswith("["):
        closing_bracket = raw.find("]")
        if closing_bracket < 0:
            return raw
        try:
            ipv6_host = ipaddress.IPv6Address(raw[1:closing_bracket])
        except ValueError:
            return raw
        suffix = raw[closing_bracket + 1 :]
        if not suffix:
            return f"[{ipv6_host}]:{DEFAULT_PORT}"
        if not suffix.startswith(":"):
            return raw
        port_str = suffix[1:]
        port = int(port_str) if port_str.isdigit() else DEFAULT_PORT
        return f"[{ipv6_host}]:{port}"

    if ":" not in raw:
        return f"{raw}:{DEFAULT_PORT}"
    if raw.count(":") != 1:
        return raw
    host, port_str = raw.rsplit(":", 1)
    if not port_str.isdigit():
        return f"{host}:{DEFAULT_PORT}"
    return f"{host}:{int(port_str)}"


def parse_server_address(
    address: str,
    *,
    default_port: int = DEFAULT_PORT,
) -> tuple[str, int]:
    """Parse and validate the host/port syntax used by mcstatus."""
    raw = (address or "").strip()
    if not raw or any(char.isspace() or ord(char) < 32 for char in raw):
        raise InvalidServerAddressError("server address is invalid")

    try:
        literal_ip = ipaddress.ip_address(raw)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not 0 <= default_port <= 65535:
            raise InvalidServerAddressError("server address port is invalid")
        return str(literal_ip), default_port

    try:
        parsed = urlsplit(f"//{raw}")
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InvalidServerAddressError("server address is invalid") from exc

    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise InvalidServerAddressError("server address is invalid")

    if port is None:
        port = default_port
    if not 0 <= port <= 65535:
        raise InvalidServerAddressError("server address port is invalid")
    return host, port


def _is_public_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an IP is suitable for an outbound server connection."""
    if isinstance(value, ipaddress.IPv4Address) and value in ipaddress.ip_network(
        "192.0.0.0/24"
    ):
        return False
    return (
        value.is_global
        and not value.is_private
        and not value.is_loopback
        and not value.is_link_local
        and not value.is_reserved
        and not value.is_unspecified
        and not value.is_multicast
    )


async def resolve_public_server_target(host: str, port: int) -> tuple[str, int]:
    """Resolve a server target and reject every non-public address result."""
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            addr_info = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise InvalidServerAddressError(
                "server address could not be resolved"
            ) from exc

        resolved_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for _, _, _, _, sockaddr in addr_info:
            try:
                resolved_ips.append(ipaddress.ip_address(sockaddr[0]))
            except (IndexError, ValueError) as exc:
                raise InvalidServerAddressError(
                    "server address resolution returned an invalid IP"
                ) from exc
    else:
        resolved_ips = [literal_ip]

    if not resolved_ips or any(not _is_public_ip(ip) for ip in resolved_ips):
        raise InvalidServerAddressError(
            "server address must resolve to public IP addresses"
        )
    return str(resolved_ips[0]), port


def address_hash(address: str) -> str:
    """将地址映射为稳定哈希，用作缓存目录名。"""
    return hashlib.sha1(address.encode("utf-8")).hexdigest()
