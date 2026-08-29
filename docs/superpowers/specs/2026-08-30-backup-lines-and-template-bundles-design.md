# Backup Lines and Template Bundles Design

## Goal

Release plugin version `v2.0.1` with ordered backup addresses for each saved Minecraft server, automatic active-query failover, all-line silent polling, image-based `/列表` and full `/查询` results, and a directory-based query/list template protocol.

## Scope

This change covers:

- `/备用线路`, `/备用`, and `/bak` commands plus an `add_mc_server_backup` LLM Tool.
- Backward-compatible storage of ordered backup addresses.
- Failover for saved-server command and Tool queries.
- All-line aggregation during silent polling.
- Image rendering for `/列表` and parameterless `/查询`.
- Directory-based template discovery with separate query and list renderers.
- Migration of `default_method` and `terminal_method` to the new template layout.
- README, command help, Tool documentation, metadata, and version constants.

This change does not add backup-line deletion or reordering commands. Deleting a logical server removes all its lines; redirecting it changes only its primary line.

## Persistent Data Model

The existing `sessions.<session>.servers` mapping remains keyed by the primary address. Each value gains an ordered `backup_addresses` list:

```python
{
    "sessions": {
        "session-key": {
            "template": "default_method",
            "servers": {
                "main.example.com:25565": {
                    "name": "生存服",
                    "address": "main.example.com:25565",
                    "backup_addresses": [
                        "backup-a.example.com:25565",
                        "backup-b.example.com:25565",
                    ],
                    "latency_history": [],
                    "last_latency": 0,
                    "motd": "",
                    "last_silent_query_at": 0,
                    "last_active_query_at": 0,
                    "created_at": 0,
                }
            },
        }
    }
}
```

`store.get_or_create_session` normalizes old records by adding an empty list and removes malformed or duplicate backup entries. The primary address and all backup addresses must be unique across one session. Cross-session reuse remains allowed.

Small store helpers provide these operations consistently:

- Enumerate a logical server's lines in primary-first order.
- Enumerate every referenced address in a session or store.
- Find the logical server owning any saved line address.
- Check whether an address is already occupied in the session, with an optional excluded primary for redirect operations.

## Adding a Backup Line

The accepted command forms are:

```text
/备用线路 <已有服务器名称> <新地址>
/备用 <已有服务器名称> <新地址>
/bak <已有服务器名称> <新地址>
```

The LLM Tool is named `add_mc_server_backup` and accepts `server` and `address` string arguments.

Both entry points call one `_add_backup_server_data` business method. The method:

1. Requires a non-empty server name and address.
2. Resolves exactly one existing logical server by name.
3. Normalizes and validates the address and rejects private, reserved, loopback, link-local, multicast, or otherwise invalid targets.
4. Rejects an address already used by any primary or backup line in the current session.
5. Performs one status request before changing storage.
6. Reloads storage under the store lock and repeats ownership and uniqueness checks to avoid races.
7. Appends the normalized address to `backup_addresses`, preserving insertion order, and saves.
8. Updates the logical server's latest successful latency and MOTD, refreshes the new line's icon cache, and invalidates related rendered-image and Tool caches.

The validation request does not append a latency-history point; history remains the result of explicit active queries and combined silent polling.

Group chats always require an administrator for this command and Tool, independent of `mutation_requires_admin`. Private chats have no administrator requirement. Connection timeout, connection failure, invalid address, duplicate address, ambiguous name, missing server, and save failure receive distinct structured errors and command messages.

## Redirect, Delete, and Cache Ownership

`/重定向` replaces only the primary address after validating and successfully querying the new address. It preserves the server name, history, metadata, and ordered backups. The new primary must be unique across all lines in the current session. The old primary cache is removed only if no session still references it.

Deleting a logical server or clearing a session collects its primary and backup addresses. It invalidates logical query caches and deletes address caches only when the addresses are no longer referenced by another session. Global cache reference collection therefore includes both primary and backup addresses.

## Active Query Failover

Saved-server lookup by name, primary address, or backup address resolves to one logical server. Command queries and `query_mc_server` then try lines sequentially:

1. Primary address.
2. Backup addresses in insertion order.
3. Stop after the first successful response.

The reusable failover result contains the status, actual address, and line type (`primary` or `backup`). Successful command rendering displays the actual address. Successful Tool output includes:

```json
{
  "primary_address": "main.example.com:25565",
  "address": "backup-a.example.com:25565",
  "line_type": "backup"
}
```

Successful active queries update the logical server's `last_latency`, `last_active_query_at`, MOTD, and history. Icons and player avatars are cached under the actual responding address. The rendered-result cache remains keyed by the logical primary address.

When every line fails, a single-server command renders the existing Offline query image. It displays the primary address and uses cached MOTD, history, and the first cached icon found in primary-first line order. This transient active-query failure is included in the rendered history as zero but is not persisted. The Tool returns an offline structured error with the primary address and attempted addresses.

Direct queries for addresses not owned by a saved logical server retain the existing direct-query permission and behavior.

## Full `/查询`

Parameterless `/查询` and `/查询服务器` query logical servers concurrently, limited by `query_all_concurrency`. Each logical server performs the same sequential primary-first failover and requests player samples.

For a successful server, the operation:

- Stores the successful latency, MOTD, active-query timestamp, and history on the logical server.
- Caches the actual line's icon and player avatars.
- Supplies the name, actual address, line type, latency, version, online counts, icon, and players to the list renderer.

For an all-lines failure, it supplies an Offline card using the primary address and available cached icon. The failure is not persisted as an active-query history point. The command returns one vertically composed base64 image rather than summary or failure text.

## Silent Polling

Each silent round queries every primary and backup line for every logical server. Shared addresses across sessions may still be deduplicated at the network-request layer, but each logical server produces exactly one history result:

- If one or more lines respond, store the maximum successful latency.
- If every line fails, store latency `0` to represent Offline.

The logical server's `last_silent_query_at` is updated in both cases. A successful round updates `last_latency` and MOTD using the status associated with the maximum latency; an all-failed round sets `last_latency` to zero and retains the last MOTD. `query_history_status` continues reading the logical server's single combined history.

## `/列表` Cache-Only View

`/列表` and `/服务器列表` never perform a network request. They load saved logical servers and render one image using the selected list renderer.

Each server card includes:

- Cached server icon, selected in primary-first line order.
- Template default icon or generated fallback when no line has a cached icon.
- Server name and primary address.
- Cached `last_latency` as the recent latency.
- Every line, one row per address, labelled as primary or backup and ordered primary first.

## Template Directory Protocol

The shipped layout becomes:

```text
templates/
├── __init__.py
├── default_method/
│   ├── default_method_query.py
│   ├── default_method_list.py
│   ├── default_method.png
│   ├── default_icon.png
│   └── HarmonyOS_SansSC_Medium.ttf
└── terminal_method/
    ├── terminal_method_query.py
    └── terminal_method_list.py
```

A usable immediate child directory of `templates` must contain exactly one `*_query.py` and one `*_list.py`. The public template name is the query filename without `_query`. Invalid public names, missing or duplicate renderer roles, and more than one `.ttf` file make the directory unavailable and omit it from `/模板`.

Background files are optional and must be named after the public template name with a case-insensitive `.png`, `.jpg`, or `.jpeg` extension. A default icon is optional and must be named `default_icon` with one of the same extensions. A template may contain zero or one `.ttf`; renderers retain system-font and generated-icon fallbacks.

The loader exposes query and list renderer modes and caches each renderer by path and modification time. Query modules export the existing asynchronous `render_server_report_image`. List modules export an asynchronous `render_server_list_image`. `/模板重载` and compatibility `/模板 reload` clear discovery, renderer, and rendered-image caches.

`terminal_method_list.py` is a thin adapter that imports and calls the default list renderer with a 1100-pixel canvas. It does not duplicate the list drawing implementation. The default list renderer uses a 900-pixel canvas.

## List Renderer Visual Design

The default list renderer follows the supplied reference and the current `default_method` visual language:

- One server card consists of a header rounded rectangle and a content rounded rectangle.
- The reference image's purple positioning guides are not rendered.
- Header: icon and identity on the left; current/recent latency and online count on the right where available.
- `/列表` content: all line addresses, one row each.
- Full `/查询` content: player avatar and name, one player per row; an empty-state row is used when no players are available.
- Cards are vertically stacked with exactly 20 pixels between cards.
- The output width equals the selected query renderer width: 900 pixels for default and 1100 pixels for terminal.
- The optional template background covers the complete composed image; absent backgrounds use the renderer's solid-color fallback.

## Compatibility and Versioning

Existing sessions remain readable without an explicit migration job. Existing `default_method` and `terminal_method` selections keep the same public names. Existing single-query rendering behavior is retained except for saved-server failover and actual-line display.

The release version is updated to `v2.0.1` in `metadata.yaml`, `main.py`, and user-facing documentation. README and help output document the backup commands, Tool, failover behavior, cache-only list, image-based full query, and new template layout.

## Testing and Verification

Automated tests use `unittest.IsolatedAsyncioTestCase` and cover:

- Old-store normalization and ordered backup persistence.
- Session-wide uniqueness across primary and backup addresses.
- Backup command and Tool group/private permission boundaries.
- Validation, connection, ambiguity, race recheck, and save-failure behavior.
- Primary success, ordered fallback, all-lines Offline, and Tool metadata.
- Full-query logical concurrency and image-result behavior.
- Silent polling maximum-latency aggregation and all-lines Offline history.
- Redirect preservation and delete/clear cache ownership across every line.
- `/列表` proving that no network status method is called.
- Template discovery, invalid-directory rejection, role-specific loading, and hot reload.
- Base64/Pillow smoke tests for 900- and 1100-pixel widths and multi-card composition.

Visual verification renders deterministic fixtures for `/列表` and full `/查询`, opens the resulting PNGs, and checks card alignment, 20-pixel gaps, text clipping, icon/avatar fallbacks, background composition, and absence of purple guide lines.
