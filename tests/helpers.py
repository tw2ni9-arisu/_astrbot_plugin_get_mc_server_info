from __future__ import annotations

import asyncio
import copy
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = PLUGIN_ROOT.parents[3]
os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))
for import_root in (PLUGIN_ROOT.parent, ASTRBOT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from astrbot_plugin_get_mc_server_info.main import Main, ServerStatus


class FakeEvent:
    def __init__(
        self,
        message: str,
        *,
        private: bool,
        admin: bool = False,
        session_key: str = "test:FriendMessage:user-1",
        sender_id: str = "user-1",
        self_id: str = "bot-1",
    ) -> None:
        self.message_str = message
        self.unified_msg_origin = session_key
        self._private = private
        self._admin = admin
        self._sender_id = sender_id
        self._self_id = self_id

    def is_private_chat(self) -> bool:
        return self._private

    def is_admin(self) -> bool:
        return self._admin

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return self._self_id

    def plain_result(self, message: str) -> str:
        return message


class MemoryStore:
    def __init__(self, data: dict[str, Any], *, fail_save: bool = False) -> None:
        self.data = copy.deepcopy(data)
        self.fail_save = fail_save

    async def load(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    async def save(self, data: dict[str, Any]) -> None:
        if self.fail_save:
            raise OSError("save failed")
        self.data = copy.deepcopy(data)


def make_plugin(*, cache_root: Path | None = None) -> Main:
    plugin = object.__new__(Main)
    plugin._store_lock = asyncio.Lock()
    plugin._cache_root = cache_root or Path("unused-test-cache")
    plugin._command_rate_limit_hits = {}
    plugin._tool_rate_limit_hits = {}
    plugin._tool_status_cache = {}
    plugin._tool_list_cache = {}
    plugin._tool_query_semaphore = asyncio.Semaphore(1)
    plugin._query_render_cache = {}
    plugin.mutation_requires_admin = True
    plugin.direct_query_requires_admin = True
    plugin.auto_append_default_port = False
    plugin.max_servers_per_session = 50
    plugin.history_limit = 48
    plugin.silent_query_interval_seconds = 1800
    plugin.cache_ttl_seconds = 86400
    return plugin


def make_status(
    address: str,
    latency: int = 47,
    *,
    motd: str = "hello",
) -> ServerStatus:
    return ServerStatus(
        address=address,
        latency=latency,
        version="1.21.4",
        players_online=2,
        players_max=20,
        icon_base64=None,
        players=[],
        motd=motd,
    )


async def collect_results(generator: Any) -> list[Any]:
    return [result async for result in generator]
