"""模板加载模块。

提供模板发现、缓存式动态导入、签名适配调用等函数。
"""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TemplateRenderer = Callable[..., Awaitable[str]]

DEFAULT_TEMPLATE_NAME = "default_method"
RENDERER_EXPORTS = {
    "query": "render_server_report_image",
    "list": "render_server_list_image",
}


@dataclass(frozen=True)
class TemplateBundle:
    """A validated query/list renderer directory."""

    name: str
    directory: Path
    query_file: Path
    list_file: Path


def discover_templates(templates_dir: Path) -> dict[str, TemplateBundle]:
    """Discover valid immediate-child template bundles by public query name."""
    bundles: dict[str, TemplateBundle] = {}
    duplicate_names: set[str] = set()
    if not templates_dir.is_dir():
        return bundles

    directories = sorted(
        (path for path in templates_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )
    for directory in directories:
        query_files = sorted(directory.glob("*_query.py"))
        list_files = sorted(directory.glob("*_list.py"))
        fonts = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() == ".ttf"
        ]
        if len(query_files) != 1 or len(list_files) != 1 or len(fonts) > 1:
            continue

        public_name = query_files[0].stem.removesuffix("_query")
        if not is_valid_template_name(public_name) or public_name in duplicate_names:
            continue
        if public_name in bundles:
            bundles.pop(public_name, None)
            duplicate_names.add(public_name)
            continue
        bundles[public_name] = TemplateBundle(
            name=public_name,
            directory=directory,
            query_file=query_files[0],
            list_file=list_files[0],
        )
    return bundles


def list_templates(templates_dir: Path) -> list[str]:
    """List validated public template names."""
    return sorted(discover_templates(templates_dir))


def is_valid_template_name(name: str) -> bool:
    """模板名合法性校验。

    只允许 Python 标识符风格，避免路径穿越和非法导入。
    """
    return bool(name) and name.isidentifier()


def template_file_path(
    templates_dir: Path,
    template_name: str,
    mode: str = "query",
) -> Path:
    """Resolve one role file from a validated template bundle."""
    if mode not in RENDERER_EXPORTS:
        raise ValueError("invalid template renderer mode")
    bundle = discover_templates(templates_dir).get(template_name)
    if bundle is None:
        raise FileNotFoundError(template_name)
    return bundle.query_file if mode == "query" else bundle.list_file


async def get_template_renderer(
    template_name: str,
    mode: str,
    templates_dir: Path,
    renderer_cache: dict[tuple[str, str], tuple[float, TemplateRenderer]],
) -> TemplateRenderer:
    """获取模板渲染函数。

    Query modules export ``render_server_report_image`` and list modules export
    ``render_server_list_image``.

    Args:
        template_name: Public template name.
        mode: ``query`` or ``list``.
        templates_dir: Root containing bundle directories.
        renderer_cache: Cache keyed by ``(template_name, mode)``.
    """
    if not is_valid_template_name(template_name):
        raise ValueError("invalid template name")
    export_name = RENDERER_EXPORTS.get(mode)
    if export_name is None:
        raise ValueError("invalid template renderer mode")

    template_file = template_file_path(templates_dir, template_name, mode)
    current_mtime = float(template_file.stat().st_mtime_ns)

    cache_key = (template_name, mode)
    cached = renderer_cache.get(cache_key)
    if cached:
        cached_mtime, cached_renderer = cached
        if cached_mtime == current_mtime:
            return cached_renderer

    packaged_templates_dir = Path(__file__).resolve().parent / "templates"
    if (
        __package__
        and template_file.parent.parent.resolve() == packaged_templates_dir.resolve()
        and template_file.parent.name.isidentifier()
    ):
        module_name = (
            f"{__package__}.templates.{template_file.parent.name}."
            f"_astrbot_dynamic_{template_file.stem}_"
            f"{template_file.stat().st_mtime_ns}"
        )
    else:
        module_name = (
            "astrbot_plugin_get_mc_server_info_template_"
            f"{template_name}_{mode}_{template_file.stat().st_mtime_ns}"
        )
    spec = importlib.util.spec_from_file_location(module_name, template_file)
    if not spec or not spec.loader:
        raise RuntimeError("cannot build module spec")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    renderer = getattr(module, export_name, None)
    if not renderer or not callable(renderer):
        raise AttributeError(f"missing {export_name}")
    if not inspect.iscoroutinefunction(renderer):
        raise TypeError(f"{export_name} must be async")

    renderer_cache[cache_key] = (current_mtime, renderer)
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
