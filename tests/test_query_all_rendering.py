from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from tests.helpers import (
    FakeEvent,
    MemoryStore,
    collect_results,
    make_plugin,
    make_status,
)

from astrbot_plugin_get_mc_server_info.main import SavedServerQueryResult

SESSION_KEY = "test:GroupMessage:group-1"
PRIMARY = "main.example.com:25565"
BACKUP = "backup.example.com:25565"
SECOND_PRIMARY = "second.example.com:25565"
SECOND_BACKUP = "second-backup.example.com:25565"


class FakeMessageResult:
    def base64_image(self, value: str) -> dict[str, str]:
        return {"image_b64": value}


class RenderEvent(FakeEvent):
    def make_result(self) -> FakeMessageResult:
        return FakeMessageResult()


def server_record(
    name: str,
    primary: str,
    backups: list[str],
    *,
    latency: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "address": primary,
        "backup_addresses": backups,
        "latency_history": [{"timestamp": 1, "latency": latency}],
        "last_latency": latency,
        "motd": f"cached {name}",
        "last_active_query_at": 0,
        "last_silent_query_at": 0,
        "created_at": 1,
    }


def make_query_all_plugin(
    cache_root: Path,
) -> tuple[Any, MemoryStore, RenderEvent]:
    persistence = MemoryStore(
        {
            "sessions": {
                SESSION_KEY: {
                    "template": "default_method",
                    "servers": {
                        PRIMARY: server_record(
                            "survival",
                            PRIMARY,
                            [BACKUP],
                            latency=30,
                        ),
                        SECOND_PRIMARY: server_record(
                            "creative",
                            SECOND_PRIMARY,
                            [SECOND_BACKUP],
                            latency=45,
                        ),
                    },
                }
            }
        }
    )
    plugin = make_plugin(cache_root=cache_root)
    plugin.query_all_concurrency = 2
    plugin._load_store = persistence.load
    plugin._save_store = persistence.save
    plugin._get_template_renderer = AsyncMock(return_value=object())
    plugin._call_template_renderer = AsyncMock(return_value="query-all-image")
    plugin._cache_server_icon = AsyncMock()
    plugin._cache_and_collect_player_avatars = AsyncMock(
        return_value=[{"name": "Alex", "avatar_path": "alex.png"}]
    )
    plugin._cleanup_expired_cache = AsyncMock()
    event = RenderEvent(
        "/查询",
        private=True,
        session_key=SESSION_KEY,
    )
    return plugin, persistence, event


def saved_success(
    address: str,
    line_type: str,
    status: Any,
) -> SavedServerQueryResult:
    attempted = [PRIMARY, address] if address != PRIMARY else [PRIMARY]
    return SavedServerQueryResult(
        status=status,
        address=address,
        line_type=line_type,
        attempted_addresses=attempted,
    )


def saved_offline(primary: str) -> SavedServerQueryResult:
    return SavedServerQueryResult(
        status=None,
        address=primary,
        line_type="primary",
        attempted_addresses=[primary, SECOND_BACKUP],
        error="CONNECTION_FAILED",
    )


class QueryAllRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_all_renders_actual_backup_players_and_offline_card(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, persistence, event = make_query_all_plugin(Path(temp_dir))
            status = make_status(
                BACKUP,
                64,
                motd="live backup",
                players=[{"name": "Alex", "uid": "uuid-alex"}],
                icon_base64="data:image/png;base64,AAAA",
            )
            plugin._query_saved_server_lines = AsyncMock(
                side_effect=[
                    saved_success(BACKUP, "backup", status),
                    saved_offline(SECOND_PRIMARY),
                ]
            )
            second_icon = plugin._icon_cache_path(SECOND_BACKUP)
            second_icon.parent.mkdir(parents=True)
            second_icon.write_bytes(b"cached-second-icon")

            async def cache_icon(address: str, icon_base64: str | None) -> None:
                icon_path = plugin._icon_cache_path(address)
                icon_path.parent.mkdir(parents=True, exist_ok=True)
                icon_path.write_bytes(b"fresh-icon")

            plugin._cache_server_icon.side_effect = cache_icon
            before_second_history = copy.deepcopy(
                persistence.data["sessions"][SESSION_KEY]["servers"][
                    SECOND_PRIMARY
                ]["latency_history"]
            )

            result = await plugin._query_all_servers(event)

            self.assertEqual(result, {"image_b64": "query-all-image"})
            plugin._get_template_renderer.assert_awaited_once_with(
                "default_method",
                mode="list",
            )
            render_args = plugin._call_template_renderer.await_args.kwargs
            self.assertEqual(render_args["mode"], "query_all")
            self.assertEqual(len(render_args["servers"]), 2)
            online, offline = render_args["servers"]
            self.assertEqual(online["name"], "survival")
            self.assertEqual(online["primary_address"], PRIMARY)
            self.assertEqual(online["address"], BACKUP)
            self.assertEqual(online["line_type"], "backup")
            self.assertEqual(online["latency"], 64)
            self.assertEqual(online["players"][0]["name"], "Alex")
            self.assertEqual(
                online["icon_path"],
                str(plugin._icon_cache_path(BACKUP)),
            )
            self.assertFalse(online["offline"])
            self.assertEqual(offline["name"], "creative")
            self.assertEqual(offline["address"], SECOND_PRIMARY)
            self.assertEqual(offline["icon_path"], str(second_icon))
            self.assertTrue(offline["offline"])
            self.assertEqual(offline["players"], [])
            plugin._cache_server_icon.assert_awaited_once_with(
                BACKUP,
                status.icon_base64,
            )
            plugin._cache_and_collect_player_avatars.assert_awaited_once_with(
                BACKUP,
                status.players,
            )

            saved_online = persistence.data["sessions"][SESSION_KEY]["servers"][
                PRIMARY
            ]
            saved_offline_server = persistence.data["sessions"][SESSION_KEY][
                "servers"
            ][SECOND_PRIMARY]
            self.assertEqual(saved_online["last_latency"], 64)
            self.assertEqual(saved_online["motd"], "live backup")
            self.assertEqual(len(saved_online["latency_history"]), 2)
            self.assertEqual(
                saved_offline_server["latency_history"],
                before_second_history,
            )
            self.assertEqual(saved_offline_server["last_latency"], 45)

    async def test_logical_server_queries_obey_outer_concurrency_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, _, event = make_query_all_plugin(Path(temp_dir))
            plugin.query_all_concurrency = 1
            active = 0
            maximum_active = 0

            async def query_lines(
                primary_address: str,
                server_obj: dict[str, Any],
                *,
                need_players: bool,
            ) -> SavedServerQueryResult:
                nonlocal active, maximum_active
                self.assertTrue(need_players)
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0)
                active -= 1
                return SavedServerQueryResult(
                    status=make_status(primary_address, 40),
                    address=primary_address,
                    line_type="primary",
                    attempted_addresses=[primary_address],
                )

            plugin._query_saved_server_lines = AsyncMock(side_effect=query_lines)

            await plugin._query_all_servers(event)

            self.assertEqual(maximum_active, 1)
            self.assertEqual(plugin._query_saved_server_lines.await_count, 2)

    async def test_parameterless_query_handler_yields_image_result_directly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, _, event = make_query_all_plugin(Path(temp_dir))
            plugin._query_all_servers = AsyncMock(
                return_value={"image_b64": "handler-image"}
            )

            results = await collect_results(plugin.query_server(event))

            self.assertEqual(results, [{"image_b64": "handler-image"}])
            plugin._query_all_servers.assert_awaited_once_with(event)

    async def test_empty_full_query_keeps_plain_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, persistence, event = make_query_all_plugin(Path(temp_dir))
            persistence.data["sessions"][SESSION_KEY]["servers"] = {}

            result = await plugin._query_all_servers(event)

            self.assertEqual(result, "当前会话暂无已添加服务器")
            plugin._get_template_renderer.assert_not_awaited()
            plugin._call_template_renderer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
