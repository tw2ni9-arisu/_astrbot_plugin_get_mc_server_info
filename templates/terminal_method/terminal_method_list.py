"""1100-pixel adapter for the shared default list renderer."""

from __future__ import annotations

from typing import Any

from ..default_method.default_method_list import (
    render_server_list_image as _render_default_list,
)


async def render_server_list_image(**kwargs: Any) -> str:
    kwargs["canvas_width"] = 1100
    return await _render_default_list(**kwargs)
