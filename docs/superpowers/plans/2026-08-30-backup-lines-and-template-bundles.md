# Backup Lines and Template Bundles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `v2.0.1` with ordered backup lines, primary-first query failover, all-line silent aggregation, cache-only image lists, image-based full queries, and directory-based query/list template bundles.

**Architecture:** Keep the existing primary-address-keyed server map and add an ordered `backup_addresses` field for backward compatibility. Centralize line ownership and failover helpers, then route commands, Tools, silent polling, cache lifecycle, and render inputs through those helpers. Discover each template as a validated directory bundle with separate query and list renderers; the terminal list entry delegates to the default list implementation with a different width.

**Tech Stack:** Python 3.10+, `asyncio`, AstrBot 4.18 APIs, `aiohttp`, `mcstatus`, Pillow, standard-library `unittest`/`unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-08-30-backup-lines-and-template-bundles-design.md`

## Global Constraints

- Release version is exactly `v2.0.1`; supported AstrBot range remains `>=4.18,<5`.
- Add no third-party dependencies.
- Existing stored sessions must normalize lazily without a destructive migration.
- A primary or backup address is unique within one session; reuse across sessions is allowed.
- `/备用线路`, `/备用`, `/bak`, and `add_mc_server_backup` always require a group administrator and always allow private-chat users.
- `/列表` and `/服务器列表` must never perform a server status request.
- Saved-server active queries use primary-first ordered failover; direct unsaved queries retain existing behavior.
- Silent polling writes one point per logical server: maximum successful line latency, or zero when all lines fail.
- Default and terminal list-renderer widths are exactly 900 and 1100 pixels; server-card spacing is exactly 20 pixels.
- Preserve the user's existing untracked tests and unrelated working-tree content.

---

### Task 1: Normalize and Index Logical Server Lines

**Files:**
- Modify: `store.py`
- Create: `tests/test_store_backup_lines.py`

**Interfaces:**
- Produces: `get_server_line_addresses(primary_address: str, server_obj: dict[str, Any]) -> list[str]`
- Produces: `find_server_primary_by_line(servers: dict[str, dict[str, Any]], line_address: str) -> str | None`
- Produces: `is_server_line_address_in_use(servers: dict[str, dict[str, Any]], line_address: str, *, exclude_primary: str | None = None) -> bool`
- Produces: `get_session_server_addresses(servers: dict[str, dict[str, Any]]) -> set[str]`
- Changes: `get_or_create_session` adds a normalized `backup_addresses` list to every server object.

- [ ] **Step 1: Write failing normalization and lookup tests**

```python
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
        data = {"sessions": {"s": {"servers": {"main.example:25565": {"name": "main"}}}}}
        session = store.get_or_create_session(data, "s")
        self.assertEqual(session["servers"]["main.example:25565"]["backup_addresses"], [])

    def test_normalization_preserves_order_and_drops_invalid_duplicates(self) -> None:
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
            ["main.example:25565", "backup-a.example:25565", "backup-b.example:25565"],
        )

    def test_primary_addresses_win_over_conflicting_backup_entries(self) -> None:
        data = {
            "sessions": {
                "s": {
                    "servers": {
                        "first.example:25565": {
                            "backup_addresses": ["second.example:25565"]
                        },
                        "second.example:25565": {},
                    }
                }
            }
        }
        session = store.get_or_create_session(data, "s")
        self.assertEqual(session["servers"]["first.example:25565"]["backup_addresses"], [])

    def test_line_lookup_and_uniqueness_cover_backups(self) -> None:
        servers = {
            "main.example:25565": {
                "backup_addresses": ["backup.example:25565"]
            }
        }
        self.assertEqual(
            store.find_server_primary_by_line(servers, "backup.example:25565"),
            "main.example:25565",
        )
        self.assertTrue(
            store.is_server_line_address_in_use(servers, "backup.example:25565")
        )
        self.assertEqual(
            store.get_session_server_addresses(servers),
            {"main.example:25565", "backup.example:25565"},
        )
```

- [ ] **Step 2: Run the store tests and verify RED**

Run: `python -m unittest tests.test_store_backup_lines -v`

Expected: failures because `backup_addresses` normalization and the four helper functions do not exist.

- [ ] **Step 3: Implement minimal ordered normalization and helpers**

```python
def get_server_line_addresses(
    primary_address: str,
    server_obj: dict[str, Any],
) -> list[str]:
    addresses = [primary_address]
    raw_backups = server_obj.get("backup_addresses", [])
    if not isinstance(raw_backups, list):
        return addresses
    for value in raw_backups:
        if isinstance(value, str) and value and value not in addresses:
            addresses.append(value)
    return addresses


def find_server_primary_by_line(
    servers: dict[str, dict[str, Any]],
    line_address: str,
) -> str | None:
    for primary_address, server_obj in servers.items():
        if line_address in get_server_line_addresses(primary_address, server_obj):
            return primary_address
    return None


def is_server_line_address_in_use(
    servers: dict[str, dict[str, Any]],
    line_address: str,
    *,
    exclude_primary: str | None = None,
) -> bool:
    for primary_address, server_obj in servers.items():
        if exclude_primary is not None and primary_address == exclude_primary:
            continue
        if line_address in get_server_line_addresses(primary_address, server_obj):
            return True
    return False


def get_session_server_addresses(
    servers: dict[str, dict[str, Any]],
) -> set[str]:
    result: set[str] = set()
    for primary_address, server_obj in servers.items():
        result.update(get_server_line_addresses(primary_address, server_obj))
    return result
```

Normalize with all valid primary keys reserved first, then accept valid backup strings in encounter order only when they are neither a primary nor a previously claimed backup.

- [ ] **Step 4: Run the store tests and the existing suite**

Run: `python -m unittest tests.test_store_backup_lines -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: all existing permission and clear-data tests remain green.

- [ ] **Step 5: Commit the store model**

```bash
git add store.py tests/test_store_backup_lines.py
git commit -m "feat: model ordered backup server lines"
```

---

### Task 2: Add Backup-Line Command, Tool, and Mutation Safety

**Files:**
- Modify: `main.py`
- Create: `tests/test_backup_line_mutations.py`

**Interfaces:**
- Consumes: Task 1 line ownership helpers.
- Produces: `BACKUP_SERVER_PATTERN` matching `/备用线路`, `/备用`, and `/bak`.
- Produces: `Main._add_backup_server_data(session_key: str, server_name: str, raw_address: str) -> dict[str, Any]`.
- Produces: command handler `Main.add_backup_server`.
- Produces: Tool handler `Main.add_mc_server_backup_tool(event, server: str, address: str) -> dict[str, Any]` registered as `add_mc_server_backup`.

- [ ] **Step 1: Write failing business-method tests**

Use a `MemoryStore`, an `object.__new__(Main)` fixture, and real Task 1 helpers. Cover these separate behaviors:

```python
async def test_add_backup_appends_after_successful_validation(self) -> None:
    plugin, persistence = make_plugin_with_server()
    plugin._fetch_server_status = AsyncMock(return_value=make_status(47))
    plugin._cache_server_icon = AsyncMock()
    plugin._cleanup_expired_cache = AsyncMock()

    result = await plugin._add_backup_server_data(
        SESSION_KEY, "survival", "backup.example.com:25565"
    )

    self.assertTrue(result["ok"])
    self.assertEqual(result["line_type"], "backup")
    server = persistence.data["sessions"][SESSION_KEY]["servers"][PRIMARY]
    self.assertEqual(server["backup_addresses"], ["backup.example.com:25565"])


async def test_add_backup_rejects_address_owned_by_another_server(self) -> None:
    plugin, _ = make_plugin_with_server(other_primary="other.example.com:25565")
    result = await plugin._add_backup_server_data(
        SESSION_KEY, "survival", "other.example.com:25565"
    )
    self.assertEqual(result["error"], "SERVER_ADDRESS_ALREADY_EXISTS")
    plugin._fetch_server_status.assert_not_awaited()


async def test_add_backup_does_not_save_when_validation_fails(self) -> None:
    plugin, persistence = make_plugin_with_server()
    plugin._fetch_server_status = AsyncMock(side_effect=McServerConnectionError())
    result = await plugin._add_backup_server_data(
        SESSION_KEY, "survival", "down.example.com:25565"
    )
    self.assertEqual(result["error"], "CONNECTION_FAILED")
    self.assertEqual(
        persistence.data["sessions"][SESSION_KEY]["servers"][PRIMARY]["backup_addresses"],
        [],
    )
```

Also add explicit tests for missing/ambiguous name, invalid/private address, timeout, uniqueness recheck after the network await, save failure, insertion order, and no latency-history append during validation.

- [ ] **Step 2: Write failing permission and registration tests**

```python
async def test_group_non_admin_cannot_add_backup_even_when_mutations_are_open(self) -> None:
    plugin, _ = make_plugin_with_server()
    plugin.mutation_requires_admin = False
    event = FakeEvent(
        "/备用 survival backup.example.com:25565",
        private=False,
        admin=False,
        session_key=SESSION_KEY,
    )
    results = await collect_results(plugin.add_backup_server(event))
    self.assertEqual(results, ["权限不足：该操作仅限管理员"])


async def test_private_user_can_add_backup(self) -> None:
    plugin, _ = make_plugin_with_server()
    plugin._add_backup_server_data = AsyncMock(
        return_value={"ok": True, "server": "survival", "address": "backup.example.com:25565"}
    )
    event = FakeEvent(
        "/bak survival backup.example.com:25565",
        private=True,
        session_key=SESSION_KEY,
    )
    results = await collect_results(plugin.add_backup_server(event))
    self.assertIn("备用线路添加成功", results[0])


def test_backup_tool_is_registered(self) -> None:
    registered = {tool.name: tool.handler for tool in llm_tools.func_list}
    self.assertIs(registered["add_mc_server_backup"], Main.add_mc_server_backup_tool)
```

Add matching group/private Tool tests.

- [ ] **Step 3: Run mutation tests and verify RED**

Run: `python -m unittest tests.test_backup_line_mutations -v`

Expected: failures because handlers and `_add_backup_server_data` do not exist.

- [ ] **Step 4: Implement the command, Tool, and shared business method**

Add:

```python
BACKUP_SERVER_PATTERN = re.compile(
    r"^/(?:备用线路|备用|bak)\s+(\S+)\s+(\S+)\s*$",
    re.IGNORECASE,
)
```

The command performs `_is_group_admin_denied` before rate limiting and maps every structured error to a specific Chinese message. The Tool performs the same unconditional group-admin check and returns `_with_tool_meta(result)`.

Implement `_add_backup_server_data` using a read/check phase, one `_fetch_server_status(address, need_players=False)` validation call, and a locked reload/recheck/save phase. Return at least:

```python
{
    "ok": True,
    "server": server_name,
    "primary_address": primary_address,
    "address": address,
    "line_type": "backup",
    "latency": status.latency,
}
```

Do not call `_append_latency`. Cache the new icon only after a successful save, then clear query/Tool/list caches.

Add the pattern to the fallback command regex, valid-pattern tuple, and help text.

- [ ] **Step 5: Make existing mutations line-aware**

Change `_add_server_data` and `_redirect_server_data` to reject any address already owned as a primary or backup. On redirect, remove/reinsert the server object under the new primary key without changing `backup_addresses`.

Change `_delete_server_data`, `_clear_session_data`, `_collect_referenced_addresses`, and `_cleanup_expired_cache` to include all logical lines. Cache deletion must happen only after save succeeds and only for addresses absent from the post-save global reference set.

- [ ] **Step 6: Run focused and full tests**

Run: `python -m unittest tests.test_backup_line_mutations -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 7: Commit mutation support**

```bash
git add main.py tests/test_backup_line_mutations.py
git commit -m "feat: add managed backup server lines"
```

---

### Task 3: Implement Primary-First Active Query Failover

**Files:**
- Modify: `main.py`
- Create: `tests/test_query_failover.py`

**Interfaces:**
- Consumes: Task 1 ordered line enumeration and ownership lookup.
- Produces: `SavedServerQueryResult` dataclass.
- Produces: `Main._query_saved_server_lines(primary_address: str, server_obj: dict[str, Any], *, need_players: bool) -> SavedServerQueryResult`.
- Produces: `Main._find_cached_server_icon(primary_address: str, server_obj: dict[str, Any]) -> Path | None`.

- [ ] **Step 1: Write failing ordered-failover tests**

```python
async def test_primary_success_does_not_touch_backups(self) -> None:
    plugin = make_plugin()
    primary_status = make_status(address=PRIMARY, latency=31)
    plugin._fetch_server_status = AsyncMock(return_value=primary_status)
    result = await plugin._query_saved_server_lines(
        PRIMARY,
        {"backup_addresses": [BACKUP_A, BACKUP_B]},
        need_players=True,
    )
    self.assertIs(result.status, primary_status)
    self.assertEqual(result.address, PRIMARY)
    self.assertEqual(result.line_type, "primary")
    plugin._fetch_server_status.assert_awaited_once_with(PRIMARY, need_players=True)


async def test_backups_are_tried_in_insertion_order(self) -> None:
    plugin = make_plugin()
    backup_status = make_status(address=BACKUP_B, latency=72)
    plugin._fetch_server_status = AsyncMock(
        side_effect=[McServerConnectionError(), McServerTimeoutError(), backup_status]
    )
    result = await plugin._query_saved_server_lines(
        PRIMARY,
        {"backup_addresses": [BACKUP_A, BACKUP_B]},
        need_players=False,
    )
    self.assertEqual(result.address, BACKUP_B)
    self.assertEqual(result.line_type, "backup")
    self.assertEqual(result.attempted_addresses, [PRIMARY, BACKUP_A, BACKUP_B])


async def test_all_lines_failed_returns_offline_result(self) -> None:
    plugin = make_plugin()
    plugin._fetch_server_status = AsyncMock(side_effect=McServerConnectionError())
    result = await plugin._query_saved_server_lines(
        PRIMARY, {"backup_addresses": [BACKUP_A]}, need_players=False
    )
    self.assertIsNone(result.status)
    self.assertEqual(result.address, PRIMARY)
    self.assertEqual(result.attempted_addresses, [PRIMARY, BACKUP_A])
```

- [ ] **Step 2: Write failing command and Tool integration tests**

Test that querying by name, primary address, or backup address resolves the same logical server; backup success updates history once, caches assets under the actual line, and renders/returns that address. Assert Tool fields `primary_address`, `address`, `line_type`, and `attempted_addresses`. Assert all-failed command rendering is Offline and does not persist a zero history point.

- [ ] **Step 3: Run failover tests and verify RED**

Run: `python -m unittest tests.test_query_failover -v`

Expected: failures because the failover result/helper and line-aware resolution do not exist.

- [ ] **Step 4: Implement minimal failover result and helper**

```python
@dataclass
class SavedServerQueryResult:
    status: ServerStatus | None
    address: str
    line_type: str
    attempted_addresses: list[str]
    error: str | None = None


async def _query_saved_server_lines(
    self,
    primary_address: str,
    server_obj: dict[str, Any],
    *,
    need_players: bool,
) -> SavedServerQueryResult:
    attempted: list[str] = []
    last_error = "CONNECTION_FAILED"
    for index, line_address in enumerate(
        _store_mod.get_server_line_addresses(primary_address, server_obj)
    ):
        attempted.append(line_address)
        try:
            status = await self._fetch_server_status(
                line_address, need_players=need_players
            )
        except McServerInvalidAddressError:
            last_error = "INVALID_ADDRESS"
        except McServerTimeoutError:
            last_error = "CONNECTION_TIMEOUT"
        except McServerConnectionError:
            last_error = "CONNECTION_FAILED"
        else:
            return SavedServerQueryResult(
                status=status,
                address=line_address,
                line_type="primary" if index == 0 else "backup",
                attempted_addresses=attempted,
            )
    return SavedServerQueryResult(
        status=None,
        address=primary_address,
        line_type="primary",
        attempted_addresses=attempted,
        error=last_error,
    )
```

- [ ] **Step 5: Route saved command and Tool queries through failover**

Resolve a normalized saved-line address through `find_server_primary_by_line`. Keep direct unsaved queries unchanged. Update `_query_single_server` and `_query_server_data` to use the helper, store state under the primary key, and use the actual line for icon/avatar paths and rendered `server_address`.

For all-failed single rendering, use `_find_cached_server_icon` to scan primary then backups. For Tool failure, include `primary_address` and `attempted_addresses` with the final structured error.

- [ ] **Step 6: Run failover and regression tests**

Run: `python -m unittest tests.test_query_failover -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 7: Commit active failover**

```bash
git add main.py tests/test_query_failover.py
git commit -m "feat: fail over saved server queries"
```

---

### Task 4: Aggregate Every Line During Silent Polling

**Files:**
- Modify: `main.py`
- Create: `tests/test_silent_line_polling.py`

**Interfaces:**
- Consumes: Task 1 line enumeration.
- Changes: `Main._silent_query_once()` queries unique line addresses once, then writes one aggregate result per logical server.

- [ ] **Step 1: Write failing silent aggregation tests**

```python
async def test_silent_poll_uses_maximum_successful_line_latency(self) -> None:
    plugin, persistence = make_plugin_with_lines([BACKUP_A, BACKUP_B])
    statuses = {
        PRIMARY: make_status(PRIMARY, 35),
        BACKUP_A: make_status(BACKUP_A, 91),
        BACKUP_B: make_status(BACKUP_B, 54),
    }
    plugin._fetch_server_status = AsyncMock(
        side_effect=lambda address, **_: statuses[address]
    )
    await plugin._silent_query_once()
    server = saved_server(persistence)
    self.assertEqual(server["last_latency"], 91)
    self.assertEqual(server["latency_history"][-1]["latency"], 91)
    self.assertEqual(plugin._fetch_server_status.await_count, 3)


async def test_silent_poll_records_zero_when_every_line_fails(self) -> None:
    plugin, persistence = make_plugin_with_lines([BACKUP_A])
    plugin._fetch_server_status = AsyncMock(side_effect=McServerConnectionError())
    await plugin._silent_query_once()
    server = saved_server(persistence)
    self.assertEqual(server["last_latency"], 0)
    self.assertEqual(server["latency_history"][-1]["latency"], 0)
    self.assertEqual(server["motd"], "cached motd")
```

Add a cross-session test proving an identical line address is fetched once while both logical servers receive their own aggregate history point.

- [ ] **Step 2: Run silent tests and verify RED**

Run: `python -m unittest tests.test_silent_line_polling -v`

Expected: maximum/all-failed assertions fail because current code queries only primaries and skips failures.

- [ ] **Step 3: Implement one-round address fetch and logical aggregation**

Snapshot sessions, build the unique address set from every logical server line, and fetch with an `asyncio.Semaphore(self.query_all_concurrency)`. Represent failures as `None`. Reload the store once, then for each logical server select:

```python
successful = [
    status_by_address[address]
    for address in _store_mod.get_server_line_addresses(primary, server_obj)
    if status_by_address.get(address) is not None
]
selected = max(successful, key=lambda status: status.latency) if successful else None
latency = selected.latency if selected is not None else 0
server_obj["last_latency"] = latency
server_obj["last_silent_query_at"] = now
if selected is not None:
    server_obj["motd"] = str(selected.motd or "")
self._append_latency(server_obj, latency, now)
```

Save once after processing all sessions.

- [ ] **Step 4: Run silent and full tests**

Run: `python -m unittest tests.test_silent_line_polling -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 5: Commit silent aggregation**

```bash
git add main.py tests/test_silent_line_polling.py
git commit -m "feat: aggregate backup lines in silent polling"
```

---

### Task 5: Discover Directory-Based Query/List Template Bundles

**Files:**
- Modify: `template_loader.py`
- Modify: `main.py`
- Create: `tests/test_template_bundles.py`
- Move: `templates/default_method.py` to `templates/default_method/default_method_query.py`
- Move: `templates/default_method.png` to `templates/default_method/default_method.png`
- Move: `templates/default_icon.png` to `templates/default_method/default_icon.png`
- Move: `templates/HarmonyOS_SansSC_Medium.ttf` to `templates/default_method/HarmonyOS_SansSC_Medium.ttf`
- Move: `templates/terminal_method.py` to `templates/terminal_method/terminal_method_query.py`
- Create: `templates/default_method/__init__.py`
- Create: `templates/terminal_method/__init__.py`
- Create: `templates/default_method/default_method_list.py`
- Create: `templates/terminal_method/terminal_method_list.py`
- Create: `tests/test_list_renderer_geometry.py`

**Interfaces:**
- Produces: `TemplateBundle(name: str, directory: Path, query_file: Path, list_file: Path)`.
- Produces: `discover_templates(templates_dir: Path) -> dict[str, TemplateBundle]`.
- Changes: `template_file_path(templates_dir: Path, template_name: str, mode: str = "query") -> Path` selects `query` or `list`.
- Changes: `get_template_renderer(template_name: str, mode: str, templates_dir: Path, renderer_cache: dict[tuple[str, str], tuple[float, TemplateRenderer]]) -> TemplateRenderer` loads the selected role.
- Query renderer export remains `render_server_report_image`; list renderer export is `render_server_list_image`.
- Produces: `default_method_list.render_server_list_image(*, mode: str, servers: list[dict[str, Any]], canvas_width: int = 900) -> str`.
- Produces: terminal adapter with the same public function and fixed `canvas_width=1100` delegation.

- [ ] **Step 1: Write failing discovery tests with temporary directories**

```python
def write_renderer(path: Path, function_name: str) -> None:
    path.write_text(
        "async def " + function_name + "(**kwargs):\n    return 'ok'\n",
        encoding="utf-8",
    )


def test_discovers_valid_bundle_under_query_public_name(self) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        bundle_dir = root / "folder-name-does-not-control-public-name"
        bundle_dir.mkdir()
        write_renderer(bundle_dir / "ocean_query.py", "render_server_report_image")
        write_renderer(bundle_dir / "cards_list.py", "render_server_list_image")
        bundles = template_loader.discover_templates(root)
        self.assertEqual(list(bundles), ["ocean"])
        self.assertEqual(bundles["ocean"].list_file.name, "cards_list.py")


def test_rejects_missing_or_duplicate_roles_and_multiple_fonts(self) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        missing_list = root / "missing-list"
        missing_list.mkdir()
        write_renderer(
            missing_list / "missing_query.py", "render_server_report_image"
        )

        duplicate_query = root / "duplicate-query"
        duplicate_query.mkdir()
        write_renderer(
            duplicate_query / "first_query.py", "render_server_report_image"
        )
        write_renderer(
            duplicate_query / "second_query.py", "render_server_report_image"
        )
        write_renderer(
            duplicate_query / "cards_list.py", "render_server_list_image"
        )

        multiple_fonts = root / "multiple-fonts"
        multiple_fonts.mkdir()
        write_renderer(
            multiple_fonts / "fonted_query.py", "render_server_report_image"
        )
        write_renderer(
            multiple_fonts / "cards_list.py", "render_server_list_image"
        )
        (multiple_fonts / "first.ttf").write_bytes(b"first")
        (multiple_fonts / "second.ttf").write_bytes(b"second")

        self.assertEqual(template_loader.discover_templates(root), {})
```

Add async tests proving query mode requires `render_server_report_image`, list mode requires `render_server_list_image`, and a changed file modification time reloads the renderer.

Add renderer geometry tests before creating the shipped list files:

```python
async def test_default_list_renderer_uses_expected_width(self) -> None:
    encoded = await render_server_list_image(
        mode="list",
        servers=[make_list_entry("one"), make_list_entry("two")],
    )
    image = Image.open(io.BytesIO(base64.b64decode(encoded)))
    self.assertEqual(image.width, 900)
    self.assertGreater(image.height, 300)


async def test_terminal_adapter_reuses_renderer_at_1100_pixels(self) -> None:
    encoded = await terminal_render(
        mode="list",
        servers=[make_list_entry("one")],
    )
    image = Image.open(io.BytesIO(base64.b64decode(encoded)))
    self.assertEqual(image.width, 1100)
```

Expose pure `_card_height(entry, mode)` and assert that a two-card canvas height equals top/bottom padding plus both card heights plus exactly `20` pixels.

- [ ] **Step 2: Run loader tests and verify RED**

Run: `python -m unittest tests.test_template_bundles tests.test_list_renderer_geometry -v`

Expected: failures because discovery currently scans flat `.py` files, role mode is absent, and the list modules do not exist.

- [ ] **Step 3: Implement `TemplateBundle` discovery and mode loading**

```python
@dataclass(frozen=True)
class TemplateBundle:
    name: str
    directory: Path
    query_file: Path
    list_file: Path


def discover_templates(templates_dir: Path) -> dict[str, TemplateBundle]:
    bundles: dict[str, TemplateBundle] = {}
    if not templates_dir.is_dir():
        return bundles
    for directory in sorted(path for path in templates_dir.iterdir() if path.is_dir()):
        query_files = sorted(directory.glob("*_query.py"))
        list_files = sorted(directory.glob("*_list.py"))
        fonts = sorted(directory.glob("*.ttf"))
        if len(query_files) != 1 or len(list_files) != 1 or len(fonts) > 1:
            continue
        name = query_files[0].stem.removesuffix("_query")
        if not is_valid_template_name(name) or name in bundles:
            continue
        bundles[name] = TemplateBundle(name, directory, query_files[0], list_files[0])
    return bundles
```

Use an export-name map `{"query": "render_server_report_image", "list": "render_server_list_image"}` and cache by `(template_name, mode)` plus file mtime.

- [ ] **Step 4: Move shipped query templates/assets and implement the list renderer**

Use `git mv` for tracked files. In both query renderers, derive the background name with `Path(__file__).stem.removesuffix("_query")`. Keep default font/icon lookup local. Let terminal query font lookup fall back to the default bundle's `.ttf` so Chinese rendering remains reliable without duplicating the font asset.

Create package marker files containing only a short module docstring.

Create `default_method_list.py`. Its async public function delegates Pillow work through `asyncio.to_thread`. Use these exact geometry constants:

```python
OUTER_PADDING = 24
PANEL_GAP = 12
CARD_GAP = 20
HEADER_HEIGHT = 120
ROW_HEIGHT = 38
CONTENT_MIN_HEIGHT = 88
```

Build a header rounded rectangle followed by a content rounded rectangle. In `mode="list"`, rows are line labels and addresses. In `mode="query_all"`, rows are player avatar/name entries or one empty-state row. Reuse the default query palette, background, font, default icon, truncation, and latency colors; never draw reference guide lines.

Create the terminal thin adapter:

```python
from astrbot_plugin_get_mc_server_info.templates.default_method.default_method_list import (
    render_server_list_image as _render_default_list,
)


async def render_server_list_image(**kwargs: Any) -> str:
    kwargs["canvas_width"] = 1100
    return await _render_default_list(**kwargs)
```

- [ ] **Step 5: Adapt `Main` to role-specific renderer loading**

Change `_template_renderer_cache` to:

```python
dict[tuple[str, str], tuple[float, _tl_mod.TemplateRenderer]]
```

Make `_get_template_renderer(template_name, mode="query")` and `_template_file_path(template_name, mode="query")` delegate to the new loader. Existing single/direct queries explicitly use query mode. Template switching validates bundle discovery rather than flat files.

For compatibility `/模板 reload`, check whether a discovered public template named `reload` exists before resolving a file path; if it does not exist, clear caches directly.

- [ ] **Step 6: Run template and regression tests**

Run: `python -m unittest tests.test_template_bundles tests.test_list_renderer_geometry -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 7: Commit template bundle loading**

```bash
git add template_loader.py main.py templates tests/test_template_bundles.py tests/test_list_renderer_geometry.py
git commit -m "feat: load query and list template bundles"
```

---

### Task 6: Render Cache-Only `/列表`

**Files:**
- Modify: `main.py`
- Create: `tests/test_list_rendering.py`

**Interfaces:**
- Consumes: Task 5 shared list renderer and list-mode template loading.
- Changes: `Main.list_servers` returns one base64 image and performs no network request.

- [ ] **Step 1: Write a failing cache-only command test**

Construct a saved server with a primary and two backups, patch `_fetch_server_status` to raise `AssertionError("network call forbidden")`, patch the list renderer to capture arguments, and assert:

```python
self.assertEqual(entry["lines"], [
    {"address": PRIMARY, "line_type": "primary"},
    {"address": BACKUP_A, "line_type": "backup"},
    {"address": BACKUP_B, "line_type": "backup"},
])
self.assertEqual(entry["latency"], 63)
plugin._fetch_server_status.assert_not_called()
```

- [ ] **Step 2: Run list tests and verify RED**

Run: `python -m unittest tests.test_list_rendering -v`

Expected: current `/列表` returns text rather than invoking the list renderer.

- [ ] **Step 3: Render `/列表` from cached state**

Load the selected template and server records, choose the first existing cached icon in primary-first order, build line rows, and call the list renderer in `list` mode. Return `event.make_result().base64_image(image_b64)`. Do not call `_fetch_server_status`, `_cache_server_icon`, or avatar download functions.

- [ ] **Step 4: Run list and full tests**

Run: `python -m unittest tests.test_list_rendering -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 5: Commit list rendering**

```bash
git add main.py tests/test_list_rendering.py
git commit -m "feat: render cached server lists as images"
```

---

### Task 7: Render Full `/查询` as One Multi-Server Image

**Files:**
- Modify: `main.py`
- Create: `tests/test_query_all_rendering.py`

**Interfaces:**
- Consumes: Task 3 active failover and Task 5 list renderer.
- Changes: `Main._query_all_servers(event)` returns an AstrBot image result rather than `(summary, failures)`.

- [ ] **Step 1: Write failing successful/offline-card tests**

```python
async def test_query_all_renders_actual_backup_line_and_players(self) -> None:
    plugin, event = make_plugin_with_two_servers()
    plugin._query_saved_server_lines = AsyncMock(
        side_effect=[
            saved_success(BACKUP_A, "backup", make_status(BACKUP_A, 64, players=[player("Alex")])),
            saved_offline(SECOND_PRIMARY),
        ]
    )
    captured: dict[str, Any] = {}
    plugin._call_template_renderer = capture_renderer(captured, "encoded-image")

    result = await plugin._query_all_servers(event)

    self.assertEqual(captured["mode"], "query_all")
    self.assertEqual(captured["servers"][0]["address"], BACKUP_A)
    self.assertEqual(captured["servers"][0]["line_type"], "backup")
    self.assertEqual(captured["servers"][0]["players"][0]["name"], "Alex")
    self.assertTrue(captured["servers"][1]["offline"])
    self.assertEqual(result, "IMAGE_RESULT")
```

Add assertions that successful entries update the logical primary record exactly once, use the actual-line icon/avatar cache paths, and offline active results do not append persisted zero history.

- [ ] **Step 2: Run full-query tests and verify RED**

Run: `python -m unittest tests.test_query_all_rendering -v`

Expected: failures because current full query returns text and does not request players/fail over.

- [ ] **Step 3: Refactor `_query_all_servers` to produce render entries**

Keep the outer semaphore across logical servers. Within each slot, call `_query_saved_server_lines(primary, server_obj, need_players=True)`. Preserve input ordering when gathering results.

For success, cache the actual line icon, collect avatars under the actual address, and create:

```python
{
    "name": server_name,
    "primary_address": primary_address,
    "address": query_result.address,
    "line_type": query_result.line_type,
    "latency": status.latency,
    "players_online": status.players_online,
    "players_max": status.players_max,
    "version": status.version,
    "icon_path": str(icon_path) if icon_path.exists() else None,
    "players": players_for_render,
    "offline": False,
}
```

For failure, use primary address, zero counts, empty players, cached icon fallback, and `offline=True`. Merge successful store updates in one locked save, then call the selected list renderer with `mode="query_all"` and return one base64 image result.

- [ ] **Step 4: Change the parameterless query handler**

Replace text joining with:

```python
yield await self._query_all_servers(event)
```

Keep the existing no-saved-server plain-text message.

- [ ] **Step 5: Run focused and full tests**

Run: `python -m unittest tests.test_query_all_rendering -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 6: Commit full-query rendering**

```bash
git add main.py tests/test_query_all_rendering.py
git commit -m "feat: render full server queries as one image"
```

---

### Task 8: Update Release Documentation and Perform Final Verification

**Files:**
- Modify: `main.py`
- Modify: `metadata.yaml`
- Modify: `README.md`
- Modify: `templates/__init__.py`
- Create: `tests/test_release_v201.py`
- Create: `tests/render_v201_fixtures.py`
- Create: `tests/visual_output/.gitignore`

**Interfaces:**
- Changes: `PLUGIN_VERSION = "v2.0.1"` and metadata `version: v2.0.1`.
- Produces: deterministic visual fixtures for cache-only list and full-query cards.

- [ ] **Step 1: Add a failing version/documentation consistency test**

Add to a new `tests/test_release_v201.py`:

```python
def test_release_versions_match(self) -> None:
    metadata = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    self.assertEqual(PLUGIN_VERSION, "v2.0.1")
    self.assertIn("version: v2.0.1", metadata)
    self.assertIn("/备用线路", readme)
    self.assertIn("add_mc_server_backup", readme)
```

- [ ] **Step 2: Run the release test and verify RED**

Run: `python -m unittest tests.test_release_v201 -v`

Expected: version and documentation assertions fail at `v1.9.3`/missing backup docs.

- [ ] **Step 3: Update versions, help, README, and template package docs**

Document exact command aliases, unconditional group-admin/private permission behavior, primary-first active failover, maximum-latency silent aggregation, cache-only `/列表`, image-based full `/查询`, Tool return line metadata, and the bundle directory validation rules. Keep dependency and AstrBot compatibility sections unchanged.

- [ ] **Step 4: Add deterministic visual fixture generation**

`tests/render_v201_fixtures.py` imports the default list renderer and writes decoded PNGs to `tests/visual_output/` for:

- Two `/列表` cards with main and backup line rows, one cached icon and one fallback icon.
- Two full-query cards, one successful backup response with players and one Offline result.
- A terminal-width card through the thin adapter.

The script accepts no network input and always overwrites only these three named PNG files. `tests/visual_output/.gitignore` ignores `*.png` while retaining the directory.

- [ ] **Step 5: Run all automated verification**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS with no warnings or tracebacks.

Run: `python -m compileall -q main.py store.py query.py cache.py avatar.py template_loader.py templates tests`

Expected: exit code 0 and no output.

Run: `git diff --check 767362b..HEAD`

Expected: no whitespace errors.

- [ ] **Step 6: Generate and inspect visual fixtures**

Run: `python tests/render_v201_fixtures.py`

Open all three PNGs and verify:

- Widths are 900, 900, and 1100 pixels as intended.
- Header/content rounded panels align with the supplied reference.
- No purple guide lines appear.
- Cards have a 20-pixel vertical gap.
- Long addresses and names do not overflow.
- Icons, avatars, empty-player state, fallback icon, background, latency colors, and Offline state render correctly.

- [ ] **Step 7: Review the complete diff for scope**

Run: `git status --short`

Expected: only planned source, template, test, and documentation files are changed; the pre-existing user-owned test files remain preserved.

Run: `git diff --stat` and `git diff -- main.py store.py template_loader.py README.md metadata.yaml templates tests`

Verify every changed line traces to this release.

- [ ] **Step 8: Commit release documentation and fixtures**

```bash
git add main.py metadata.yaml README.md templates/__init__.py tests/test_release_v201.py tests/render_v201_fixtures.py tests/visual_output/.gitignore
git commit -m "docs: release backup line support in v2.0.1"
```

- [ ] **Step 9: Run final post-commit verification**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

Run: `git status --short`

Expected: no new implementation changes; only intentionally pre-existing untracked content may remain.
