from __future__ import annotations

import base64
import io
import unittest
from typing import Any
from unittest.mock import patch

from PIL import Image

from tests.helpers import PLUGIN_ROOT

from astrbot_plugin_get_mc_server_info.templates.default_method.default_method_list import (
    CARD_GAP,
    OUTER_PADDING,
    _card_height,
    _display_address,
    _render_server_list_image_sync,
    render_server_list_image,
)
from astrbot_plugin_get_mc_server_info.templates.terminal_method.terminal_method_list import (
    render_server_list_image as terminal_render,
)


def make_list_entry(name: str, *, line_count: int = 2) -> dict[str, Any]:
    lines = [
        {
            "address": f"{name}-{index}.example.com:25565",
            "line_type": "primary" if index == 0 else "backup",
        }
        for index in range(line_count)
    ]
    return {
        "name": name,
        "primary_address": lines[0]["address"],
        "address": lines[0]["address"],
        "line_type": "primary",
        "latency": 63,
        "players_online": 2,
        "players_max": 20,
        "version": "1.21.4",
        "offline": False,
        "icon_path": None,
        "lines": lines,
        "players": [],
    }


def decode_image(encoded: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(encoded)))


class ListRendererGeometryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_list_renderer_uses_expected_width(self) -> None:
        entries = [make_list_entry("one"), make_list_entry("two")]

        encoded = await render_server_list_image(mode="list", servers=entries)
        image = decode_image(encoded)

        self.assertEqual(image.width, 900)
        self.assertGreater(image.height, 300)

    async def test_two_cards_have_exactly_twenty_pixel_gap(self) -> None:
        entries = [
            make_list_entry("one", line_count=1),
            make_list_entry("two", line_count=4),
        ]

        encoded = await render_server_list_image(mode="list", servers=entries)
        image = decode_image(encoded)
        expected_height = (
            OUTER_PADDING * 2
            + _card_height(entries[0], "list")
            + CARD_GAP
            + _card_height(entries[1], "list")
        )

        self.assertEqual(CARD_GAP, 20)
        self.assertEqual(image.height, expected_height)

    async def test_query_all_uses_player_rows_and_empty_state(self) -> None:
        populated = make_list_entry("online")
        populated["players"] = [
            {"name": "Alex", "avatar_path": ""},
            {"name": "Steve", "avatar_path": ""},
        ]
        empty = make_list_entry("empty")

        encoded = await render_server_list_image(
            mode="query_all",
            servers=[populated, empty],
        )
        image = decode_image(encoded)

        expected_height = (
            OUTER_PADDING * 2
            + _card_height(populated, "query_all")
            + CARD_GAP
            + _card_height(empty, "query_all")
        )
        self.assertEqual(image.height, expected_height)

    def test_header_address_uses_the_line_relevant_to_each_mode(self) -> None:
        entry = make_list_entry("online")
        entry["address"] = "backup.example.com:25565"
        entry["line_type"] = "backup"

        self.assertEqual(
            _display_address(entry, "list"),
            entry["primary_address"],
        )
        self.assertEqual(
            _display_address(entry, "query_all"),
            entry["address"],
        )

    def test_list_background_is_top_aligned_to_match_reference(self) -> None:
        loader_path = (
            "astrbot_plugin_get_mc_server_info.templates.default_method."
            "default_method_list._load_template_background"
        )
        with patch(loader_path, return_value=None) as load_background:
            _render_server_list_image_sync(
                mode="list",
                servers=[make_list_entry("one")],
            )

        self.assertEqual(load_background.call_args.kwargs["centering"], (0.5, 0.0))

    async def test_terminal_adapter_reuses_renderer_at_1100_pixels(self) -> None:
        encoded = await terminal_render(
            mode="list",
            servers=[make_list_entry("one")],
        )
        image = decode_image(encoded)

        self.assertEqual(image.width, 1100)

    def test_reference_asset_is_not_required_for_geometry(self) -> None:
        self.assertTrue((PLUGIN_ROOT / "templates").is_dir())


if __name__ == "__main__":
    unittest.main()
