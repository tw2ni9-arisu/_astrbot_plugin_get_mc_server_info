"""Default renderer for cached lists and full multi-server queries."""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .default_method_query import (
    BAD_COLOR,
    BG,
    CARD_ALPHA,
    SUB_TEXT,
    TEXT,
    _latency_color,
    _load_font,
    _load_template_background,
    _server_icon,
)

DEFAULT_CANVAS_WIDTH = 900
OUTER_PADDING = 24
PANEL_GAP = 12
CARD_GAP = 20
HEADER_HEIGHT = 120
ROW_HEIGHT = 38
CONTENT_MIN_HEIGHT = 88
CONTENT_PADDING = 14
CONTENT_TITLE_HEIGHT = 28
ICON_SIZE = 80
AVATAR_SIZE = 28


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _row_count(entry: dict[str, Any], mode: str) -> int:
    field = "lines" if mode == "list" else "players"
    rows = entry.get(field, [])
    return max(len(rows) if isinstance(rows, list) else 0, 1)


def _content_height(entry: dict[str, Any], mode: str) -> int:
    return max(
        CONTENT_MIN_HEIGHT,
        CONTENT_PADDING * 2 + CONTENT_TITLE_HEIGHT + _row_count(entry, mode) * ROW_HEIGHT,
    )


def _card_height(entry: dict[str, Any], mode: str) -> int:
    """Return one card's exact stacked header/content height."""
    if mode not in {"list", "query_all"}:
        raise ValueError("mode must be 'list' or 'query_all'")
    return HEADER_HEIGHT + PANEL_GAP + _content_height(entry, mode)


def _canvas_height(servers: list[dict[str, Any]], mode: str) -> int:
    if not servers:
        return OUTER_PADDING * 2
    return (
        OUTER_PADDING * 2
        + sum(_card_height(entry, mode) for entry in servers)
        + CARD_GAP * (len(servers) - 1)
    )


def _truncate_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    value = str(value or "")
    if draw.textlength(value, font=font) <= max_width:
        return value
    suffix = "..."
    while value and draw.textlength(value + suffix, font=font) > max_width:
        value = value[:-1]
    return value + suffix if value else suffix


def _format_latency(entry: dict[str, Any]) -> tuple[str, tuple[int, int, int]]:
    latency = _safe_int(entry.get("latency"), 0)
    offline = bool(entry.get("offline")) or latency <= 0
    if offline:
        return "Offline", BAD_COLOR
    return f"{latency}ms", _latency_color(latency)


def _display_address(entry: dict[str, Any], mode: str) -> str:
    """Select the logical primary for lists and the responding line for queries."""
    if mode == "query_all":
        value = entry.get("address") or entry.get("primary_address")
    else:
        value = entry.get("primary_address") or entry.get("address")
    return str(value or "")


def _load_avatar(path_value: Any) -> Image.Image:
    path = Path(str(path_value or ""))
    if path_value and path.is_file():
        try:
            image = Image.open(path).convert("RGBA")
            return ImageOps.fit(
                image,
                (AVATAR_SIZE, AVATAR_SIZE),
                method=Image.Resampling.LANCZOS,
            )
        except OSError:
            pass
    image = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (84, 94, 110, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 4, 20, 16), fill=(190, 200, 220, 255))
    draw.rectangle((7, 16, 21, 27), fill=(190, 200, 220, 255))
    return image


def _draw_header(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    entry: dict[str, Any],
    rect: tuple[int, int, int, int],
    *,
    mode: str,
) -> None:
    left, top, right, _ = rect
    icon = _server_icon(str(entry.get("icon_path") or "") or None)
    icon = ImageOps.fit(icon, (ICON_SIZE, ICON_SIZE), method=Image.Resampling.LANCZOS)
    icon_x = left + 16
    icon_y = top + (HEADER_HEIGHT - ICON_SIZE) // 2
    image.paste(icon, (icon_x, icon_y), icon)

    title_font = _load_font(30)
    detail_font = _load_font(18)
    value_font = _load_font(22)
    text_x = icon_x + ICON_SIZE + 28
    metrics_start = max(text_x + 260, right - 320)
    name = _truncate_text(
        draw,
        str(entry.get("name") or entry.get("primary_address") or "Unknown"),
        title_font,
        max(metrics_start - text_x - 18, 80),
    )
    address = _truncate_text(
        draw,
        _display_address(entry, mode),
        detail_font,
        max(metrics_start - text_x - 18, 80),
    )
    draw.text((text_x, top + 18), name, fill=TEXT, font=title_font)
    draw.text((text_x, top + 59), address, fill=SUB_TEXT, font=detail_font)

    version = str(entry.get("version") or "").strip()
    if version:
        version = _truncate_text(
            draw,
            f"版本: {version}",
            detail_font,
            max(metrics_start - text_x - 18, 80),
        )
        draw.text((text_x, top + 87), version, fill=SUB_TEXT, font=detail_font)

    latency_label = "最近延迟" if mode == "list" else "当前延迟"
    latency_text, latency_color = _format_latency(entry)
    draw.text((metrics_start, top + 24), latency_label, fill=SUB_TEXT, font=detail_font)
    draw.text((metrics_start, top + 56), latency_text, fill=latency_color, font=value_font)

    online_x = right - 142
    draw.text((online_x, top + 24), "在线人数", fill=SUB_TEXT, font=detail_font)
    players_online = entry.get("players_online")
    players_max = entry.get("players_max")
    online_text = (
        f"{_safe_int(players_online)}/{_safe_int(players_max)}"
        if players_online is not None and players_max is not None
        else "—"
    )
    draw.text((online_x, top + 56), online_text, fill=TEXT, font=value_font)


def _draw_list_rows(
    draw: ImageDraw.ImageDraw,
    entry: dict[str, Any],
    *,
    left: int,
    top: int,
    right: int,
) -> None:
    title_font = _load_font(19)
    row_font = _load_font(17)
    draw.text((left + 16, top + 12), "服务器线路", fill=TEXT, font=title_font)
    lines = entry.get("lines", [])
    if not isinstance(lines, list) or not lines:
        draw.text((left + 18, top + 48), "暂无线路", fill=SUB_TEXT, font=row_font)
        return

    y = top + CONTENT_PADDING + CONTENT_TITLE_HEIGHT
    backup_index = 0
    for line in lines:
        if not isinstance(line, dict):
            continue
        is_primary = str(line.get("line_type", "backup")) == "primary"
        if is_primary:
            label = "主线路"
        else:
            backup_index += 1
            label = f"备用线路 {backup_index}"
        draw.text((left + 18, y + 8), label, fill=SUB_TEXT, font=row_font)
        address = _truncate_text(
            draw,
            str(line.get("address") or ""),
            row_font,
            max(right - left - 174, 80),
        )
        draw.text((left + 148, y + 8), address, fill=TEXT, font=row_font)
        y += ROW_HEIGHT


def _draw_player_rows(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    entry: dict[str, Any],
    *,
    left: int,
    top: int,
    right: int,
) -> None:
    title_font = _load_font(19)
    row_font = _load_font(17)
    draw.text((left + 16, top + 12), "在线玩家", fill=TEXT, font=title_font)
    players = entry.get("players", [])
    if not isinstance(players, list) or not players:
        empty_text = "服务器离线" if entry.get("offline") else "暂无玩家在线"
        draw.text((left + 18, top + 48), empty_text, fill=SUB_TEXT, font=row_font)
        return

    y = top + CONTENT_PADDING + CONTENT_TITLE_HEIGHT
    for player in players:
        if not isinstance(player, dict):
            continue
        avatar = _load_avatar(player.get("avatar_path"))
        image.paste(avatar, (left + 18, y + 5), avatar)
        name = _truncate_text(
            draw,
            str(player.get("name") or "Unknown"),
            row_font,
            max(right - left - 78, 80),
        )
        draw.text((left + 58, y + 9), name, fill=TEXT, font=row_font)
        y += ROW_HEIGHT


async def render_server_list_image(
    *,
    mode: str,
    servers: list[dict[str, Any]],
    canvas_width: int = DEFAULT_CANVAS_WIDTH,
) -> str:
    """Render list/query-all cards without blocking the event loop."""
    return await asyncio.to_thread(
        _render_server_list_image_sync,
        mode=mode,
        servers=servers,
        canvas_width=canvas_width,
    )


def _render_server_list_image_sync(
    *,
    mode: str,
    servers: list[dict[str, Any]],
    canvas_width: int = DEFAULT_CANVAS_WIDTH,
) -> str:
    if mode not in {"list", "query_all"}:
        raise ValueError("mode must be 'list' or 'query_all'")
    entries = [entry for entry in servers if isinstance(entry, dict)]
    width = max(int(canvas_width), 480)
    height = _canvas_height(entries, mode)
    background = _load_template_background(
        width,
        height,
        centering=(0.5, 0.0),
    )
    image = background or Image.new("RGBA", (width, height), BG)
    image = image.convert("RGBA")

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    card_layout: list[
        tuple[dict[str, Any], tuple[int, int, int, int], tuple[int, int, int, int]]
    ] = []
    y = OUTER_PADDING
    for entry in entries:
        header_rect = (
            OUTER_PADDING,
            y,
            width - OUTER_PADDING,
            y + HEADER_HEIGHT,
        )
        content_top = header_rect[3] + PANEL_GAP
        content_rect = (
            OUTER_PADDING,
            content_top,
            width - OUTER_PADDING,
            content_top + _content_height(entry, mode),
        )
        overlay_draw.rounded_rectangle(header_rect, radius=14, fill=CARD_ALPHA)
        overlay_draw.rounded_rectangle(content_rect, radius=14, fill=CARD_ALPHA)
        card_layout.append((entry, header_rect, content_rect))
        y = content_rect[3] + CARD_GAP

    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image)
    for entry, header_rect, content_rect in card_layout:
        _draw_header(image, draw, entry, header_rect, mode=mode)
        if mode == "list":
            _draw_list_rows(
                draw,
                entry,
                left=content_rect[0],
                top=content_rect[1],
                right=content_rect[2],
            )
        else:
            _draw_player_rows(
                image,
                draw,
                entry,
                left=content_rect[0],
                top=content_rect[1],
                right=content_rect[2],
            )

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
