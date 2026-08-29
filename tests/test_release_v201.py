from __future__ import annotations

import unittest

from tests.helpers import PLUGIN_ROOT

from astrbot.core.star.star_handler import star_handlers_registry
from astrbot_plugin_get_mc_server_info.main import Main, PLUGIN_VERSION


class ReleaseV201Tests(unittest.TestCase):
    def test_release_versions_and_documentation_match(self) -> None:
        metadata = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(PLUGIN_VERSION, "v2.0.1")
        self.assertIn("version: v2.0.1", metadata)
        self.assertIn("v2.0.1", readme)
        self.assertIn("/备用线路", readme)
        self.assertIn("/bak", readme)
        self.assertIn("add_mc_server_backup", readme)
        self.assertIn("*_query.py", readme)
        self.assertIn("*_list.py", readme)
        self.assertIn("完全使用已有缓存", readme)

    def test_help_mentions_backup_aliases(self) -> None:
        help_message = Main._build_help_message()

        self.assertIn("/备用线路", help_message)
        self.assertIn("/备用", help_message)
        self.assertIn("/bak", help_message)

    def test_malformed_backup_aliases_reach_format_guard(self) -> None:
        handler_name = (
            f"{Main.command_help_and_format_guard.__module__}_"
            "command_help_and_format_guard"
        )
        metadata = star_handlers_registry.get_handler_by_full_name(handler_name)
        self.assertIsNotNone(metadata)
        regex_filters = [
            event_filter
            for event_filter in metadata.event_filters
            if hasattr(event_filter, "regex")
        ]

        for message in ("/备用线路 survival", "/备用 survival", "/BAK survival"):
            with self.subTest(message=message):
                self.assertTrue(
                    any(
                        event_filter.regex.search(message)
                        for event_filter in regex_filters
                    )
                )

    def test_template_package_documents_both_exports(self) -> None:
        package_doc = (PLUGIN_ROOT / "templates" / "__init__.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("render_server_report_image", package_doc)
        self.assertIn("render_server_list_image", package_doc)


if __name__ == "__main__":
    unittest.main()
