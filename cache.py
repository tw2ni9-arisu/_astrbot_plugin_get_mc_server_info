"""缓存管理模块。

提供缓存路径计算、图标缓存等纯函数。
头像缓存和过期清理逻辑因与 Main 类状态耦合过紧，保留在 main.py 中。
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
from pathlib import Path

from .store import address_hash

MAX_ICON_BYTES = 256 * 1024
PLAYER_UUID_PATTERN = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def is_valid_player_uuid(uid: str) -> bool:
    """Return whether a player UUID is in a safe compact or dashed form."""
    return bool(PLAYER_UUID_PATTERN.fullmatch(str(uid or "").strip()))


def normalize_player_uid(uid: str, player_name: str = "") -> str:
    """Keep valid UUIDs and use a stable safe fallback for malformed values."""
    normalized_uid = str(uid or "").strip().lower()
    if is_valid_player_uuid(normalized_uid):
        return normalized_uid
    fallback_name = str(player_name or normalized_uid)
    return hashlib.md5(fallback_name.encode("utf-8")).hexdigest()


def server_cache_dir(cache_root: Path, address: str) -> Path:
    """服务器缓存目录。"""
    return cache_root / address_hash(address)


def icon_cache_path(cache_root: Path, address: str) -> Path:
    """服务器图标缓存路径。"""
    return server_cache_dir(cache_root, address) / "icon.png"


def skin_cache_path(cache_root: Path, address: str, uid: str) -> Path:
    """玩家头像缓存路径。"""
    safe_uid = normalize_player_uid(uid)
    return server_cache_dir(cache_root, address) / "skins" / f"{safe_uid}.png"


def delete_server_cache(cache_root: Path, address: str) -> None:
    """删除指定服务器的全部缓存目录。"""
    cache_dir = server_cache_dir(cache_root, address)
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)


async def cache_server_icon(
    cache_root: Path,
    address: str,
    icon_base64: str | None,
) -> None:
    """缓存服务器图标（icon.png）。

    图标缓存失败不抛错，避免影响主业务链路。
    """
    if not icon_base64:
        return
    payload = icon_base64
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return
    if len(raw) > MAX_ICON_BYTES:
        return
    icon_path = icon_cache_path(cache_root, address)
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    icon_path.write_bytes(raw)
