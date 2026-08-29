from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import PLUGIN_ROOT

from astrbot_plugin_get_mc_server_info import template_loader


def write_renderer(
    path: Path,
    function_name: str,
    *,
    result: str = "ok",
) -> None:
    path.write_text(
        f"async def {function_name}(**kwargs):\n"
        f"    return {result!r}\n",
        encoding="utf-8",
    )


def make_bundle(
    root: Path,
    *,
    directory_name: str = "bundle",
    public_name: str = "ocean",
) -> Path:
    bundle_dir = root / directory_name
    bundle_dir.mkdir()
    write_renderer(
        bundle_dir / f"{public_name}_query.py",
        "render_server_report_image",
    )
    write_renderer(
        bundle_dir / "cards_list.py",
        "render_server_list_image",
    )
    return bundle_dir


class TemplateBundleDiscoveryTests(unittest.TestCase):
    def test_discovers_valid_bundle_under_query_public_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_dir = make_bundle(
                root,
                directory_name="folder-name-does-not-control-public-name",
                public_name="ocean",
            )

            bundles = template_loader.discover_templates(root)

            self.assertEqual(list(bundles), ["ocean"])
            self.assertEqual(bundles["ocean"].directory, bundle_dir)
            self.assertEqual(bundles["ocean"].query_file.name, "ocean_query.py")
            self.assertEqual(bundles["ocean"].list_file.name, "cards_list.py")
            self.assertEqual(template_loader.list_templates(root), ["ocean"])

    def test_rejects_missing_or_duplicate_roles_and_multiple_fonts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            missing_list = root / "missing-list"
            missing_list.mkdir()
            write_renderer(
                missing_list / "missing_query.py",
                "render_server_report_image",
            )

            duplicate_query = root / "duplicate-query"
            duplicate_query.mkdir()
            write_renderer(
                duplicate_query / "first_query.py",
                "render_server_report_image",
            )
            write_renderer(
                duplicate_query / "second_query.py",
                "render_server_report_image",
            )
            write_renderer(
                duplicate_query / "cards_list.py",
                "render_server_list_image",
            )

            multiple_fonts = root / "multiple-fonts"
            multiple_fonts.mkdir()
            write_renderer(
                multiple_fonts / "fonted_query.py",
                "render_server_report_image",
            )
            write_renderer(
                multiple_fonts / "cards_list.py",
                "render_server_list_image",
            )
            multiple_fonts.joinpath("first.ttf").write_bytes(b"first")
            multiple_fonts.joinpath("second.TTF").write_bytes(b"second")

            self.assertEqual(template_loader.discover_templates(root), {})

    def test_shipped_bundles_keep_existing_public_names(self) -> None:
        bundles = template_loader.discover_templates(PLUGIN_ROOT / "templates")

        self.assertEqual(list(bundles), ["default_method", "terminal_method"])
        self.assertEqual(
            bundles["default_method"].query_file.name,
            "default_method_query.py",
        )
        self.assertEqual(
            bundles["terminal_method"].list_file.name,
            "terminal_method_list.py",
        )


class TemplateBundleLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_modes_require_their_role_specific_async_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_dir = make_bundle(root)
            cache: dict[tuple[str, str], tuple[float, object]] = {}

            query_renderer = await template_loader.get_template_renderer(
                "ocean",
                "query",
                root,
                cache,
            )
            list_renderer = await template_loader.get_template_renderer(
                "ocean",
                "list",
                root,
                cache,
            )

            self.assertEqual(await query_renderer(), "ok")
            self.assertEqual(await list_renderer(), "ok")
            self.assertEqual(set(cache), {("ocean", "query"), ("ocean", "list")})

            write_renderer(
                bundle_dir / "ocean_query.py",
                "wrong_query_export",
            )
            os.utime(
                bundle_dir / "ocean_query.py",
                (2_000_000_000, 2_000_000_000),
            )
            with self.assertRaises(AttributeError):
                await template_loader.get_template_renderer(
                    "ocean",
                    "query",
                    root,
                    cache,
                )

            write_renderer(
                bundle_dir / "cards_list.py",
                "wrong_list_export",
            )
            os.utime(
                bundle_dir / "cards_list.py",
                (2_000_000_000, 2_000_000_000),
            )
            with self.assertRaises(AttributeError):
                await template_loader.get_template_renderer(
                    "ocean",
                    "list",
                    root,
                    cache,
                )

    async def test_changed_renderer_mtime_reloads_module(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle_dir = make_bundle(root)
            query_file = bundle_dir / "ocean_query.py"
            write_renderer(
                query_file,
                "render_server_report_image",
                result="first",
            )
            cache: dict[tuple[str, str], tuple[float, object]] = {}
            first_renderer = await template_loader.get_template_renderer(
                "ocean",
                "query",
                root,
                cache,
            )

            write_renderer(
                query_file,
                "render_server_report_image",
                result="second",
            )
            os.utime(query_file, (2_000_000_000, 2_000_000_000))
            second_renderer = await template_loader.get_template_renderer(
                "ocean",
                "query",
                root,
                cache,
            )

            self.assertEqual(await first_renderer(), "first")
            self.assertEqual(await second_renderer(), "second")
            self.assertIsNot(first_renderer, second_renderer)

    async def test_invalid_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_bundle(root)

            with self.assertRaises(ValueError):
                await template_loader.get_template_renderer(
                    "ocean",
                    "unknown",
                    root,
                    {},
                )


if __name__ == "__main__":
    unittest.main()
