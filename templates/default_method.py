"""Default renderer template for MC server report images. (Fixed Version)"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

# 画布与布局参数
WIDTH = 900
PADDING = 24
HEADER_H = 120
MOTD_H = 72
CHART_H = 220
PLAYER_ROW_H = 38
PLAYER_AVATAR_SIZE = 28

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


def _draw_history_chart(
    draw: ImageDraw.ImageDraw,
    chart_rect: tuple[int, int, int, int],
    history: list[dict[str, Any]],
    history_title: str,
) -> None:
    left, top, right, bottom = chart_rect
    # 修复：不再此处绘制圆角矩形，已统一在主函数的 overlay 图层处理
    title_font = _load_font(24)
    text_font = _load_font(16)
    draw.text((left + 16, top + 10), history_title, fill=TEXT, font=title_font)

    plot_left, plot_right = left + 16, right - 16
    plot_top, plot_bottom = top + 52, bottom - 20

    for i in range(5):
        y = int(plot_top + (plot_bottom - plot_top) * i / 4)
        draw.line((plot_left, y, plot_right, y), fill=GRID, width=1)

    if not history:
        draw.text(
            (plot_left + 10, plot_top + 20),
            "暂无延迟数据",
            fill=SUB_TEXT,
            font=text_font,
        )
        return

    latencies = [max(0, int(point.get("latency", 0))) for point in history]
    lmin, lmax = min(latencies), max(latencies)
    if lmax == lmin:
        lmax = lmin + 1

    points = []
    n = len(latencies)
    for idx, val in enumerate(latencies):
        x = int(plot_left + (plot_right - plot_left) * idx / max(1, n - 1))
        ratio = (val - lmin) / (lmax - lmin)
        y = int(plot_bottom - ratio * (plot_bottom - plot_top))
        points.append((x, y))

    for i in range(1, len(points)):
        draw.line((points[i - 1], points[i]), fill=LINE, width=3)
    if points:
        draw.ellipse(
            (
                points[-1][0] - 4,
                points[-1][1] - 4,
                points[-1][0] + 4,
                points[-1][1] + 4,
            ),
            fill=LINE,
        )

    draw.text(
        (plot_left, plot_top - 18),
        f"max: {max(latencies)}ms",
        fill=SUB_TEXT,
        font=text_font,
    )
    draw.text(
        (plot_left + 160, plot_top - 18),
        f"min: {min(latencies)}ms",
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


async def render_server_report_image(
    *,
    server_name: str,
    server_address: str,
    latency: int,
    players_online: int,
    players_max: int,
    server_version: str,
    history: list[dict[str, Any]],
    icon_path: str | None,
    players: list[dict[str, str]],
    motd: str = "",
    history_title: str = "历史延迟",
) -> str:
    # 1. 动态计算高度
    player_section_h = max(160, 56 + len(players) * PLAYER_ROW_H + 20)
    # 修复：PADDING * 4，确保底部有足够的留白空间
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
    draw.text(
        (WIDTH - 300, PADDING + 54),
        f"{latency}ms",
        fill=_latency_color(latency),
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
    _draw_history_chart(draw, chart_rect, history, history_title)

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
