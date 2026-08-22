"""Default renderer template for MC server report images. (Fixed Version)"""

from __future__ import annotations

import asyncio
import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

# 画布与布局参数
WIDTH = 900
PADDING = 24
HEADER_H = 120
MOTD_H = 72
CHART_H = 232
PLAYER_ROW_H = 38
PLAYER_AVATAR_SIZE = 28
MAX_RENDERED_PLAYERS = 50
CHART_GRID_COUNT = 5
HOUR_SECONDS = 60 * 60
TIME_TICK_FONT_SIZE = 12
OUTAGE_LINE = (166, 182, 200)  # #A6B6C8
OUTAGE_FILL = (211, 219, 230)  # #D3DBE6
OUTAGE_LINE_WIDTH = 2
OUTAGE_DASH_LENGTH = 6
OUTAGE_DASH_GAP = 4

# 主题色
BG = (18, 22, 28)
CARD_ALPHA = (214, 214, 214, 40)  # 保留 15.7% 不透明度
TEXT = (240, 245, 255)
SUB_TEXT = (170, 182, 200)
LINE = (80, 196, 255)
GRID = (70, 80, 95)
OK_COLOR = (98, 215, 126)
WARN_COLOR = (245, 187, 87)
BAD_COLOR = (242, 103, 103)
CUSTOM_FONT_EXTENSIONS = (".ttf", ".ttc", ".otf")
_CUSTOM_FONT_PATHS: list[Path] | None = None


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


def _list_custom_fonts() -> list[Path]:
    global _CUSTOM_FONT_PATHS
    if _CUSTOM_FONT_PATHS is not None:
        return _CUSTOM_FONT_PATHS
    template_dir = Path(__file__).resolve().parent
    font_paths: list[Path] = []
    for ext in CUSTOM_FONT_EXTENSIONS:
        font_paths.extend(sorted(template_dir.glob(f"*{ext}")))
    _CUSTOM_FONT_PATHS = font_paths
    return font_paths


def _latency_color(latency: int) -> tuple[int, int, int]:
    if latency < 100:
        return OK_COLOR
    if latency < 200:
        return WARN_COLOR
    return BAD_COLOR


def _server_icon(icon_path: str | None) -> Image.Image:
    default_icon_path = Path(__file__).resolve().parent / "default_icon.png"
    for file_path in (Path(icon_path) if icon_path else None, default_icon_path):
        if file_path and file_path.exists():
            try:
                icon = Image.open(file_path).convert("RGBA")
                return icon.resize((80, 80))
            except OSError:
                pass
    icon = Image.new("RGBA", (80, 80), (48, 92, 170, 255))
    d = ImageDraw.Draw(icon)
    d.rounded_rectangle((4, 4, 76, 76), radius=14, outline=(160, 205, 255), width=2)
    d.text((26, 24), "MC", fill=TEXT, font=_load_font(24))
    return icon


def _load_template_background(width: int, height: int) -> Image.Image | None:
    template_file = Path(__file__).resolve()
    stem = template_file.stem
    parent = template_file.parent
    for ext in ("png", "jpg", "jpeg", "webp", "bmp"):
        candidate = parent / f"{stem}.{ext}"
        if not candidate.exists():
            continue
        try:
            img = Image.open(candidate).convert("RGBA")
            return ImageOps.fit(img, (width, height), method=Image.Resampling.LANCZOS)
        except OSError:
            continue
    return None


def _next_twenty(value: int) -> int:
    """返回严格大于 value 的最小 20 的倍数。"""
    return max(20, (value // 20 + 1) * 20)


def _calculate_y_axis_max(latencies: list[int]) -> int:
    """按延迟平均值和尖峰计算纵轴上限。"""
    if not latencies:
        return 20

    max_latency = max(latencies)
    average_latency = sum(latencies) / len(latencies)
    if max_latency <= average_latency + 20:
        return _next_twenty(max_latency)

    other_latencies = latencies.copy()
    other_latencies.remove(max_latency)
    if not other_latencies:
        return _next_twenty(max_latency)
    return _next_twenty(max(other_latencies))


def _non_zero_latencies(latencies: list[int]) -> list[int]:
    """返回用于最小值比较和纵轴计算的有效延迟。"""
    return [latency for latency in latencies if latency > 0]


def _find_zero_ranges(latencies: list[int]) -> list[tuple[int, int]]:
    """查找连续的断连/缺失区间。"""
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
    """绘制断连边界的竖向虚线。"""
    step = OUTAGE_DASH_LENGTH + OUTAGE_DASH_GAP
    for y in range(top, bottom + 1, step):
        draw.line(
            (x, y, x, min(y + OUTAGE_DASH_LENGTH - 1, bottom)),
            fill=OUTAGE_LINE,
            width=OUTAGE_LINE_WIDTH,
        )


def _draw_outage_background(
    image: Image.Image | None,
    latencies: list[int],
    plot_left: int,
    plot_top: int,
    plot_right: int,
    plot_bottom: int,
) -> list[tuple[int, int, bool, bool]]:
    """填充断连区间并返回其边界，供后续绘制虚线。"""
    if image is None or not _non_zero_latencies(latencies):
        return []

    point_count = len(latencies)
    point_width = plot_right - plot_left

    def point_x(index: int) -> int:
        return int(plot_left + point_width * index / max(1, point_count - 1))

    boundaries: list[tuple[int, int, bool, bool]] = []
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    plot_height = max(1, plot_bottom - plot_top)

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

        for y in range(plot_top, plot_bottom + 1):
            alpha = int(255 * 0.5 * (plot_bottom - y) / plot_height)
            overlay_draw.line(
                (left_x, y, right_x, y),
                fill=(*OUTAGE_FILL, alpha),
                width=1,
            )
        boundaries.append((left_x, right_x, start > 0, end < point_count - 1))

    if boundaries:
        image.alpha_composite(overlay)
    return boundaries


def _floor_to_half_hour(timestamp: int) -> datetime:
    """将时间向下取整到整点或半点。"""
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
    """生成从查询时间向左按小时排列的时间刻度。"""
    timestamps: list[int] = []
    for point in history:
        try:
            timestamp = int(point.get("timestamp", 0) or 0)
        except Exception:
            continue
        if timestamp > 0:
            timestamps.append(timestamp)
    if not timestamps:
        return []

    latest_tick = int(_floor_to_half_hour(max(timestamps)).timestamp())
    earliest_timestamp = min(timestamps)
    tick_count = max(1, (latest_tick - earliest_timestamp) // HOUR_SECONDS + 1)
    width = plot_right - plot_left
    ticks: list[tuple[int, str]] = []
    for index in range(tick_count):
        timestamp = latest_tick - index * HOUR_SECONDS
        x = int(plot_right - width * index / max(1, tick_count - 1))
        label = datetime.fromtimestamp(timestamp).strftime("%H:%M")
        ticks.append((x, label))
    return ticks


def _draw_history_chart(
    draw: ImageDraw.ImageDraw,
    chart_rect: tuple[int, int, int, int],
    history: list[dict[str, Any]],
    history_title: str,
    image: Image.Image | None = None,
) -> None:
    left, top, right, bottom = chart_rect
    # 修复：不再此处绘制圆角矩形，已统一在主函数的 overlay 图层处理
    title_font = _load_font(24)
    text_font = _load_font(16)
    time_font = _load_font(TIME_TICK_FONT_SIZE)
    draw.text((left + 16, top + 10), history_title, fill=TEXT, font=title_font)

    plot_left, plot_right = left + 64, right - 16
    plot_top, plot_bottom = top + 52, bottom - 48

    latencies = [max(0, int(point.get("latency", 0))) for point in history]
    observed_latencies = _non_zero_latencies(latencies)
    y_axis_max = _calculate_y_axis_max(observed_latencies or latencies)
    image = image or getattr(draw, "_image", None)
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
        draw.line((plot_left - 4, y, plot_left, y), fill=GRID, width=1)
        if value != 0:
            draw.text(
                (plot_left - 8, y),
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
            (x, plot_bottom + 4 + (index % 2) * 18),
            label,
            fill=SUB_TEXT,
            font=time_font,
            anchor="mt",
        )

    if not history:
        draw.text(
            (plot_left + 10, plot_top + 20),
            "暂无延迟数据",
            fill=SUB_TEXT,
            font=text_font,
        )
        return

    lmin = min(observed_latencies) if observed_latencies else 0
    lmax = max(latencies)

    points = []
    n = len(latencies)
    for idx, val in enumerate(latencies):
        x = int(plot_left + (plot_right - plot_left) * idx / max(1, n - 1))
        ratio = min(1, max(0, val / y_axis_max))
        y = int(plot_bottom - ratio * (plot_bottom - plot_top))
        points.append((x, y))

    for i in range(1, len(points)):
        if latencies[i - 1] > 0 and latencies[i] > 0:
            draw.line((points[i - 1], points[i]), fill=LINE, width=3)
    if points and latencies[-1] > 0:
        draw.ellipse(
            (
                points[-1][0] - 4,
                points[-1][1] - 4,
                points[-1][0] + 4,
                points[-1][1] + 4,
            ),
            fill=LINE,
        )

    for left_x, right_x, has_left_boundary, has_right_boundary in outage_boundaries:
        if has_left_boundary:
            _draw_dashed_vertical_line(draw, left_x, plot_top, plot_bottom)
        if has_right_boundary:
            _draw_dashed_vertical_line(draw, right_x, plot_top, plot_bottom)

    draw.text(
        (plot_left, plot_top - 18),
        f"max: {max(latencies)}ms",
        fill=SUB_TEXT,
        font=text_font,
    )
    draw.text(
        (plot_left + 160, plot_top - 18),
        f"min: {lmin}ms",
        fill=SUB_TEXT,
        font=text_font,
    )


def _paste_avatar(img: Image.Image, avatar_path: str, xy: tuple[int, int]) -> None:
    x, y = xy
    if avatar_path:
        file = Path(avatar_path)
        if file.exists():
            try:
                avatar = (
                    Image.open(file)
                    .convert("RGBA")
                    .resize(
                        (PLAYER_AVATAR_SIZE, PLAYER_AVATAR_SIZE),
                        Image.Resampling.NEAREST,
                    )
                )
                img.paste(avatar, (x, y), avatar)
                return
            except OSError:
                pass
    fallback = Image.new(
        "RGBA", (PLAYER_AVATAR_SIZE, PLAYER_AVATAR_SIZE), (84, 94, 110, 255)
    )
    d = ImageDraw.Draw(fallback)
    d.ellipse((8, 6, 20, 18), fill=(190, 200, 220, 255))
    d.rectangle((7, 18, 21, 27), fill=(190, 200, 220, 255))
    img.paste(fallback, (x, y), fallback)


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
    # 1. 动态计算高度
    player_section_h = max(160, 56 + len(players) * PLAYER_ROW_H + 20)
    # 保留底部留白，并为图表的新增坐标刻度预留高度
    total_h = PADDING * 5 + HEADER_H + MOTD_H + CHART_H + player_section_h

    # 2. 准备底图
    bg_img = _load_template_background(WIDTH, total_h)
    if bg_img is None:
        bg_img = Image.new("RGBA", (WIDTH, total_h), BG)
    else:
        bg_img = bg_img.convert("RGBA")

    # 3. [修复核心]：使用 overlay 图层处理 Alpha 混合
    overlay = Image.new("RGBA", (WIDTH, total_h), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    # 绘制所有半透明背景框到 overlay
    header_rect = (PADDING, PADDING, WIDTH - PADDING, PADDING + HEADER_H)
    overlay_draw.rounded_rectangle(header_rect, radius=14, fill=CARD_ALPHA)

    motd_top = PADDING * 2 + HEADER_H
    motd_rect = (PADDING, motd_top, WIDTH - PADDING, motd_top + MOTD_H)
    overlay_draw.rounded_rectangle(motd_rect, radius=12, fill=CARD_ALPHA)

    chart_top = motd_top + MOTD_H + PADDING
    chart_rect = (PADDING, chart_top, WIDTH - PADDING, chart_top + CHART_H)
    overlay_draw.rounded_rectangle(chart_rect, radius=12, fill=CARD_ALPHA)

    players_top = chart_top + CHART_H + PADDING
    player_rect = (PADDING, players_top, WIDTH - PADDING, total_h - PADDING)
    overlay_draw.rounded_rectangle(player_rect, radius=14, fill=CARD_ALPHA)

    # 将 overlay 复合到背景图上
    img = Image.alpha_composite(bg_img, overlay)
    draw = ImageDraw.Draw(img)

    # 4. 绘制内容
    title_font, key_font = _load_font(32), _load_font(20)
    value_font, player_font = _load_font(22), _load_font(18)

    # Header 内容
    icon = _server_icon(icon_path)
    img.paste(icon, (PADDING + 16, PADDING + 20), icon)
    text_x = PADDING + 120
    draw.text((text_x, PADDING + 18), server_name, fill=TEXT, font=title_font)
    draw.text((text_x, PADDING + 60), server_address, fill=SUB_TEXT, font=key_font)
    draw.text((WIDTH - 300, PADDING + 22), "当前延迟", fill=SUB_TEXT, font=key_font)
    is_offline = offline or str(latency).strip().lower() == "offline"
    latency_text = "Offline" if is_offline else f"{latency}ms"
    latency_color = BAD_COLOR if is_offline else _latency_color(int(latency))
    draw.text(
        (WIDTH - 300, PADDING + 54),
        latency_text,
        fill=latency_color,
        font=value_font,
    )
    draw.text((WIDTH - 170, PADDING + 22), "在线人数", fill=SUB_TEXT, font=key_font)
    draw.text(
        (WIDTH - 170, PADDING + 54),
        f"{players_online}/{players_max}",
        fill=TEXT,
        font=value_font,
    )
    draw.text(
        (text_x, PADDING + 90), f"版本: {server_version}", fill=SUB_TEXT, font=key_font
    )

    # Motd 信息
    motd_title_font = _load_font(20)
    motd_font = _load_font(16)
    draw.text((PADDING + 16, motd_top + 12), "Motd", fill=TEXT, font=motd_title_font)
    motd_text = (motd or "").replace("\r", " ").replace("\n", " ").strip()
    if not motd_text:
        motd_text = "无"
    max_chars = 88
    if len(motd_text) > max_chars:
        motd_text = motd_text[: max_chars - 3] + "..."
    draw.text((PADDING + 16, motd_top + 40), motd_text, fill=SUB_TEXT, font=motd_font)

    # 图表内容
    _draw_history_chart(draw, chart_rect, history, history_title, image=img)

    # 玩家列表内容
    draw.text(
        (PADDING + 16, players_top + 12), "在线玩家", fill=TEXT, font=_load_font(24)
    )
    y = players_top + 52
    if not players:
        draw.text((PADDING + 20, y), "暂无玩家在线", fill=SUB_TEXT, font=player_font)
    else:
        for player in players:
            _paste_avatar(img, player.get("avatar_path", ""), (PADDING + 16, y + 2))
            draw.text(
                (PADDING + 54, y + 6),
                player.get("name", "Unknown"),
                fill=TEXT,
                font=player_font,
            )
            y += PLAYER_ROW_H

    # 5. 导出
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
