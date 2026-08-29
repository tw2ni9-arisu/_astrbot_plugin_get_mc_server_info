"""Alternative dashboard renderer for MC server report images."""

from __future__ import annotations

import asyncio
import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH = 1100
PADDING = 28
GAP = 18
HEADER_H = 166
MOTD_H = 88
CHART_H = 300
PLAYER_PANEL_W = 350
PLAYER_ROW_H = 42
PLAYER_AVATAR_SIZE = 32
MAX_RENDERED_PLAYERS = 50
CHART_GRID_COUNT = 5
HOUR_SECONDS = 60 * 60
TIME_TICK_FONT_SIZE = 12

# A graphite dashboard palette with teal, amber, and red status accents.
BG = (14, 18, 23)
PANEL = (27, 34, 41)
PANEL_ALT = (22, 29, 35)
PANEL_EDGE = (57, 72, 84)
PANEL_ALPHA = 188
PANEL_ALT_ALPHA = 172
TEXT = (239, 245, 249)
SUB_TEXT = (158, 174, 190)
MUTED_TEXT = (105, 123, 139)
LINE = (91, 220, 199)
GRID = (52, 67, 79)
OK_COLOR = (106, 224, 151)
WARN_COLOR = (255, 188, 95)
BAD_COLOR = (244, 107, 107)
OUTAGE_FILL = (236, 151, 115)
OUTAGE_LINE = (244, 178, 129)
CUSTOM_FONT_EXTENSIONS = (".ttf", ".ttc", ".otf")
_CUSTOM_FONT_PATHS: list[Path] | None = None


def _list_custom_fonts() -> list[Path]:
    global _CUSTOM_FONT_PATHS
    if _CUSTOM_FONT_PATHS is not None:
        return _CUSTOM_FONT_PATHS

    template_dir = Path(__file__).resolve().parent
    paths: list[Path] = []
    for extension in CUSTOM_FONT_EXTENSIONS:
        paths.extend(sorted(template_dir.glob(f"*{extension}")))
    if not paths:
        default_template_dir = template_dir.parent / "default_method"
        for extension in CUSTOM_FONT_EXTENSIONS:
            paths.extend(sorted(default_template_dir.glob(f"*{extension}")))
    _CUSTOM_FONT_PATHS = paths
    return paths


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_path in _list_custom_fonts():
        try:
            return ImageFont.truetype(str(font_path), size)
        except OSError:
            continue
    for name in ("arial.ttf", "msyh.ttc", "simhei.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _latency_color(latency: int) -> tuple[int, int, int]:
    if latency < 100:
        return OK_COLOR
    if latency < 200:
        return WARN_COLOR
    return BAD_COLOR


def _truncate_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    if draw.textlength(value, font=font) <= max_width:
        return value

    suffix = "..."
    value = value.rstrip()
    while value and draw.textlength(value + suffix, font=font) > max_width:
        value = value[:-1]
    return value + suffix if value else suffix


def _server_icon(icon_path: str | None) -> Image.Image:
    default_icon_path = _find_named_image(
        Path(__file__).resolve().parent,
        "default_icon",
    )
    candidates = [Path(icon_path)] if icon_path else []
    if default_icon_path is not None:
        candidates.append(default_icon_path)

    for file_path in candidates:
        if not file_path.exists():
            continue
        try:
            icon = Image.open(file_path).convert("RGBA")
            return ImageOps.fit(
                icon,
                (104, 104),
                method=Image.Resampling.NEAREST,
            )
        except OSError:
            continue

    icon = Image.new("RGBA", (104, 104), (40, 93, 113, 255))
    icon_draw = ImageDraw.Draw(icon)
    icon_draw.rectangle((5, 5, 98, 98), outline=LINE, width=3)
    icon_draw.rectangle((18, 18, 85, 85), outline=(198, 246, 235), width=2)
    icon_draw.text((32, 37), "MC", fill=TEXT, font=_load_font(24))
    return icon


def _load_template_background(width: int, height: int) -> Image.Image | None:
    """Load a same-named background image and fit it to the rendered canvas."""
    template_file = Path(__file__).resolve()
    parent = template_file.parent
    public_name = template_file.stem.removesuffix("_query")
    candidate = _find_named_image(parent, public_name)
    if candidate is not None:
        try:
            image = Image.open(candidate).convert("RGBA")
            return ImageOps.fit(
                image,
                (width, height),
                method=Image.Resampling.LANCZOS,
            )
        except OSError:
            pass
    return None


def _find_named_image(directory: Path, stem: str) -> Path | None:
    for candidate in directory.iterdir():
        if not candidate.is_file() or candidate.stem != stem:
            continue
        if candidate.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            return candidate
    return None


def _next_twenty(value: int) -> int:
    return max(20, (value // 20 + 1) * 20)


def _non_zero_latencies(latencies: list[int]) -> list[int]:
    return [latency for latency in latencies if latency > 0]


def _calculate_y_axis_max(latencies: list[int]) -> int:
    if not latencies:
        return 20

    maximum = max(latencies)
    average = sum(latencies) / len(latencies)
    if maximum <= average + 20:
        return _next_twenty(maximum)

    other_latencies = latencies.copy()
    other_latencies.remove(maximum)
    return _next_twenty(max(other_latencies)) if other_latencies else _next_twenty(maximum)


def _find_zero_ranges(latencies: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for index, latency in enumerate(latencies):
        if latency <= 0 and start is None:
            start = index
        elif latency > 0 and start is not None:
            ranges.append((start, index - 1))
            start = None
    if start is not None:
        ranges.append((start, len(latencies) - 1))
    return ranges


def _draw_dashed_vertical_line(
    draw: ImageDraw.ImageDraw,
    x: int,
    top: int,
    bottom: int,
) -> None:
    for y in range(top, bottom + 1, 10):
        draw.line(
            (x, y, x, min(y + 5, bottom)),
            fill=OUTAGE_LINE,
            width=2,
        )


def _draw_outage_background(
    image: Image.Image,
    latencies: list[int],
    plot_left: int,
    plot_top: int,
    plot_right: int,
    plot_bottom: int,
) -> list[tuple[int, int, bool, bool]]:
    if not _non_zero_latencies(latencies):
        return []

    point_count = len(latencies)
    point_width = plot_right - plot_left

    def point_x(index: int) -> int:
        return int(plot_left + point_width * index / max(1, point_count - 1))

    boundaries: list[tuple[int, int, bool, bool]] = []
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for start, end in _find_zero_ranges(latencies):
        left_x = (
            plot_left
            if start == 0
            else (point_x(start - 1) + point_x(start)) // 2
        )
        right_x = (
            plot_right
            if end == point_count - 1
            else (point_x(end) + point_x(end + 1)) // 2
        )
        if right_x <= left_x:
            continue
        overlay_draw.rectangle(
            (left_x, plot_top, right_x, plot_bottom),
            fill=(*OUTAGE_FILL, 42),
        )
        boundaries.append((left_x, right_x, start > 0, end < point_count - 1))

    if boundaries:
        image.alpha_composite(overlay)
    return boundaries


def _floor_to_half_hour(timestamp: int) -> datetime:
    current = datetime.fromtimestamp(timestamp)
    return current.replace(
        minute=0 if current.minute < 30 else 30,
        second=0,
        microsecond=0,
    )


def _build_time_ticks(
    history: list[dict[str, Any]],
    plot_left: int,
    plot_right: int,
) -> list[tuple[int, str]]:
    timestamps: list[int] = []
    for point in history:
        try:
            timestamp = int(point.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            continue
        if timestamp > 0:
            timestamps.append(timestamp)
    if not timestamps:
        return []

    latest_tick = int(_floor_to_half_hour(max(timestamps)).timestamp())
    earliest_timestamp = min(timestamps)
    tick_count = max(1, (latest_tick - earliest_timestamp) // HOUR_SECONDS + 1)
    width = plot_right - plot_left
    return [
        (
            int(plot_right - width * index / max(1, tick_count - 1)),
            datetime.fromtimestamp(latest_tick - index * HOUR_SECONDS).strftime(
                "%H:%M"
            ),
        )
        for index in range(tick_count)
    ]


def _history_latencies(history: list[dict[str, Any]]) -> list[int]:
    latencies: list[int] = []
    for point in history:
        try:
            latencies.append(max(0, int(point.get("latency", 0) or 0)))
        except (TypeError, ValueError):
            latencies.append(0)
    return latencies


def _draw_history_chart(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    chart_rect: tuple[int, int, int, int],
    history: list[dict[str, Any]],
    history_title: str,
) -> None:
    left, top, right, bottom = chart_rect
    title_font = _load_font(23)
    text_font = _load_font(15)
    time_font = _load_font(TIME_TICK_FONT_SIZE)
    draw.text((left + 18, top + 14), history_title, fill=TEXT, font=title_font)
    draw.text(
        (right - 18, top + 20),
        "LATENCY / TIMELINE",
        fill=MUTED_TEXT,
        font=time_font,
        anchor="ra",
    )

    plot_left, plot_right = left + 72, right - 20
    plot_top, plot_bottom = top + 72, bottom - 50
    latencies = _history_latencies(history)
    observed_latencies = _non_zero_latencies(latencies)
    y_axis_max = _calculate_y_axis_max(observed_latencies or latencies)
    outage_boundaries = _draw_outage_background(
        image,
        latencies,
        plot_left,
        plot_top,
        plot_right,
        plot_bottom,
    )

    for index in range(CHART_GRID_COUNT):
        ratio = index / (CHART_GRID_COUNT - 1)
        y = int(plot_top + (plot_bottom - plot_top) * ratio)
        value = int(y_axis_max * (1 - ratio))
        draw.line((plot_left, y, plot_right, y), fill=GRID, width=1)
        draw.line((plot_left - 5, y, plot_left, y), fill=GRID, width=1)
        draw.text(
            (plot_left - 10, y),
            str(value),
            fill=SUB_TEXT,
            font=text_font,
            anchor="rm",
        )

    for index, (x, label) in enumerate(
        _build_time_ticks(history, plot_left, plot_right)
    ):
        draw.line((x, plot_bottom, x, plot_bottom + 4), fill=GRID, width=1)
        draw.text(
            (x, plot_bottom + 7 + (index % 2) * 16),
            label,
            fill=SUB_TEXT,
            font=time_font,
            anchor="mt",
        )

    if not history:
        draw.text(
            (plot_left + 12, plot_top + 20),
            "暂无延迟数据",
            fill=SUB_TEXT,
            font=text_font,
        )
        return

    point_count = len(latencies)
    points: list[tuple[int, int]] = []
    for index, latency in enumerate(latencies):
        x = int(plot_left + (plot_right - plot_left) * index / max(1, point_count - 1))
        ratio = min(1, max(0, latency / y_axis_max))
        y = int(plot_bottom - ratio * (plot_bottom - plot_top))
        points.append((x, y))

    for index in range(1, len(points)):
        if latencies[index - 1] > 0 and latencies[index] > 0:
            draw.line((points[index - 1], points[index]), fill=LINE, width=3)

    for index, point in enumerate(points):
        if latencies[index] > 0:
            radius = 3 if index != len(points) - 1 else 5
            draw.ellipse(
                (
                    point[0] - radius,
                    point[1] - radius,
                    point[0] + radius,
                    point[1] + radius,
                ),
                fill=LINE,
            )

    for left_x, right_x, has_left_boundary, has_right_boundary in outage_boundaries:
        if has_left_boundary:
            _draw_dashed_vertical_line(draw, left_x, plot_top, plot_bottom)
        if has_right_boundary:
            _draw_dashed_vertical_line(draw, right_x, plot_top, plot_bottom)

    minimum = min(observed_latencies) if observed_latencies else 0
    draw.text(
        (right - 18, top + 48),
        f"MAX {max(latencies)}ms   MIN {minimum}ms",
        fill=SUB_TEXT,
        font=text_font,
        anchor="ra",
    )


def _paste_avatar(img: Image.Image, avatar_path: str, xy: tuple[int, int]) -> None:
    x, y = xy
    if avatar_path:
        file_path = Path(avatar_path)
        if file_path.exists():
            try:
                avatar = ImageOps.fit(
                    Image.open(file_path).convert("RGBA"),
                    (PLAYER_AVATAR_SIZE, PLAYER_AVATAR_SIZE),
                    method=Image.Resampling.NEAREST,
                )
                img.paste(avatar, (x, y), avatar)
                return
            except OSError:
                pass

    fallback = Image.new("RGBA", (PLAYER_AVATAR_SIZE, PLAYER_AVATAR_SIZE), (67, 86, 98, 255))
    fallback_draw = ImageDraw.Draw(fallback)
    fallback_draw.rectangle((5, 5, 26, 26), outline=(181, 224, 216, 255), width=2)
    fallback_draw.rectangle((11, 11, 20, 20), fill=(181, 224, 216, 255))
    img.paste(fallback, (x, y), fallback)


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    rect: tuple[int, int, int, int],
    *,
    fill: tuple[int, int, int, int] = (*PANEL, PANEL_ALPHA),
    radius: int = 14,
) -> None:
    draw.rounded_rectangle(rect, radius=radius, fill=fill, outline=PANEL_EDGE, width=1)


async def render_server_report_image(**kwargs: Any) -> str:
    """Offload Pillow rendering so it cannot block AstrBot's event loop."""
    return await asyncio.to_thread(_render_server_report_image_sync, **kwargs)


def _render_server_report_image_sync(
    *,
    server_name: str,
    server_address: str,
    latency: int | str,
    players_online: int,
    players_max: int,
    server_version: str,
    history: list[dict[str, Any]],
    icon_path: str | None,
    players: list[dict[str, str]],
    motd: str = "",
    history_title: str = "历史延迟",
    offline: bool = False,
) -> str:
    players = players[:MAX_RENDERED_PLAYERS]
    player_panel_h = max(CHART_H, 88 + len(players) * PLAYER_ROW_H)
    total_h = PADDING + HEADER_H + GAP + MOTD_H + GAP + player_panel_h + PADDING
    background = _load_template_background(WIDTH, total_h)
    if background is None:
        background = Image.new("RGBA", (WIDTH, total_h), BG)

    overlay = Image.new("RGBA", (WIDTH, total_h), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    header_top = PADDING
    header_rect = (PADDING, header_top, WIDTH - PADDING, header_top + HEADER_H)
    motd_top = header_rect[3] + GAP
    motd_rect = (PADDING, motd_top, WIDTH - PADDING, motd_top + MOTD_H)
    dashboard_top = motd_rect[3] + GAP
    chart_left = PADDING
    chart_right = WIDTH - PADDING - PLAYER_PANEL_W - GAP
    chart_rect = (chart_left, dashboard_top, chart_right, dashboard_top + CHART_H)
    player_left = chart_right + GAP
    player_rect = (player_left, dashboard_top, WIDTH - PADDING, dashboard_top + player_panel_h)

    _draw_panel(overlay_draw, header_rect)
    _draw_panel(
        overlay_draw,
        motd_rect,
        fill=(*PANEL_ALT, PANEL_ALT_ALPHA),
        radius=12,
    )
    _draw_panel(overlay_draw, chart_rect, radius=12)
    _draw_panel(
        overlay_draw,
        player_rect,
        fill=(*PANEL_ALT, PANEL_ALT_ALPHA),
        radius=12,
    )
    image = Image.alpha_composite(background, overlay)
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, WIDTH, 5), fill=LINE)
    draw.line((PADDING, total_h - PADDING, WIDTH - PADDING, total_h - PADDING), fill=GRID)

    # Header: identity stays left while the three live facts form a compact status rail.
    icon = _server_icon(icon_path)
    icon_x, icon_y = PADDING + 24, header_top + 31
    image.paste(icon, (icon_x, icon_y), icon)

    text_x = icon_x + 132
    title_font = _load_font(34)
    key_font = _load_font(16)
    value_font = _load_font(24)
    server_title = _truncate_text(draw, str(server_name), title_font, 390)
    server_address_text = _truncate_text(draw, str(server_address), key_font, 390)
    draw.text((text_x, header_top + 28), server_title, fill=TEXT, font=title_font)
    draw.text((text_x, header_top + 76), server_address_text, fill=SUB_TEXT, font=key_font)

    is_offline = offline or str(latency).strip().lower() == "offline"
    status_color = BAD_COLOR if is_offline else OK_COLOR
    draw.ellipse(
        (text_x, header_top + 123, text_x + 10, header_top + 133),
        fill=status_color,
    )
    draw.text(
        (text_x + 18, header_top + 116),
        "离线" if is_offline else "在线",
        fill=status_color,
        font=key_font,
    )

    try:
        numeric_latency = int(latency)
    except (TypeError, ValueError):
        numeric_latency = 0
    latency_text = "Offline" if is_offline else f"{numeric_latency}ms"
    divider_x = text_x + 430
    draw.line((divider_x, header_top + 24, divider_x, header_top + 142), fill=PANEL_EDGE, width=1)

    metric_font = _load_font(13)
    metric_columns = (
        (divider_x + 36, "当前延迟", latency_text, BAD_COLOR if is_offline else _latency_color(numeric_latency)),
        (divider_x + 176, "在线人数", f"{players_online}/{players_max}", TEXT),
        (divider_x + 316, "版本", str(server_version), TEXT),
    )
    for metric_x, label, value, color in metric_columns:
        draw.text((metric_x, header_top + 42), label, fill=MUTED_TEXT, font=metric_font)
        draw.text((metric_x, header_top + 75), value, fill=color, font=value_font)

    # MOTD: a full-width band keeps the server message readable before the charts.
    motd_label_font = _load_font(15)
    motd_font = _load_font(18)
    draw.rectangle(
        (PADDING + 18, motd_top + 18, PADDING + 22, motd_top + MOTD_H - 18),
        fill=LINE,
    )
    draw.text((PADDING + 38, motd_top + 18), "MOTD", fill=TEXT, font=motd_label_font)
    motd_text = (motd or "").replace("\r", " ").replace("\n", " ").strip() or "无"
    motd_text = _truncate_text(draw, motd_text, motd_font, WIDTH - PADDING * 2 - 62)
    draw.text((PADDING + 38, motd_top + 45), motd_text, fill=SUB_TEXT, font=motd_font)

    _draw_history_chart(image, draw, chart_rect, history, history_title)

    # Player panel: fixed row height keeps avatars and names aligned as the list grows.
    player_title_font = _load_font(22)
    player_font = _load_font(17)
    draw.text((player_left + 18, dashboard_top + 17), "在线玩家", fill=TEXT, font=player_title_font)
    draw.text(
        (WIDTH - PADDING - 18, dashboard_top + 23),
        str(len(players)),
        fill=MUTED_TEXT,
        font=key_font,
        anchor="ra",
    )
    draw.line(
        (player_left + 18, dashboard_top + 55, WIDTH - PADDING - 18, dashboard_top + 55),
        fill=GRID,
        width=1,
    )

    if not players:
        draw.text(
            (player_left + 20, dashboard_top + 82),
            "暂无玩家在线",
            fill=SUB_TEXT,
            font=player_font,
        )
    else:
        row_y = dashboard_top + 68
        for index, player in enumerate(players):
            _paste_avatar(image, player.get("avatar_path", ""), (player_left + 18, row_y))
            draw.text(
                (player_left + 64, row_y + 7),
                _truncate_text(draw, player.get("name", "Unknown"), player_font, PLAYER_PANEL_W - 92),
                fill=TEXT,
                font=player_font,
            )
            if index < len(players) - 1:
                draw.line(
                    (player_left + 18, row_y + PLAYER_ROW_H - 5, WIDTH - PADDING - 18, row_y + PLAYER_ROW_H - 5),
                    fill=GRID,
                    width=1,
                )
            row_y += PLAYER_ROW_H

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
