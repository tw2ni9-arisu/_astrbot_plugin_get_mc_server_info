"""头像下载与渲染模块。

提供玩家皮肤下载、PILSkinMC 渲染、回退裁剪等独立函数。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
from PIL import Image

from astrbot.api import logger

from . import cache as _cache_mod

try:
    import PILSkinMC as _PILSKINMC
except Exception:
    _PILSKINMC = None

SKIN_SIZE = 32
MAX_SKIN_BYTES = 2 * 1024 * 1024
MAX_SKIN_DIMENSION = 1_024
MAX_SKIN_PIXELS = 1_024 * 1_024
MAX_RETRY_AFTER_SECONDS = 30.0


async def download_and_render_avatar_by_uuid(
    *,
    uid: str,
    avatar_path: Path,
    skin_api_url_template: str,
    avatar_download_retries: int,
    semaphore: asyncio.Semaphore,
    session: aiohttp.ClientSession | None,
) -> bool:
    """通过 UUID 拉取皮肤并渲染头像。

    流程：
    1) 调用 skin.mualliance.ltd API 获取皮肤图；
    2) 使用 PILSkinMC 渲染成玩家立体头像；
    3) 缩放到 SKIN_SIZE 并缓存为 PNG。
    """
    if not session:
        return False

    # Collect compact failure reasons for operation visibility and diagnostics.
    failed_reasons: list[str] = []
    for candidate_uuid in build_uuid_candidates(uid):
        try:
            url = skin_api_url_template.format(uuid=candidate_uuid)
        except (KeyError, ValueError):
            failed_reasons.append(f"{candidate_uuid}:invalid_url_template")
            break
        for attempt in range(avatar_download_retries + 1):
            should_retry = attempt < avatar_download_retries
            retry_after_seconds: float | None = None
            try:
                async with semaphore:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            content_length = _parse_content_length(
                                resp.headers.get("Content-Length")
                            )
                            if (
                                content_length is not None
                                and content_length > MAX_SKIN_BYTES
                            ):
                                failed_reasons.append(
                                    f"{candidate_uuid}:200_skin_too_large"
                                )
                                should_retry = False
                                break
                            try:
                                raw = await resp.content.readexactly(MAX_SKIN_BYTES + 1)
                            except asyncio.IncompleteReadError as exc:
                                raw = exc.partial
                            if len(raw) > MAX_SKIN_BYTES:
                                failed_reasons.append(
                                    f"{candidate_uuid}:200_skin_too_large"
                                )
                                should_retry = False
                                break
                            if await asyncio.to_thread(
                                render_avatar_from_skin_bytes,
                                skin_bytes=raw,
                                avatar_path=avatar_path,
                            ):
                                return True
                            # 即使状态码 200，内容也可能非有效皮肤；直接放弃该候选 UUID
                            failed_reasons.append(f"{candidate_uuid}:200_invalid_skin")
                            should_retry = False
                            break
                        # 404 表示该 UUID 没有皮肤记录，尝试下一个 UUID 候选
                        if resp.status == 404:
                            failed_reasons.append(f"{candidate_uuid}:404")
                            should_retry = False
                            break
                        if resp.status == 429:
                            failed_reasons.append(f"{candidate_uuid}:429")
                            retry_after_seconds = parse_retry_after_seconds(
                                resp.headers.get("Retry-After")
                            )
                        # 4xx(除 429)通常不适合重试
                        elif resp.status < 500:
                            failed_reasons.append(f"{candidate_uuid}:{resp.status}")
                            should_retry = False
                            break
                        else:
                            failed_reasons.append(f"{candidate_uuid}:{resp.status}")
            except Exception as exc:
                failed_reasons.append(f"{candidate_uuid}:exc:{type(exc).__name__}")

            if should_retry:
                # Respect Retry-After on 429 when available; otherwise use short backoff.
                await asyncio.sleep(
                    retry_after_seconds
                    if retry_after_seconds is not None
                    else 0.2 * (attempt + 1)
                )
            else:
                break

    logger.warning(
        "avatar download/render failed for uid=%r, reasons=%s",
        uid,
        "; ".join(failed_reasons[:6]) if failed_reasons else "unknown",
    )
    return False


def _parse_content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return max(length, 0)


def render_avatar_from_skin_bytes(
    *,
    skin_bytes: bytes,
    avatar_path: Path,
) -> bool:
    """将皮肤图渲染为头像并保存。

    渲染策略：
    1) 优先尝试 PILSkinMC 的对象式 API（兼容部分版本）；
    2) 若对象式 API 不可用，则回退为标准皮肤头部裁剪（含帽子层）；
    3) 若 PILSkinMC 缺失，不影响回退逻辑，仍可生成头像。
    """
    temp_path: Path | None = None
    try:
        with Image.open(io.BytesIO(skin_bytes)) as skin_raw:
            width, height = skin_raw.size
            if (
                width < 16
                or height < 16
                or width > MAX_SKIN_DIMENSION
                or height > MAX_SKIN_DIMENSION
                or width * height > MAX_SKIN_PIXELS
            ):
                return False
            skin_raw.load()
            skin = skin_raw.convert("RGBA")
            avatar = _render_avatar_by_pilskinmc_object_api(skin_bytes)
            if avatar is None:
                avatar = _render_avatar_head_fallback(skin)
            # 头像使用最近邻放大，保留像素边缘清晰度。
            avatar = avatar.resize((SKIN_SIZE, SKIN_SIZE), Image.Resampling.NEAREST)
            avatar_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=avatar_path.parent,
                prefix=f".{avatar_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
            avatar.save(temp_path, format="PNG")
            temp_path.replace(avatar_path)
        return True
    except Exception as exc:
        logger.debug("render avatar from skin failed: %s", exc)
        return False
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)


def _render_avatar_by_pilskinmc_object_api(
    skin_bytes: bytes,
) -> Image.Image | None:
    """尝试使用 PILSkinMC 的对象式 API 生成头像。

    兼容性说明：
    - 不同版本 PILSkinMC 的入口类/方法可能不同；
    - 这里仅在检测到可用 API 时调用，否则返回 None。
    """
    if _PILSKINMC is None:
        return None

    skin_cls = getattr(_PILSKINMC, "Skin", None)
    if skin_cls is None:
        return None

    try:
        if hasattr(skin_cls, "open") and callable(skin_cls.open):
            skin_obj = skin_cls.open(io.BytesIO(skin_bytes))
        else:
            skin_obj = skin_cls(io.BytesIO(skin_bytes))
    except Exception:
        return None

    # 方法式 API
    for method_name in (
        "get_avatar",
        "render_avatar",
        "render_head",
        "get_head",
    ):
        method = getattr(skin_obj, method_name, None)
        if not callable(method):
            continue
        try:
            sig = inspect.signature(method)
            if "size" in sig.parameters:
                rendered = method(size=SKIN_SIZE)
            else:
                rendered = method()
            if isinstance(rendered, Image.Image):
                return rendered.convert("RGBA")
        except Exception:
            continue

    # 属性式 API（少数实现）
    for attr_name in ("avatar", "head"):
        value = getattr(skin_obj, attr_name, None)
        if isinstance(value, Image.Image):
            return value.convert("RGBA")

    return None


def _render_avatar_head_fallback(skin: Image.Image) -> Image.Image:
    """标准皮肤头像回退渲染（前脸 + 帽子层）。"""
    work_skin = skin
    if _PILSKINMC is not None and hasattr(_PILSKINMC, "fix_legacy"):
        with contextlib.suppress(Exception):
            if work_skin.height == 32:
                work_skin = _PILSKINMC.fix_legacy(work_skin)

    head = work_skin.crop((8, 8, 16, 16)).convert("RGBA")
    # 叠加帽子层
    if work_skin.width >= 48 and work_skin.height >= 16:
        overlay = work_skin.crop((40, 8, 48, 16)).convert("RGBA")
        head.alpha_composite(overlay)
    return head


def build_uuid_candidates(uid: str) -> list[str]:
    """构造 UUID 候选格式（兼容带/不带连字符）。"""
    raw = str(uid or "").strip().lower()
    if not raw or not _cache_mod.is_valid_player_uuid(raw):
        return []
    candidates: list[str] = []
    if raw not in candidates:
        candidates.append(raw)

    no_dash = raw.replace("-", "")
    if len(no_dash) == 32:
        if no_dash not in candidates:
            candidates.append(no_dash)
        hyphen = (
            f"{no_dash[0:8]}-{no_dash[8:12]}-"
            f"{no_dash[12:16]}-{no_dash[16:20]}-{no_dash[20:32]}"
        )
        if hyphen not in candidates:
            candidates.append(hyphen)
    return candidates


def parse_retry_after_seconds(retry_after: str | None) -> float | None:
    """解析 Retry-After 头，返回秒数。"""
    if not retry_after:
        return None
    raw = retry_after.strip()
    try:
        # 数字秒（最常见）
        sec = int(raw)
        return min(float(max(sec, 0)), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        pass
    try:
        # HTTP 日期
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(float(max(delta, 0.0)), MAX_RETRY_AFTER_SECONDS)
    except Exception:
        return None
