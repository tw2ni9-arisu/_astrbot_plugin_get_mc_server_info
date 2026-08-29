from __future__ import annotations

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

from astrbot_plugin_get_mc_server_info.main import (
    McServerConnectionError,
    McServerTimeoutError,
)

SESSION_KEY = "test:GroupMessage:group-1"
PRIMARY = "main.example.com:25565"
BACKUP_A = "backup-a.example.com:25565"
BACKUP_B = "backup-b.example.com:25565"


class FakeMessageResult:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def message(self, value: str) -> FakeMessageResult:
        self.messages.append(value)
        return self

    def base64_image(self, value: str) -> dict[str, Any]:
        return {"image_b64": value, "messages": list(self.messages)}


class RenderEvent(FakeEvent):
    def make_result(self) -> FakeMessageResult:
        return FakeMessageResult()


def make_managed_plugin(
    *,
    cache_root: Path | None = None,
) -> tuple[Any, MemoryStore]:
    persistence = MemoryStore(
        {
            "sessions": {
                SESSION_KEY: {
                    "template": "default_method",
                    "servers": {
                        PRIMARY: {
                            "name": "survival",
                            "address": PRIMARY,
                            "backup_addresses": [BACKUP_A, BACKUP_B],
                            "latency_history": [
                                {"timestamp": 1, "latency": 30}
                            ],
                            "last_latency": 30,
                            "motd": "cached motd",
                            "last_silent_query_at": 0,
                            "last_active_query_at": 0,
                            "created_at": 1,
                        }
                    },
                }
            }
        }
    )
    plugin = make_plugin(cache_root=cache_root)
    plugin.query_result_cache_ttl_seconds = 10
    plugin._load_store = persistence.load
    plugin._save_store = persistence.save
    plugin._cache_server_icon = AsyncMock()
    plugin._cache_and_collect_player_avatars = AsyncMock(return_value=[])
    plugin._cleanup_expired_cache = AsyncMock()
    return plugin, persistence


class SavedServerFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success_does_not_touch_backups(self) -> None:
        plugin = make_plugin()
        primary_status = make_status(PRIMARY, 31)
        plugin._fetch_server_status = AsyncMock(return_value=primary_status)

        result = await plugin._query_saved_server_lines(
            PRIMARY,
            {"backup_addresses": [BACKUP_A, BACKUP_B]},
            need_players=True,
        )

        self.assertIs(result.status, primary_status)
        self.assertEqual(result.address, PRIMARY)
        self.assertEqual(result.line_type, "primary")
        self.assertEqual(result.attempted_addresses, [PRIMARY])
        plugin._fetch_server_status.assert_awaited_once_with(
            PRIMARY,
            need_players=True,
        )

    async def test_backups_are_tried_in_insertion_order(self) -> None:
        plugin = make_plugin()
        backup_status = make_status(BACKUP_B, 72)
        plugin._fetch_server_status = AsyncMock(
            side_effect=[
                McServerConnectionError(),
                McServerTimeoutError(),
                backup_status,
            ]
        )

        result = await plugin._query_saved_server_lines(
            PRIMARY,
            {"backup_addresses": [BACKUP_A, BACKUP_B]},
            need_players=False,
        )

        self.assertIs(result.status, backup_status)
        self.assertEqual(result.address, BACKUP_B)
        self.assertEqual(result.line_type, "backup")
        self.assertEqual(
            result.attempted_addresses,
            [PRIMARY, BACKUP_A, BACKUP_B],
        )
        self.assertEqual(
            [call.args[0] for call in plugin._fetch_server_status.await_args_list],
            [PRIMARY, BACKUP_A, BACKUP_B],
        )

    async def test_all_lines_failed_returns_offline_result(self) -> None:
        plugin = make_plugin()
        plugin._fetch_server_status = AsyncMock(
            side_effect=McServerConnectionError()
        )

        result = await plugin._query_saved_server_lines(
            PRIMARY,
            {"backup_addresses": [BACKUP_A]},
            need_players=False,
        )

        self.assertIsNone(result.status)
        self.assertEqual(result.address, PRIMARY)
        self.assertEqual(result.line_type, "primary")
        self.assertEqual(result.error, "CONNECTION_FAILED")
        self.assertEqual(result.attempted_addresses, [PRIMARY, BACKUP_A])


class QueryFailoverIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_name_primary_and_backup_resolve_same_logical_server(
        self,
    ) -> None:
        for query_token in ("survival", PRIMARY, BACKUP_A):
            with self.subTest(query_token=query_token):
                plugin, persistence = make_managed_plugin()
                backup_status = make_status(BACKUP_A, 61, motd="live motd")

                async def fetch(address: str, *, need_players: bool):
                    if address == PRIMARY:
                        raise McServerConnectionError()
                    return backup_status

                plugin._fetch_server_status = AsyncMock(side_effect=fetch)

                result = await plugin._query_server_data(
                    SESSION_KEY,
                    query_token,
                    allow_direct=False,
                )

                self.assertTrue(result["ok"])
                self.assertTrue(result["managed"])
                self.assertEqual(result["server"], "survival")
                self.assertEqual(result["primary_address"], PRIMARY)
                self.assertEqual(result["address"], BACKUP_A)
                self.assertEqual(result["line_type"], "backup")
                self.assertEqual(
                    result["attempted_addresses"],
                    [PRIMARY, BACKUP_A],
                )
                self.assertEqual(
                    [
                        call.args[0]
                        for call in plugin._fetch_server_status.await_args_list
                    ],
                    [PRIMARY, BACKUP_A],
                )
                plugin._cache_server_icon.assert_awaited_once_with(
                    BACKUP_A,
                    None,
                )
                server = persistence.data["sessions"][SESSION_KEY]["servers"][
                    PRIMARY
                ]
                self.assertEqual(server["last_latency"], 61)
                self.assertEqual(server["motd"], "live motd")
                self.assertEqual(len(server["latency_history"]), 2)

    async def test_tool_all_failed_returns_line_metadata(self) -> None:
        plugin, persistence = make_managed_plugin()
        before = copy.deepcopy(persistence.data)
        plugin._fetch_server_status = AsyncMock(
            side_effect=[
                McServerConnectionError(),
                McServerTimeoutError(),
                McServerTimeoutError(),
            ]
        )

        result = await plugin._query_server_data(
            SESSION_KEY,
            "survival",
            allow_direct=False,
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["online"])
        self.assertEqual(result["error"], "CONNECTION_TIMEOUT")
        self.assertEqual(result["primary_address"], PRIMARY)
        self.assertEqual(result["address"], PRIMARY)
        self.assertEqual(result["line_type"], "primary")
        self.assertEqual(
            result["attempted_addresses"],
            [PRIMARY, BACKUP_A, BACKUP_B],
        )
        self.assertEqual(persistence.data, before)

    async def test_command_backup_address_uses_logical_primary(self) -> None:
        plugin, _ = make_managed_plugin()
        plugin._query_single_server = AsyncMock(return_value="rendered")
        plugin._query_direct_address = AsyncMock(return_value="direct")
        event = FakeEvent(
            f"/查询 {BACKUP_A}",
            private=True,
            session_key=SESSION_KEY,
        )

        results = await collect_results(plugin.query_server(event))

        self.assertEqual(results, ["rendered"])
        plugin._query_single_server.assert_awaited_once_with(event, PRIMARY)
        plugin._query_direct_address.assert_not_awaited()

    async def test_single_query_renders_actual_backup_and_updates_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, persistence = make_managed_plugin(
                cache_root=Path(temp_dir)
            )
            backup_status = make_status(BACKUP_A, 58, motd="backup live")
            plugin._fetch_server_status = AsyncMock(
                side_effect=[McServerConnectionError(), backup_status]
            )
            plugin._get_template_renderer = AsyncMock(return_value=object())
            plugin._call_template_renderer = AsyncMock(return_value="image-data")
            event = RenderEvent(
                "",
                private=True,
                session_key=SESSION_KEY,
            )

            result = await plugin._query_single_server(event, PRIMARY)

            self.assertEqual(result["image_b64"], "image-data")
            render_args = plugin._call_template_renderer.await_args.kwargs
            self.assertEqual(render_args["server_address"], BACKUP_A)
            self.assertEqual(render_args["latency"], 58)
            self.assertFalse(render_args.get("offline", False))
            plugin._cache_server_icon.assert_awaited_once_with(BACKUP_A, None)
            plugin._cache_and_collect_player_avatars.assert_awaited_once_with(
                BACKUP_A,
                backup_status.players,
            )
            server = persistence.data["sessions"][SESSION_KEY]["servers"][
                PRIMARY
            ]
            self.assertEqual(server["last_latency"], 58)
            self.assertEqual(len(server["latency_history"]), 2)

    async def test_all_failed_single_query_is_offline_without_saved_zero(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, persistence = make_managed_plugin(
                cache_root=Path(temp_dir)
            )
            plugin._fetch_server_status = AsyncMock(
                side_effect=McServerConnectionError()
            )
            plugin._get_template_renderer = AsyncMock(return_value=object())
            plugin._call_template_renderer = AsyncMock(return_value="offline-image")
            backup_icon = plugin._icon_cache_path(BACKUP_A)
            backup_icon.parent.mkdir(parents=True)
            backup_icon.write_bytes(b"icon")
            before_history = copy.deepcopy(
                persistence.data["sessions"][SESSION_KEY]["servers"][PRIMARY][
                    "latency_history"
                ]
            )
            event = RenderEvent(
                "",
                private=True,
                session_key=SESSION_KEY,
            )

            result = await plugin._query_single_server(event, PRIMARY)

            self.assertEqual(result["image_b64"], "offline-image")
            render_args = plugin._call_template_renderer.await_args.kwargs
            self.assertTrue(render_args["offline"])
            self.assertEqual(render_args["server_address"], PRIMARY)
            self.assertEqual(render_args["latency"], "Offline")
            self.assertEqual(render_args["icon_path"], str(backup_icon))
            after_history = persistence.data["sessions"][SESSION_KEY]["servers"][
                PRIMARY
            ]["latency_history"]
            self.assertEqual(after_history, before_history)


if __name__ == "__main__":
    unittest.main()
