from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from tests.helpers import MemoryStore, make_plugin, make_status

from astrbot_plugin_get_mc_server_info.main import McServerConnectionError

SESSION_A = "test:GroupMessage:group-a"
SESSION_B = "test:GroupMessage:group-b"
PRIMARY_A = "main-a.example.com:25565"
PRIMARY_B = "main-b.example.com:25565"
BACKUP_A = "backup-a.example.com:25565"
BACKUP_B = "backup-b.example.com:25565"
SHARED = "shared.example.com:25565"


def server_record(
    name: str,
    primary: str,
    backups: list[str],
) -> dict[str, object]:
    return {
        "name": name,
        "address": primary,
        "backup_addresses": backups,
        "latency_history": [{"timestamp": 1, "latency": 22}],
        "last_latency": 22,
        "motd": "cached motd",
        "last_silent_query_at": 0,
        "last_active_query_at": 0,
        "created_at": 1,
    }


def make_plugin_with_lines(
    backups: list[str],
) -> tuple[object, MemoryStore]:
    persistence = MemoryStore(
        {
            "sessions": {
                SESSION_A: {
                    "template": "default_method",
                    "servers": {
                        PRIMARY_A: server_record(
                            "survival",
                            PRIMARY_A,
                            backups,
                        )
                    },
                }
            }
        }
    )
    plugin = make_plugin()
    plugin.query_all_concurrency = 3
    plugin._load_store = persistence.load
    plugin._save_store = persistence.save
    return plugin, persistence


def saved_server(
    persistence: MemoryStore,
    *,
    session_key: str = SESSION_A,
    primary: str = PRIMARY_A,
) -> dict[str, object]:
    return persistence.data["sessions"][session_key]["servers"][primary]


class SilentLinePollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_poll_uses_maximum_successful_line_latency(self) -> None:
        plugin, persistence = make_plugin_with_lines([BACKUP_A, BACKUP_B])
        statuses = {
            PRIMARY_A: make_status(PRIMARY_A, 35, motd="primary"),
            BACKUP_A: make_status(BACKUP_A, 91, motd="slowest"),
            BACKUP_B: make_status(BACKUP_B, 54, motd="backup"),
        }
        plugin._fetch_server_status = AsyncMock(
            side_effect=lambda address, **_: statuses[address]
        )

        await plugin._silent_query_once()

        server = saved_server(persistence)
        self.assertEqual(server["last_latency"], 91)
        self.assertEqual(server["latency_history"][-1]["latency"], 91)
        self.assertEqual(server["motd"], "slowest")
        self.assertEqual(plugin._fetch_server_status.await_count, 3)
        self.assertCountEqual(
            [call.args[0] for call in plugin._fetch_server_status.await_args_list],
            [PRIMARY_A, BACKUP_A, BACKUP_B],
        )

    async def test_silent_poll_records_zero_when_every_line_fails(self) -> None:
        plugin, persistence = make_plugin_with_lines([BACKUP_A])
        plugin._fetch_server_status = AsyncMock(
            side_effect=McServerConnectionError()
        )

        await plugin._silent_query_once()

        server = saved_server(persistence)
        self.assertEqual(server["last_latency"], 0)
        self.assertEqual(server["latency_history"][-1]["latency"], 0)
        self.assertEqual(server["motd"], "cached motd")
        self.assertGreater(server["last_silent_query_at"], 0)
        self.assertEqual(plugin._fetch_server_status.await_count, 2)

    async def test_shared_line_is_fetched_once_for_multiple_sessions(self) -> None:
        plugin, persistence = make_plugin_with_lines([SHARED])
        persistence.data["sessions"][SESSION_B] = {
            "template": "default_method",
            "servers": {
                PRIMARY_B: server_record(
                    "creative",
                    PRIMARY_B,
                    [SHARED],
                )
            },
        }
        statuses = {
            PRIMARY_A: make_status(PRIMARY_A, 40, motd="a"),
            PRIMARY_B: make_status(PRIMARY_B, 70, motd="b"),
            SHARED: make_status(SHARED, 55, motd="shared"),
        }
        plugin._fetch_server_status = AsyncMock(
            side_effect=lambda address, **_: statuses[address]
        )

        await plugin._silent_query_once()

        server_a = saved_server(persistence)
        server_b = saved_server(
            persistence,
            session_key=SESSION_B,
            primary=PRIMARY_B,
        )
        self.assertEqual(server_a["last_latency"], 55)
        self.assertEqual(server_b["last_latency"], 70)
        self.assertEqual(server_a["latency_history"][-1]["latency"], 55)
        self.assertEqual(server_b["latency_history"][-1]["latency"], 70)
        self.assertEqual(plugin._fetch_server_status.await_count, 3)
        shared_calls = [
            call
            for call in plugin._fetch_server_status.await_args_list
            if call.args[0] == SHARED
        ]
        self.assertEqual(len(shared_calls), 1)


if __name__ == "__main__":
    unittest.main()
