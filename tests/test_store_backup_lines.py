from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_get_mc_server_info import store


class BackupLineStoreTests(unittest.TestCase):
    def test_old_record_gets_empty_backup_list(self) -> None:
        data = {
            "sessions": {
                "s": {
                    "servers": {
                        "main.example:25565": {"name": "main"},
                    }
                }
            }
        }

        session = store.get_or_create_session(data, "s")

        self.assertEqual(
            session["servers"]["main.example:25565"]["backup_addresses"],
            [],
        )

    def test_normalization_preserves_order_and_drops_invalid_duplicates(
        self,
    ) -> None:
        data = {
            "sessions": {
                "s": {
                    "servers": {
                        "main.example:25565": {
                            "name": "main",
                            "backup_addresses": [
                                "backup-a.example:25565",
                                "backup-a.example:25565",
                                "main.example:25565",
                                42,
                                "backup-b.example:25565",
                            ],
                        },
                        "other.example:25565": {"name": "other"},
                    }
                }
            }
        }

        session = store.get_or_create_session(data, "s")
        server = session["servers"]["main.example:25565"]

        self.assertEqual(
            server["backup_addresses"],
            ["backup-a.example:25565", "backup-b.example:25565"],
        )
        self.assertEqual(
            store.get_server_line_addresses("main.example:25565", server),
            [
                "main.example:25565",
                "backup-a.example:25565",
                "backup-b.example:25565",
            ],
        )

    def test_primary_addresses_win_over_conflicting_backup_entries(self) -> None:
        data = {
            "sessions": {
                "s": {
                    "servers": {
                        "first.example:25565": {
                            "backup_addresses": ["second.example:25565"],
                        },
                        "second.example:25565": {},
                    }
                }
            }
        }

        session = store.get_or_create_session(data, "s")

        self.assertEqual(
            session["servers"]["first.example:25565"]["backup_addresses"],
            [],
        )

    def test_duplicate_backups_across_servers_keep_first_owner(self) -> None:
        data = {
            "sessions": {
                "s": {
                    "servers": {
                        "first.example:25565": {
                            "backup_addresses": ["shared.example:25565"],
                        },
                        "second.example:25565": {
                            "backup_addresses": ["shared.example:25565"],
                        },
                    }
                }
            }
        }

        session = store.get_or_create_session(data, "s")

        self.assertEqual(
            session["servers"]["first.example:25565"]["backup_addresses"],
            ["shared.example:25565"],
        )
        self.assertEqual(
            session["servers"]["second.example:25565"]["backup_addresses"],
            [],
        )

    def test_line_lookup_and_uniqueness_cover_backups(self) -> None:
        servers = {
            "main.example:25565": {
                "backup_addresses": ["backup.example:25565"],
            }
        }

        self.assertEqual(
            store.find_server_primary_by_line(
                servers,
                "backup.example:25565",
            ),
            "main.example:25565",
        )
        self.assertTrue(
            store.is_server_line_address_in_use(
                servers,
                "backup.example:25565",
            )
        )
        self.assertTrue(
            store.is_server_line_address_in_use(
                servers,
                "backup.example:25565",
                exclude_primary="main.example:25565",
            )
        )
        self.assertFalse(
            store.is_server_line_address_in_use(
                servers,
                "main.example:25565",
                exclude_primary="main.example:25565",
            )
        )
        self.assertEqual(
            store.get_session_server_addresses(servers),
            {"main.example:25565", "backup.example:25565"},
        )


if __name__ == "__main__":
    unittest.main()
