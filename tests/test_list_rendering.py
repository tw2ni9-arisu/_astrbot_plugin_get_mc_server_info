from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from tests.helpers import FakeEvent, MemoryStore, collect_results, make_plugin

SESSION_KEY = "test:GroupMessage:group-1"
PRIMARY = "main.example.com:25565"
BACKUP_A = "backup-a.example.com:25565"
BACKUP_B = "backup-b.example.com:25565"


class FakeMessageResult:
    def base64_image(self, value: str) -> dict[str, str]:
        return {"image_b64": value}


class RenderEvent(FakeEvent):
    def make_result(self) -> FakeMessageResult:
        return FakeMessageResult()


def make_list_plugin(cache_root: Path) -> tuple[Any, MemoryStore]:
    persistence = MemoryStore(
        {
            "sessions": {
                SESSION_KEY: {
                    "template": "terminal_method",
                    "servers": {
                        PRIMARY: {
                            "name": "survival",
                            "address": PRIMARY,
                            "backup_addresses": [BACKUP_A, BACKUP_B],
                            "latency_history": [],
                            "last_latency": 63,
                            "motd": "cached",
                        }
                    },
                }
            }
        }
    )
    plugin = make_plugin(cache_root=cache_root)
    plugin._load_store = persistence.load
    plugin._save_store = persistence.save
    plugin._fetch_server_status = AsyncMock(
        side_effect=AssertionError("network call forbidden")
    )
    plugin._cache_server_icon = AsyncMock(
        side_effect=AssertionError("icon download forbidden")
    )
    plugin._cache_and_collect_player_avatars = AsyncMock(
        side_effect=AssertionError("avatar download forbidden")
    )
    plugin._get_template_renderer = AsyncMock(return_value=object())
    plugin._call_template_renderer = AsyncMock(return_value="list-image")
    return plugin, persistence


class CachedListRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_renders_all_lines_from_cache_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, _ = make_list_plugin(Path(temp_dir))
            backup_icon = plugin._icon_cache_path(BACKUP_A)
            backup_icon.parent.mkdir(parents=True)
            backup_icon.write_bytes(b"cached-icon")
            event = RenderEvent(
                "/列表",
                private=True,
                session_key=SESSION_KEY,
            )

            results = await collect_results(plugin.list_servers(event))

            self.assertEqual(results, [{"image_b64": "list-image"}])
            plugin._get_template_renderer.assert_awaited_once_with(
                "terminal_method",
                mode="list",
            )
            render_args = plugin._call_template_renderer.await_args.kwargs
            self.assertEqual(render_args["mode"], "list")
            self.assertEqual(len(render_args["servers"]), 1)
            entry = render_args["servers"][0]
            self.assertEqual(entry["name"], "survival")
            self.assertEqual(entry["primary_address"], PRIMARY)
            self.assertEqual(entry["latency"], 63)
            self.assertEqual(entry["icon_path"], str(backup_icon))
            self.assertEqual(
                entry["lines"],
                [
                    {"address": PRIMARY, "line_type": "primary"},
                    {"address": BACKUP_A, "line_type": "backup"},
                    {"address": BACKUP_B, "line_type": "backup"},
                ],
            )
            plugin._fetch_server_status.assert_not_awaited()
            plugin._cache_server_icon.assert_not_awaited()
            plugin._cache_and_collect_player_avatars.assert_not_awaited()

    async def test_list_uses_template_fallback_when_no_icon_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, _ = make_list_plugin(Path(temp_dir))
            event = RenderEvent(
                "/服务器列表",
                private=True,
                session_key=SESSION_KEY,
            )

            await collect_results(plugin.list_servers(event))

            entry = plugin._call_template_renderer.await_args.kwargs["servers"][0]
            self.assertIsNone(entry["icon_path"])
            plugin._fetch_server_status.assert_not_awaited()
            plugin._cache_server_icon.assert_not_awaited()

    async def test_empty_list_keeps_plain_message_and_skips_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, persistence = make_list_plugin(Path(temp_dir))
            persistence.data["sessions"][SESSION_KEY]["servers"] = {}
            event = RenderEvent(
                "/列表",
                private=True,
                session_key=SESSION_KEY,
            )

            results = await collect_results(plugin.list_servers(event))

            self.assertEqual(results, ["当前会话暂无已添加服务器"])
            plugin._get_template_renderer.assert_not_awaited()
            plugin._call_template_renderer.assert_not_awaited()

    async def test_llm_list_data_includes_ordered_line_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, _ = make_list_plugin(Path(temp_dir))

            servers = await plugin._list_servers_data(SESSION_KEY)

            self.assertEqual(servers[0]["address"], PRIMARY)
            self.assertEqual(
                servers[0]["backup_addresses"],
                [BACKUP_A, BACKUP_B],
            )
            self.assertEqual(
                servers[0]["lines"],
                [
                    {"address": PRIMARY, "line_type": "primary"},
                    {"address": BACKUP_A, "line_type": "backup"},
                    {"address": BACKUP_B, "line_type": "backup"},
                ],
            )


if __name__ == "__main__":
    unittest.main()
