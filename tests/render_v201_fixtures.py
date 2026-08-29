"""Generate deterministic v2.0.1 list/query-all visual QA fixtures."""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_get_mc_server_info.templates.default_method.default_method_list import (
    render_server_list_image,
)
from astrbot_plugin_get_mc_server_info.templates.terminal_method.terminal_method_list import (
    render_server_list_image as render_terminal_list,
)

OUTPUT_DIR = Path(__file__).resolve().parent / "visual_output"
OUTPUT_NAMES = {
    "v201_list_default.png",
    "v201_query_all_default.png",
    "v201_list_terminal.png",
}
DEFAULT_ICON = (
    PLUGIN_ROOT / "templates" / "default_method" / "default_icon.png"
)


def list_entry(
    name: str,
    primary: str,
    backups: list[str],
    *,
    latency: int,
    icon_path: str | None,
) -> dict[str, Any]:
    addresses = [primary, *backups]
    return {
        "name": name,
        "primary_address": primary,
        "address": primary,
        "line_type": "primary",
        "latency": latency,
        "players_online": None,
        "players_max": None,
        "version": "",
        "offline": latency <= 0,
        "icon_path": icon_path,
        "lines": [
            {
                "address": address,
                "line_type": "primary" if index == 0 else "backup",
            }
            for index, address in enumerate(addresses)
        ],
        "players": [],
    }


def write_fixture(name: str, encoded: str) -> None:
    if name not in OUTPUT_NAMES:
        raise ValueError(f"unexpected fixture name: {name}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath(name).write_bytes(base64.b64decode(encoded))


async def main() -> None:
    cached_icon = str(DEFAULT_ICON) if DEFAULT_ICON.is_file() else None
    list_servers = [
        list_entry(
            "生存主服",
            "main.example.com:25565",
            ["backup-a.example.com:25565", "backup-b.example.com:25565"],
            latency=63,
            icon_path=cached_icon,
        ),
        list_entry(
            "建筑测试服",
            "creative.example.com:25565",
            ["creative-bak.example.com:25565"],
            latency=0,
            icon_path=None,
        ),
    ]
    query_servers = [
        {
            "name": "生存主服",
            "primary_address": "main.example.com:25565",
            "address": "backup-a.example.com:25565",
            "line_type": "backup",
            "latency": 64,
            "players_online": 2,
            "players_max": 20,
            "version": "1.21.4",
            "offline": False,
            "icon_path": cached_icon,
            "lines": [],
            "players": [
                {"name": "Alex", "avatar_path": ""},
                {"name": "Steve", "avatar_path": ""},
            ],
        },
        {
            "name": "建筑测试服",
            "primary_address": "creative.example.com:25565",
            "address": "creative.example.com:25565",
            "line_type": "primary",
            "latency": 0,
            "players_online": 0,
            "players_max": 0,
            "version": "Unknown",
            "offline": True,
            "icon_path": None,
            "lines": [],
            "players": [],
        },
    ]

    write_fixture(
        "v201_list_default.png",
        await render_server_list_image(mode="list", servers=list_servers),
    )
    write_fixture(
        "v201_query_all_default.png",
        await render_server_list_image(mode="query_all", servers=query_servers),
    )
    write_fixture(
        "v201_list_terminal.png",
        await render_terminal_list(mode="list", servers=[list_servers[0]]),
    )


if __name__ == "__main__":
    asyncio.run(main())
