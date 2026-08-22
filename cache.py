"""缓存管理模块。

提供缓存路径计算、图标缓存等纯函数。
头像缓存和过期清理逻辑因与 Main 类状态耦合过紧，保留在 main.py 中。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import re
import shutil
import struct
import tempfile
from pathlib import Path

from .store import address_hash

MAX_ICON_BYTES = 256 * 1024
MAX_ICON_DIMENSION = 1_024
MAX_ICON_PIXELS = 1_024 * 1_024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
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
    payload = "".join(payload.split())
    max_base64_chars = ((MAX_ICON_BYTES + 2) // 3) * 4 + 4
    if len(payload) > max_base64_chars:
        return
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        return
    if len(raw) > MAX_ICON_BYTES or not _is_safe_png(raw):
        return
    temp_path: Path | None = None
    try:
        icon_path = icon_cache_path(cache_root, address)
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=icon_path.parent,
            prefix=f".{icon_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(raw)
            temp_path = Path(temp_file.name)
        temp_path.replace(icon_path)
    except OSError:
        return
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)


def _is_safe_png(raw: bytes) -> bool:
    """Validate the fixed PNG header without decompressing attacker data."""
    if len(raw) < 33 or not raw.startswith(PNG_SIGNATURE):
        return False
    if raw[12:16] != b"IHDR" or raw[8:12] != b"\x00\x00\x00\r":
        return False
    width, height = struct.unpack(">II", raw[16:24])
    return (
        0 < width <= MAX_ICON_DIMENSION
        and 0 < height <= MAX_ICON_DIMENSION
        and width * height <= MAX_ICON_PIXELS
    )
