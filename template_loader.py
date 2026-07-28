"""模板加载模块。

提供模板发现、缓存式动态导入、签名适配调用等函数。
"""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

TemplateRenderer = Callable[..., Awaitable[str]]

DEFAULT_TEMPLATE_NAME = "default_method"


def list_templates(templates_dir: Path) -> list[str]:
    """列出模板目录中的可用模板名（不带 .py）。"""
    if not templates_dir.exists():
        return []
    names: list[str] = []
    for path in templates_dir.glob("*.py"):
        if path.name == "__init__.py":
            continue
        names.append(path.stem)
    names.sort()
    return names


def is_valid_template_name(name: str) -> bool:
    """模板名合法性校验。

    只允许 Python 标识符风格，避免路径穿越和非法导入。
    """
    return bool(name) and name.isidentifier()


def template_file_path(templates_dir: Path, template_name: str) -> Path:
    """根据模板名获取模板文件路径。"""
    return templates_dir / f"{template_name}.py"


async def get_template_renderer(
    template_name: str,
    templates_dir: Path,
    renderer_cache: dict[str, tuple[float, TemplateRenderer]],
) -> TemplateRenderer:
    """获取模板渲染函数。

    约定模板文件必须提供：
        async def render_server_report_image(...)

    Args:
        template_name: 模板名称（不含 .py）
        templates_dir: 模板文件所在目录
        renderer_cache: 模板渲染函数缓存字典，键为模板名，值为 (mtime, renderer)
    """
    if not is_valid_template_name(template_name):
        raise ValueError("invalid template name")

    template_file = template_file_path(templates_dir, template_name)
    if not template_file.exists():
        raise FileNotFoundError(str(template_file))
    current_mtime = template_file.stat().st_mtime

    cached = renderer_cache.get(template_name)
    if cached:
        cached_mtime, cached_renderer = cached
        if cached_mtime == current_mtime:
            return cached_renderer

    module_name = f"astrbot_plugin_get_mc_server_info_template_{template_name}"
    spec = importlib.util.spec_from_file_location(module_name, template_file)
    if not spec or not spec.loader:
        raise RuntimeError("cannot build module spec")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    renderer = getattr(module, "render_server_report_image", None)
    if not renderer or not callable(renderer):
        raise AttributeError("missing render_server_report_image")
    if not inspect.iscoroutinefunction(renderer):
        raise TypeError("render_server_report_image must be async")

    renderer_cache[template_name] = (current_mtime, renderer)
    return renderer


async def call_template_renderer(
    renderer: TemplateRenderer,
    **kwargs: Any,
) -> str:
    """按模板函数签名过滤参数，兼容旧模板。"""
    try:
        sig = inspect.signature(renderer)
    except Exception:
        return await renderer(**kwargs)

    accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_kwargs:
        return await renderer(**kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return await renderer(**filtered)
