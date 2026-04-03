"""Default renderer template for MC server report images.

Responsibilities:
- Render one PNG image from structured data provided by `main.py`.
- Handle icon/no-icon, players/no-players, and history/no-history scenarios.
- Return base64-encoded PNG so AstrBot can send it directly as an image message.

Extension notes:
- Create a new theme by copying this file and changing layout/colors.
- Keep the async signature of `render_server_report_image(...)` unchanged.
"""

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
CHART_H = 220
PLAYER_ROW_H = 38
PLAYER_AVATAR_SIZE = 28

# 主题色
BG = (18, 22, 28)
CARD = (34, 40, 50)
TEXT = (240, 245, 255)
SUB_TEXT = (170, 182, 200)
LINE = (80, 196, 255)
GRID = (70, 80, 95)
OK_COLOR = (98, 215, 126)
WARN_COLOR = (245, 187, 87)
BAD_COLOR = (242, 103, 103)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按优先级加载常见字体，失败时回退默认字体。"""
    for name in ("arial.ttf", "msyh.ttc", "simhei.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _latency_color(latency: int) -> tuple[int, int, int]:
    """根据延迟区间返回颜色。"""
    if latency < 100:
        return OK_COLOR
    if latency < 200:
        return WARN_COLOR
    return BAD_COLOR


def _server_icon(icon_path: str | None) -> Image.Image:
    """读取服务器图标；若缺失则使用默认图标；再缺失则绘制占位图。"""
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
    """加载与模板同名的背景图。

    规则：
    - 在当前模板文件所在目录查找同名图片（忽略后缀）。
    - 支持常见格式：png/jpg/jpeg/webp/bmp。
    - 找到后等比裁剪并缩放到目标尺寸。
    """
    template_file = Path(__file__).resolve()
    stem = template_file.stem
    parent = template_file.parent
    # Resolve `<template_name>.<image_ext>` from the same folder as the template.
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
) -> None:
    """绘制历史延迟折线图区域。"""
    left, top, right, bottom = chart_rect
    draw.rounded_rectangle(chart_rect, radius=12, fill=CARD)
    title_font = _load_font(24)
    text_font = _load_font(16)
    draw.text(
        (left + 16, top + 10), "历史延迟（24h / 48点）", fill=TEXT, font=title_font
    )

    plot_left = left + 16
    plot_right = right - 16
    plot_top = top + 52
    plot_bottom = bottom - 20

    # 水平网格线
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
    lmin = min(latencies)
    lmax = max(latencies)
    if lmax == lmin:
        lmax = lmin + 1

    # 归一化并映射到像素坐标
    points: list[tuple[int, int]] = []
    n = len(latencies)
    for idx, val in enumerate(latencies):
        x = int(plot_left + (plot_right - plot_left) * idx / max(1, n - 1))
        ratio = (val - lmin) / (lmax - lmin)
        y = int(plot_bottom - ratio * (plot_bottom - plot_top))
        points.append((x, y))

    # 连线
    for i in range(1, len(points)):
        draw.line((points[i - 1], points[i]), fill=LINE, width=3)

    # 最末点高亮
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
    """粘贴玩家头像；失败时绘制默认头像。"""
    x, y = xy
    if avatar_path:
        file = Path(avatar_path)
        if file.exists():
            try:
                avatar = (
                    Image.open(file)
                    .convert("RGBA")
                    .resize((PLAYER_AVATAR_SIZE, PLAYER_AVATAR_SIZE))
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
) -> str:
    """生成服务器信息图并返回 base64 编码。

    Args:
        server_name: 展示用服务器名
        server_address: 地址文本
        latency: 当前延迟（ms）
        players_online: 在线人数
        players_max: 最大人数
        server_version: 服务端版本
        history: 历史延迟点列表（每项包含 timestamp/latency）
        icon_path: 缓存图标路径，可为空
        players: 在线玩家列表（name/avatar_path）
    """
    # 根据在线玩家数量动态计算玩家区域高度，避免内容截断
    player_section_h = max(160, 56 + max(1, len(players)) * PLAYER_ROW_H)
    total_h = PADDING * 3 + HEADER_H + CHART_H + player_section_h

    # 优先使用模板同名背景图；找不到时使用纯色背景
    img = _load_template_background(WIDTH, total_h) or Image.new(
        "RGBA", (WIDTH, total_h), BG
    )
    draw = ImageDraw.Draw(img)

    title_font = _load_font(32)
    key_font = _load_font(20)
    value_font = _load_font(22)
    player_font = _load_font(18)

    # 顶部信息卡片：服务器名、地址、当前延迟、在线人数
    header_rect = (PADDING, PADDING, WIDTH - PADDING, PADDING + HEADER_H)
    draw.rounded_rectangle(header_rect, radius=14, fill=CARD)
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
    draw.text(
        (WIDTH - 170, PADDING + 22),
        "在线人数",
        fill=SUB_TEXT,
        font=key_font,
    )
    draw.text(
        (WIDTH - 170, PADDING + 54),
        f"{players_online}/{players_max}",
        fill=TEXT,
        font=value_font,
    )

    draw.text(
        (text_x, PADDING + 90), f"版本: {server_version}", fill=SUB_TEXT, font=key_font
    )

    # 中部历史图区域
    chart_top = PADDING * 2 + HEADER_H
    chart_rect = (PADDING, chart_top, WIDTH - PADDING, chart_top + CHART_H)
    _draw_history_chart(draw, chart_rect, history)

    # 底部玩家列表区域
    players_top = chart_top + CHART_H + PADDING
    player_rect = (PADDING, players_top, WIDTH - PADDING, total_h - PADDING)
    draw.rounded_rectangle(player_rect, radius=14, fill=CARD)
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

    # 导出 PNG 并编码为 base64，供消息组件直接发送
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
