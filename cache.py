"""缓存管理模块。

提供缓存路径计算、图标缓存等纯函数。
头像缓存和过期清理逻辑因与 Main 类状态耦合过紧，保留在 main.py 中。
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

from .store import address_hash


def server_cache_dir(cache_root: Path, address: str) -> Path:
    """服务器缓存目录。"""
    return cache_root / address_hash(address)


def icon_cache_path(cache_root: Path, address: str) -> Path:
    """服务器图标缓存路径。"""
    return server_cache_dir(cache_root, address) / "icon.png"


def skin_cache_path(cache_root: Path, address: str, uid: str) -> Path:
    """玩家头像缓存路径。"""
    return server_cache_dir(cache_root, address) / "skins" / f"{uid}.png"


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
    icon_path = icon_cache_path(cache_root, address)
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    icon_path.write_bytes(raw)
