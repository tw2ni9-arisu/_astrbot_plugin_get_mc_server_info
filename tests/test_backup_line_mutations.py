from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.helpers import (
    FakeEvent,
    MemoryStore,
    collect_results,
    make_plugin,
    make_status,
)

from astrbot.core.provider.register import llm_tools
from astrbot_plugin_get_mc_server_info.main import (
    Main,
    McServerConnectionError,
    McServerTimeoutError,
)

SESSION_KEY = "test:GroupMessage:group-1"
OTHER_SESSION_KEY = "test:GroupMessage:group-2"
PRIMARY = "main.example.com:25565"
BACKUP_A = "backup-a.example.com:25565"
BACKUP_B = "backup-b.example.com:25565"


def make_plugin_with_server(
    *,
    backup_addresses: list[str] | None = None,
    other_primary: str | None = None,
    fail_save: bool = False,
    cache_root: Path | None = None,
) -> tuple[Main, MemoryStore]:
    servers = {
        PRIMARY: {
            "name": "survival",
            "address": PRIMARY,
            "backup_addresses": list(backup_addresses or []),
            "latency_history": [{"timestamp": 1, "latency": 30}],
            "last_latency": 30,
            "motd": "old motd",
            "last_silent_query_at": 0,
            "last_active_query_at": 0,
            "created_at": 1,
        }
    }
    if other_primary:
        servers[other_primary] = {
            "name": "creative",
            "address": other_primary,
            "backup_addresses": [],
            "latency_history": [],
        }
    persistence = MemoryStore(
        {
            "sessions": {
                SESSION_KEY: {
                    "template": "default_method",
                    "servers": servers,
                }
            }
        },
        fail_save=fail_save,
    )
    plugin = make_plugin(cache_root=cache_root)
    plugin._load_store = persistence.load
    plugin._save_store = persistence.save
    plugin._fetch_server_status = AsyncMock(
        return_value=make_status(BACKUP_A, 47, motd="new motd")
    )
    plugin._cache_server_icon = AsyncMock()
    plugin._cleanup_expired_cache = AsyncMock()
    return plugin, persistence


class AddBackupLineDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_appends_line_after_validation_without_history_point(
        self,
    ) -> None:
        plugin, persistence = make_plugin_with_server()

        result = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )

        server = persistence.data["sessions"][SESSION_KEY]["servers"][PRIMARY]
        self.assertTrue(result["ok"])
        self.assertEqual(result["primary_address"], PRIMARY)
        self.assertEqual(result["address"], BACKUP_A)
        self.assertEqual(result["line_type"], "backup")
        self.assertEqual(server["backup_addresses"], [BACKUP_A])
        self.assertEqual(server["latency_history"], [{"timestamp": 1, "latency": 30}])
        self.assertEqual(server["last_latency"], 47)
        self.assertEqual(server["motd"], "new motd")

    async def test_insertion_order_is_preserved_across_multiple_adds(self) -> None:
        plugin, persistence = make_plugin_with_server()

        first = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )
        plugin._fetch_server_status.return_value = make_status(BACKUP_B, 52)
        second = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_B,
        )

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        server = persistence.data["sessions"][SESSION_KEY]["servers"][PRIMARY]
        self.assertEqual(server["backup_addresses"], [BACKUP_A, BACKUP_B])

    async def test_address_owned_by_another_server_is_rejected_before_network(
        self,
    ) -> None:
        plugin, _ = make_plugin_with_server(other_primary=BACKUP_A)

        result = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )

        self.assertEqual(result["error"], "SERVER_ADDRESS_ALREADY_EXISTS")
        plugin._fetch_server_status.assert_not_awaited()

    async def test_existing_backup_address_is_rejected_before_network(self) -> None:
        plugin, _ = make_plugin_with_server(backup_addresses=[BACKUP_A])

        result = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )

        self.assertEqual(result["error"], "SERVER_ADDRESS_ALREADY_EXISTS")
        plugin._fetch_server_status.assert_not_awaited()

    async def test_failed_validation_does_not_change_store(self) -> None:
        plugin, persistence = make_plugin_with_server()
        before = copy.deepcopy(persistence.data)
        plugin._fetch_server_status.side_effect = McServerConnectionError()

        result = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )

        self.assertEqual(result["error"], "CONNECTION_FAILED")
        self.assertEqual(persistence.data, before)

    async def test_timeout_has_distinct_error(self) -> None:
        plugin, _ = make_plugin_with_server()
        plugin._fetch_server_status.side_effect = McServerTimeoutError()

        result = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )

        self.assertEqual(result["error"], "CONNECTION_TIMEOUT")

    async def test_missing_and_ambiguous_names_are_rejected(self) -> None:
        plugin, persistence = make_plugin_with_server()
        missing = await plugin._add_backup_server_data(
            SESSION_KEY,
            "missing",
            BACKUP_A,
        )
        persistence.data["sessions"][SESSION_KEY]["servers"][
            "second.example.com:25565"
        ] = {
            "name": "survival",
            "address": "second.example.com:25565",
            "backup_addresses": [],
            "latency_history": [],
        }
        ambiguous = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )

        self.assertEqual(missing["error"], "SERVER_NOT_FOUND")
        self.assertEqual(ambiguous["error"], "AMBIGUOUS_SERVER_NAME")
        plugin._fetch_server_status.assert_not_awaited()

    async def test_uniqueness_is_rechecked_after_network_wait(self) -> None:
        plugin, persistence = make_plugin_with_server()

        async def validate_and_claim(address: str, *, need_players: bool):
            persistence.data["sessions"][SESSION_KEY]["servers"][BACKUP_A] = {
                "name": "racer",
                "address": BACKUP_A,
                "backup_addresses": [],
                "latency_history": [],
            }
            return make_status(address, 41)

        plugin._fetch_server_status.side_effect = validate_and_claim

        result = await plugin._add_backup_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )

        self.assertEqual(result["error"], "SERVER_ADDRESS_ALREADY_EXISTS")
        server = persistence.data["sessions"][SESSION_KEY]["servers"][PRIMARY]
        self.assertEqual(server["backup_addresses"], [])

    async def test_save_failure_does_not_cache_icon(self) -> None:
        plugin, persistence = make_plugin_with_server(fail_save=True)

        with patch("astrbot_plugin_get_mc_server_info.main.logger.exception"):
            result = await plugin._add_backup_server_data(
                SESSION_KEY,
                "survival",
                BACKUP_A,
            )

        self.assertEqual(result["error"], "SAVE_FAILED")
        plugin._cache_server_icon.assert_not_awaited()
        server = persistence.data["sessions"][SESSION_KEY]["servers"][PRIMARY]
        self.assertEqual(server["backup_addresses"], [])


class BackupLinePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_non_admin_is_denied_even_when_mutations_are_open(
        self,
    ) -> None:
        plugin, _ = make_plugin_with_server()
        plugin.mutation_requires_admin = False
        event = FakeEvent(
            f"/备用 survival {BACKUP_A}",
            private=False,
            admin=False,
            session_key=SESSION_KEY,
        )

        results = await collect_results(plugin.add_backup_server(event))

        self.assertEqual(results, ["权限不足：该操作仅限管理员"])

    async def test_private_user_can_add_backup(self) -> None:
        plugin, _ = make_plugin_with_server()
        plugin._add_backup_server_data = AsyncMock(
            return_value={
                "ok": True,
                "server": "survival",
                "primary_address": PRIMARY,
                "address": BACKUP_A,
                "line_type": "backup",
                "latency": 47,
            }
        )
        event = FakeEvent(
            f"/bak survival {BACKUP_A}",
            private=True,
        )

        results = await collect_results(plugin.add_backup_server(event))

        self.assertEqual(
            results,
            [f"备用线路添加成功！服务器 [survival] 已添加备用地址 [{BACKUP_A}]"],
        )

    async def test_group_non_admin_tool_is_denied(self) -> None:
        plugin, _ = make_plugin_with_server()
        event = FakeEvent("", private=False, admin=False, session_key=SESSION_KEY)

        result = await plugin.add_mc_server_backup_tool(
            event,
            "survival",
            BACKUP_A,
        )

        self.assertEqual(result["error"], "PERMISSION_DENIED")

    async def test_private_tool_adds_backup(self) -> None:
        plugin, _ = make_plugin_with_server()
        event = FakeEvent("", private=True, session_key=SESSION_KEY)

        result = await plugin.add_mc_server_backup_tool(
            event,
            "survival",
            BACKUP_A,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["address"], BACKUP_A)
        self.assertIn("request_id", result)

    def test_backup_tool_is_registered(self) -> None:
        registered = {tool.name: tool.handler for tool in llm_tools.func_list}
        handler = getattr(Main, "add_mc_server_backup_tool", None)

        self.assertIsNotNone(handler)
        self.assertIs(registered.get("add_mc_server_backup"), handler)


class LineAwareMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_primary_cannot_reuse_an_existing_backup(self) -> None:
        plugin, _ = make_plugin_with_server(backup_addresses=[BACKUP_A])
        plugin._fetch_server_status.return_value = make_status(BACKUP_A, 38)

        result = await plugin._add_server_data("another-session", "new", BACKUP_A)

        self.assertTrue(result["ok"])

        same_session = await plugin._add_server_data(SESSION_KEY, "new", BACKUP_A)
        self.assertEqual(same_session["error"], "SERVER_ALREADY_EXISTS")

    async def test_redirect_rejects_own_backup_and_preserves_backups(self) -> None:
        plugin, persistence = make_plugin_with_server(backup_addresses=[BACKUP_A])
        rejected = await plugin._redirect_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_A,
        )
        plugin._fetch_server_status.return_value = make_status(BACKUP_B, 44)
        redirected = await plugin._redirect_server_data(
            SESSION_KEY,
            "survival",
            BACKUP_B,
        )

        self.assertEqual(rejected["error"], "SERVER_ALREADY_EXISTS")
        self.assertTrue(redirected["ok"])
        server = persistence.data["sessions"][SESSION_KEY]["servers"][BACKUP_B]
        self.assertEqual(server["backup_addresses"], [BACKUP_A])

    async def test_delete_cleans_unreferenced_primary_and_backup_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, _ = make_plugin_with_server(
                backup_addresses=[BACKUP_A, BACKUP_B],
                cache_root=Path(temp_dir),
            )
            for address in (PRIMARY, BACKUP_A, BACKUP_B):
                cache_dir = plugin._server_cache_dir(address)
                cache_dir.mkdir(parents=True)
                cache_dir.joinpath("marker").write_text("cached", encoding="utf-8")

            result = await plugin._delete_server_data(SESSION_KEY, "survival")

            self.assertTrue(result["ok"])
            for address in (PRIMARY, BACKUP_A, BACKUP_B):
                self.assertFalse(plugin._server_cache_dir(address).exists())

    async def test_clear_keeps_backup_cache_referenced_by_other_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin, persistence = make_plugin_with_server(
                backup_addresses=[BACKUP_A, BACKUP_B],
                cache_root=Path(temp_dir),
            )
            persistence.data["sessions"][OTHER_SESSION_KEY] = {
                "template": "default_method",
                "servers": {
                    "other.example.com:25565": {
                        "name": "other",
                        "address": "other.example.com:25565",
                        "backup_addresses": [BACKUP_B],
                        "latency_history": [],
                    }
                },
            }
            for address in (PRIMARY, BACKUP_A, BACKUP_B):
                plugin._server_cache_dir(address).mkdir(parents=True)

            result = await plugin._clear_session_data(SESSION_KEY)

            self.assertTrue(result["ok"])
            self.assertFalse(plugin._server_cache_dir(PRIMARY).exists())
            self.assertFalse(plugin._server_cache_dir(BACKUP_A).exists())
            self.assertTrue(plugin._server_cache_dir(BACKUP_B).exists())


if __name__ == "__main__":
    unittest.main()
