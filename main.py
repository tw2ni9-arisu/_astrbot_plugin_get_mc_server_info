"""Main plugin workflow for Minecraft server management.

This module focuses on orchestration and state management:
1. Session-scoped server storage (group/private isolated).
2. Periodic silent polling and latency history updates.
3. Active queries (single/all) and result assembly.
4. Cache lifecycle management for icon/skin/avatar assets.
5. Template dispatching for image rendering.

Rendering details are delegated to `templates/default_method.py`.
Persistent data is stored via plugin KV (`get_kv_data` / `put_kv_data`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import inspect
import io
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import aiohttp
from mcstatus import JavaServer
from PIL import Image

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

try:
    import PILSkinMC as _PILSKINMC
except Exception:
    _PILSKINMC = None

ADD_SERVER_PATTERN = re.compile(r"^#添加服务器\s+(\S+)\s+(\S+)\s*$")
QUERY_SERVER_PATTERN = re.compile(r"^#查询服务器(?:\s+(\S+))?\s*$")
TEMPLATE_PATTERN = re.compile(r"^#模板(?:\s+(\S+))?\s*$")

# 默认补全端口（Minecraft Java Edition 常见端口）
DEFAULT_PORT = 25565
# 静默轮询间隔：30 分钟
SILENT_QUERY_INTERVAL_SECONDS = 30 * 60
# 仅保留最近 48 个延迟点（刚好对应 24 小时，30 分钟/点）
HISTORY_LIMIT = 48
# 图片缓存有效期：24 小时
CACHE_TTL_SECONDS = 24 * 60 * 60
# 向 MC 服务端拉取状态的超时
STATUS_TIMEOUT = 10
# 头像下载尺寸（像素）
SKIN_SIZE = 32
# 默认渲染模板（对应 templates/default_method.py）
DEFAULT_TEMPLATE_NAME = "default_method"
# 全服主动查询并发上限
QUERY_ALL_CONCURRENCY = 5
# 头像下载并发上限
AVATAR_DOWNLOAD_CONCURRENCY = 5
# 头像下载重试次数（总尝试次数 = 1 + retries）
AVATAR_DOWNLOAD_RETRIES = 2
# 皮肤接口（按 UUID 获取玩家皮肤）
SKIN_API_URL_TEMPLATE = "https://skin.mualliance.ltd/api/union/skin/byuuid/{uuid}"


@dataclass
class ServerStatus:
    """标准化后的服务器状态结构。

    该数据类是查询层和业务层之间的统一数据载体，
    避免后续逻辑直接依赖 mcstatus 的原始对象结构。
    """

    address: str
    latency: int
    version: str
    players_online: int
    players_max: int
    icon_base64: str | None
    players: list[dict[str, str]]


@dataclass
class TemplateRendererEntry:
    """模板渲染器缓存项。"""

    mtime: float
    renderer: Callable[..., Awaitable[str]]


class Main(Star):
    """插件入口类。

    生命周期：
    - initialize: 建立缓存目录、初始化 HTTP 会话、启动静默轮询协程；
    - terminate: 停止后台任务并关闭会话。
    """

    def __init__(self, context: Context, config: Any | None = None) -> None:
        super().__init__(context, config=config)
        # 保护存储读写，避免并发命令与后台轮询同时改写数据
        self._store_lock = asyncio.Lock()
        # 插件配置（由 _conf_schema.json 驱动）
        self._plugin_config = config if config is not None else {}
        # 复用 HTTP 会话，用于拉取玩家头像
        self._session: aiohttp.ClientSession | None = None
        # 头像下载并发控制
        self._avatar_download_semaphore: asyncio.Semaphore | None = None
        # 静默查询后台任务
        self._silent_task: asyncio.Task | None = None
        # 缓存根目录（位于 AstrBot temp 目录下）
        self._cache_root = (
            Path(get_astrbot_temp_path()) / "astrbot_plugin_get_mc_server_info"
        )
        # 模板目录（存放渲染方法）
        self._templates_dir = Path(__file__).resolve().parent / "templates"
        # 模板渲染函数缓存，避免每次查询都重复加载文件
        self._template_renderer_cache: dict[str, TemplateRendererEntry] = {}
        # 运行时配置（支持插件配置覆盖）
        self.silent_query_interval_seconds = SILENT_QUERY_INTERVAL_SECONDS
        self.history_limit = HISTORY_LIMIT
        self.cache_ttl_seconds = CACHE_TTL_SECONDS
        self.status_timeout_seconds = STATUS_TIMEOUT
        self.query_all_concurrency = QUERY_ALL_CONCURRENCY
        self.avatar_download_concurrency = AVATAR_DOWNLOAD_CONCURRENCY
        self.avatar_download_retries = AVATAR_DOWNLOAD_RETRIES
        self.skin_api_url_template = SKIN_API_URL_TEMPLATE

    async def initialize(self) -> None:
        """插件初始化：创建目录、建立会话、启动后台任务。"""
        self._load_runtime_config()
        self._cache_root.mkdir(parents=True, exist_ok=True)
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        self._avatar_download_semaphore = asyncio.Semaphore(
            self.avatar_download_concurrency
        )
        # 防止重复 initialize 导致创建多个后台轮询任务
        if self._silent_task is None or self._silent_task.done():
            self._silent_task = asyncio.create_task(self._silent_query_loop())
        logger.info("astrbot_plugin_get_mc_server_info initialized.")

    async def terminate(self) -> None:
        """插件销毁：优雅停止后台任务并释放网络资源。"""
        if self._silent_task:
            self._silent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._silent_task
            self._silent_task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._avatar_download_semaphore = None
        logger.info("astrbot_plugin_get_mc_server_info terminated.")

    @filter.regex(r"^#添加服务器\s+\S+\s+\S+\s*$")
    async def add_server(self, event: AstrMessageEvent):
        """添加 MC 服务器：#添加服务器 <服务器名称> <服务器地址>"""
        # 1) 解析与格式校验
        matched = ADD_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(
                "参数格式错误：#添加服务器 <服务器名称> <服务器地址>"
            )
            return

        name = matched.group(1).strip()
        address = self._normalize_address(matched.group(2).strip())
        # 以 unified_msg_origin 作为“会话隔离键”
        session_key = event.unified_msg_origin

        # 2) 先连通性验证，防止不可达服务器进入存储
        try:
            status = await self._fetch_server_status(address, need_players=False)
        except Exception:
            yield event.plain_result("添加失败！服务器连接失败")
            return

        # 3) 写入存储（加锁）
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers = session_obj["servers"]
            if address in servers:
                yield event.plain_result("添加失败！该服务器已存在")
                return

            now = int(time.time())
            servers[address] = {
                "name": name,
                "address": address,
                "latency_history": [],
                "last_latency": status.latency,
                "last_silent_query_at": 0,
                "last_active_query_at": 0,
                "created_at": now,
            }

            self._append_latency(servers[address], status.latency, now)
            await self._save_store(store)

        # 4) 尝试缓存图标（失败不影响主流程）+ 触发一次过期清理
        await self._cache_server_icon(address, status.icon_base64)
        await self._cleanup_expired_cache()
        yield event.plain_result(f"添加成功！服务器 [{name}] 已添加")

    @filter.regex(r"^#查询服务器(?:\s+\S+)?\s*$")
    async def query_server(self, event: AstrMessageEvent):
        """查询 MC 服务器：#查询服务器 [服务器地址]"""
        # 无参数 => 查询当前会话全部服务器
        # 有参数 => 查询当前会话内指定服务器
        matched = QUERY_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result("参数格式错误：#查询服务器 [服务器地址]")
            return

        address_arg = matched.group(1)
        if address_arg:
            yield await self._query_single_server(
                event, self._normalize_address(address_arg)
            )
            return

        summary, failures = await self._query_all_servers(event)
        for failure in failures:
            yield event.plain_result(failure)
        yield event.plain_result(summary)

    @filter.regex(r"^#模板(?:\s+\S+)?\s*$")
    async def switch_template(self, event: AstrMessageEvent):
        """模板切换命令。

        - `#模板`：列出 templates 目录下的全部模板名（不带 .py）。
        - `#模板 <模板名>`：切换当前会话模板。
        """
        matched = TEMPLATE_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result("切换失败！未找到模板！")
            return

        template_name = matched.group(1)
        if not template_name:
            names = self._list_templates()
            output = "已有模板如下："
            if names:
                output += "\n" + "\n".join(names)
            yield event.plain_result(output)
            return

        # 手动清理模板缓存：#模板 reload
        if template_name == "reload":
            self._template_renderer_cache.clear()
            yield event.plain_result("模板缓存已重载")
            return

        if not self._is_valid_template_name(template_name):
            yield event.plain_result("切换失败！未找到模板！")
            return

        try:
            await self._get_template_renderer(template_name)
        except Exception as exc:
            logger.warning("template load failed: %s", exc)
            yield event.plain_result("切换失败！未找到模板！")
            return

        session_key = event.unified_msg_origin
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            session_obj["template"] = template_name
            await self._save_store(store)

        yield event.plain_result(f"已切换至 {template_name}")

    async def _query_single_server(self, event: AstrMessageEvent, address: str):
        """主动查询单个服务器并返回渲染图。

        注意：
        - 必须先校验“当前会话是否已添加该服务器”；
        - 主动查询得到的 latency 会立即写入历史；
        - 图标和玩家头像会执行缓存刷新。
        """
        session_key = event.unified_msg_origin
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            server_obj = session_obj["servers"].get(address)
            template_name = str(
                session_obj.get("template", DEFAULT_TEMPLATE_NAME)
                or DEFAULT_TEMPLATE_NAME
            )

        if not server_obj:
            return event.plain_result("查询失败！群聊内无该服务器")

        # 1) 拉取服务端状态（含玩家 sample）
        try:
            status = await self._fetch_server_status(address, need_players=True)
        except Exception:
            return event.plain_result(f"服务器 [{server_obj['name']}] 查询失败！")

        # 2) 刷新图标与玩家头像缓存
        now = int(time.time())
        await self._cache_server_icon(address, status.icon_base64)
        players_for_render = await self._cache_and_collect_player_avatars(
            address,
            status.players,
        )

        # 3) 写回最新延迟与历史
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            real_server_obj = session_obj["servers"].get(address)
            if not real_server_obj:
                return event.plain_result("查询失败！群聊内无该服务器")
            real_server_obj["last_latency"] = status.latency
            real_server_obj["last_active_query_at"] = now
            self._append_latency(real_server_obj, status.latency, now)
            history = list(real_server_obj["latency_history"])
            await self._save_store(store)

        # 4) 清理过期缓存并生成渲染图
        await self._cleanup_expired_cache()
        icon_path = self._icon_cache_path(address)
        renderer = await self._get_template_renderer(template_name)
        image_b64 = await renderer(
            server_name=server_obj["name"],
            server_address=address,
            latency=status.latency,
            players_online=status.players_online,
            players_max=status.players_max,
            server_version=status.version,
            history=history,
            icon_path=str(icon_path) if icon_path.exists() else None,
            players=players_for_render,
        )
        return event.make_result().base64_image(image_b64)

    async def _query_all_servers(
        self, event: AstrMessageEvent
    ) -> tuple[str, list[str]]:
        """主动查询当前会话下全部服务器。

        Returns:
            tuple[str, list[str]]:
            - summary: 汇总文本（多行）
            - failures: 失败提示列表（按需求单独回传）
        """
        session_key = event.unified_msg_origin
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = dict(session_obj["servers"])

        if not servers:
            return "当前会话暂无已添加服务器", []

        results: list[str] = []
        failures: list[str] = []
        now = int(time.time())
        semaphore = asyncio.Semaphore(self.query_all_concurrency)

        async def _query_one(address: str, server_obj: dict[str, Any]):
            async with semaphore:
                try:
                    status = await self._fetch_server_status(
                        address, need_players=False
                    )
                    return address, server_obj, status, None
                except Exception:
                    return (
                        address,
                        server_obj,
                        None,
                        f"服务器 [{server_obj['name']}] 查询失败！",
                    )

        queried = await asyncio.gather(
            *[
                _query_one(address, server_obj)
                for address, server_obj in servers.items()
            ]
        )

        successful_status: dict[str, ServerStatus] = {}
        for address, server_obj, status, fail_msg in queried:
            if fail_msg:
                failures.append(fail_msg)
                continue
            assert status is not None
            successful_status[address] = status
            results.append(
                f"{server_obj['name']}: 延迟 : {status.latency}ms | 玩家人数 : {status.players_online}/{status.players_max}"
            )
            await self._cache_server_icon(address, status.icon_base64)

        # 单次合并写入，减少高频全量写
        if successful_status:
            async with self._store_lock:
                store = await self._load_store()
                session_obj = self._get_or_create_session(store, session_key)
                for address, status in successful_status.items():
                    real_server_obj = session_obj["servers"].get(address)
                    if not real_server_obj:
                        continue
                    real_server_obj["last_latency"] = status.latency
                    real_server_obj["last_active_query_at"] = now
                    self._append_latency(real_server_obj, status.latency, now)
                await self._save_store(store)

        await self._cleanup_expired_cache()
        output = "\n".join(results) if results else "本次无可用服务器结果"
        return output, failures

    async def _silent_query_loop(self) -> None:
        """静默轮询主循环。

        设计要点：
        - 永久循环，异常吞吐后继续；
        - 每轮结束执行一次缓存清理；
        - 间隔固定 30 分钟。
        """
        while True:
            try:
                await self._silent_query_once()
                await self._cleanup_expired_cache()
            except Exception as exc:
                logger.warning(f"silent query loop error: {exc}")
            await asyncio.sleep(self.silent_query_interval_seconds)

    async def _silent_query_once(self) -> None:
        """执行一轮静默查询。

        去重策略：
        - 先聚合“地址 -> 会话列表”；
        - 相同地址仅查询一次，再同步写回多个会话。
        """
        async with self._store_lock:
            store = await self._load_store()
            sessions = store.get("sessions", {})

        # Build a reverse index so the same address is queried only once per round.
        address_to_sessions: dict[str, list[str]] = {}
        for session_key, session_obj in sessions.items():
            for address in session_obj.get("servers", {}):
                address_to_sessions.setdefault(address, []).append(session_key)

        if not address_to_sessions:
            return

        now = int(time.time())
        for address, related_sessions in address_to_sessions.items():
            # 静默失败直接跳过，不产生用户侧噪音
            try:
                status = await self._fetch_server_status(address, need_players=False)
            except Exception as exc:
                logger.warning("silent query failed for %s: %s", address, exc)
                continue

            async with self._store_lock:
                store = await self._load_store()
                for session_key in related_sessions:
                    session_obj = self._get_or_create_session(store, session_key)
                    server_obj = session_obj["servers"].get(address)
                    if not server_obj:
                        continue
                    server_obj["last_latency"] = status.latency
                    server_obj["last_silent_query_at"] = now
                    self._append_latency(server_obj, status.latency, now)
                await self._save_store(store)

    async def _fetch_server_status(
        self,
        address: str,
        *,
        need_players: bool,
    ) -> ServerStatus:
        """请求并标准化服务器状态。

        Args:
            address: host:port 形式地址
            need_players: 是否读取在线玩家 sample（主动单服查询时为 True）
        """
        server = JavaServer.lookup(address)

        try:
            status = await asyncio.wait_for(
                server.async_status(), timeout=self.status_timeout_seconds
            )
        except Exception as exc:
            raise RuntimeError("server status failed") from exc

        # favicon 通常是 data:image/png;base64,xxxxx
        icon_base64 = None
        if getattr(status, "favicon", None):
            icon_base64 = str(status.favicon)

        players: list[dict[str, str]] = []
        if need_players:
            sample_players = getattr(status.players, "sample", None) or []
            for player in sample_players:
                player_name = getattr(player, "name", "") or ""
                player_uid = getattr(player, "id", "") or ""
                if not player_name:
                    continue
                # 若服务端未返回 UUID，退化为 name 的稳定散列，便于缓存命名
                if not player_uid:
                    player_uid = hashlib.md5(player_name.encode("utf-8")).hexdigest()
                players.append({"name": player_name, "uid": player_uid})

        latency = int(round(getattr(status, "latency", 0) or 0))
        version = (
            getattr(status.version, "name", "Unknown") if status.version else "Unknown"
        )
        return ServerStatus(
            address=address,
            latency=max(latency, 0),
            version=version,
            players_online=int(getattr(status.players, "online", 0) or 0),
            players_max=int(getattr(status.players, "max", 0) or 0),
            icon_base64=icon_base64,
            players=players,
        )

    async def _cache_server_icon(self, address: str, icon_base64: str | None) -> None:
        """缓存服务器图标（icon.png）。

        图标缓存失败不抛错，避免影响主业务链路。
        """
        if not icon_base64:
            return
        payload = icon_base64
        if "," in payload:
            payload = payload.split(",", 1)[1]
        try:
            raw = base64.b64decode(payload)
        except Exception:
            return

        icon_path = self._icon_cache_path(address)
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        icon_path.write_bytes(raw)

    async def _cache_and_collect_player_avatars(
        self,
        address: str,
        players: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """下载并缓存玩家头像，同时返回渲染所需结构。

        返回结构:
            [{"name": "<玩家名>", "avatar_path": "<本地文件或空串>"}]
        """
        if not players:
            return []

        if not self._session:
            return [{"name": player["name"], "avatar_path": ""} for player in players]

        now = int(time.time())
        semaphore = self._avatar_download_semaphore or asyncio.Semaphore(
            self.avatar_download_concurrency
        )

        async def _resolve_one(player: dict[str, str]) -> dict[str, str]:
            name = player["name"]
            uid = player["uid"]
            avatar_path = self._skin_cache_path(address, uid)
            avatar_path.parent.mkdir(parents=True, exist_ok=True)

            # 未过期则直接复用缓存
            if (
                avatar_path.exists()
                and now - int(avatar_path.stat().st_mtime) <= self.cache_ttl_seconds
            ):
                return {"name": name, "avatar_path": str(avatar_path)}

            # 新逻辑：先拉取皮肤图，再用 PILSkinMC 渲染为头像
            _ = await self._download_and_render_avatar_by_uuid(
                uid=uid,
                avatar_path=avatar_path,
                semaphore=semaphore,
            )

            return {
                "name": name,
                "avatar_path": str(avatar_path) if avatar_path.exists() else "",
            }

        return await asyncio.gather(*[_resolve_one(player) for player in players])

    async def _cleanup_expired_cache(self) -> None:
        """清理过期缓存。

        规则：
        1) 某服务器超过 24h 未主动查询：清理该服务器目录下全部缓存；
        2) 否则仅清理超过 24h 的头像与图标文件。
        """
        now = int(time.time())
        async with self._store_lock:
            store = await self._load_store()
            sessions = store.get("sessions", {})
            session_server_map: dict[str, dict[str, Any]] = {}
            for session_obj in sessions.values():
                for address, server_obj in session_obj.get("servers", {}).items():
                    session_server_map[address] = server_obj

        for address, server_obj in session_server_map.items():
            cache_dir = self._server_cache_dir(address)
            if not cache_dir.exists():
                continue

            last_active_query_at = int(server_obj.get("last_active_query_at", 0) or 0)
            created_at = int(server_obj.get("created_at", 0) or 0)
            # 以“最近一次主动查询时间”为准；若从未主动查询，则回退到创建时间。
            # 这样可满足“24h 内未查询则清理该服务器全部缓存”的需求。
            last_touch_ts = (
                last_active_query_at if last_active_query_at > 0 else created_at
            )
            if last_touch_ts > 0 and now - last_touch_ts > self.cache_ttl_seconds:
                for file_path in cache_dir.rglob("*"):
                    if file_path.is_file():
                        file_path.unlink(missing_ok=True)
                continue

            for skin_file in cache_dir.joinpath("skins").glob("*.png"):
                if now - int(skin_file.stat().st_mtime) > self.cache_ttl_seconds:
                    skin_file.unlink(missing_ok=True)

            icon_file = cache_dir.joinpath("icon.png")
            if (
                icon_file.exists()
                and now - int(icon_file.stat().st_mtime) > self.cache_ttl_seconds
            ):
                icon_file.unlink(missing_ok=True)

    async def _load_store(self) -> dict[str, Any]:
        """读取插件存储。

        统一保证返回至少包含：
            {"sessions": {}}
        """
        data = await self.get_kv_data("session_servers", {"sessions": {}})
        if not isinstance(data, dict):
            return {"sessions": {}}
        data.setdefault("sessions", {})
        return data

    async def _save_store(self, data: dict[str, Any]) -> None:
        """写回插件存储（会话级合并）。

        说明：
        - 不直接覆盖整个对象；
        - 只将传入 data 中的 sessions 合并到当前存储，降低并发覆盖风险。
        """
        # Merge by session key instead of blind full overwrite, reducing lost-update risk
        # when concurrent operations touch different sessions.
        current = await self.get_kv_data("session_servers", {"sessions": {}})
        if not isinstance(current, dict):
            current = {"sessions": {}}
        current_sessions = current.setdefault("sessions", {})
        incoming_sessions = data.get("sessions", {})
        if isinstance(incoming_sessions, dict):
            current_sessions.update(incoming_sessions)
        await self.put_kv_data("session_servers", current)

    @staticmethod
    def _get_or_create_session(
        store: dict[str, Any], session_key: str
    ) -> dict[str, Any]:
        """获取或初始化会话对象。"""
        sessions = store.setdefault("sessions", {})
        session_obj = sessions.setdefault(session_key, {})
        session_obj.setdefault("servers", {})
        session_obj.setdefault("template", DEFAULT_TEMPLATE_NAME)
        return session_obj

    def _list_templates(self) -> list[str]:
        """列出模板目录中的可用模板名（不带 .py）。"""
        if not self._templates_dir.exists():
            return []
        names: list[str] = []
        for path in self._templates_dir.glob("*.py"):
            if path.name == "__init__.py":
                continue
            names.append(path.stem)
        names.sort()
        return names

    @staticmethod
    def _is_valid_template_name(name: str) -> bool:
        """模板名合法性校验。

        只允许 Python 标识符风格，避免路径穿越和非法导入。
        """
        return bool(name) and name.isidentifier()

    def _template_file_path(self, template_name: str) -> Path:
        """根据模板名获取模板文件路径。"""
        return self._templates_dir / f"{template_name}.py"

    async def _get_template_renderer(
        self, template_name: str
    ) -> Callable[..., Awaitable[str]]:
        """获取模板渲染函数。

        约定模板文件必须提供：
            async def render_server_report_image(...)
        """
        if not self._is_valid_template_name(template_name):
            raise ValueError("invalid template name")

        template_file = self._template_file_path(template_name)
        if not template_file.exists():
            raise FileNotFoundError(str(template_file))
        current_mtime = template_file.stat().st_mtime

        cached = self._template_renderer_cache.get(template_name)
        if cached and cached.mtime == current_mtime:
            return cached.renderer

        module_name = f"astrbot_plugin_get_mc_server_info_template_{template_name}"
        spec = importlib.util.spec_from_file_location(module_name, template_file)
        if not spec or not spec.loader:
            raise RuntimeError("cannot build module spec")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        renderer = getattr(module, "render_server_report_image", None)
        if not renderer or not callable(renderer):
            raise AttributeError("missing render_server_report_image")
        if not inspect.iscoroutinefunction(renderer):
            raise TypeError("render_server_report_image must be async")

        self._template_renderer_cache[template_name] = TemplateRendererEntry(
            mtime=current_mtime,
            renderer=renderer,
        )
        return renderer

    def _load_runtime_config(self) -> None:
        """读取插件配置并覆盖运行时参数。"""
        self.silent_query_interval_seconds = self._get_config_int(
            "silent_query_interval_seconds",
            SILENT_QUERY_INTERVAL_SECONDS,
            min_value=60,
        )
        self.history_limit = self._get_config_int(
            "history_limit",
            HISTORY_LIMIT,
            min_value=1,
        )
        self.cache_ttl_seconds = self._get_config_int(
            "cache_ttl_seconds",
            CACHE_TTL_SECONDS,
            min_value=60,
        )
        self.status_timeout_seconds = self._get_config_int(
            "status_timeout_seconds",
            STATUS_TIMEOUT,
            min_value=1,
        )
        self.query_all_concurrency = self._get_config_int(
            "query_all_concurrency",
            QUERY_ALL_CONCURRENCY,
            min_value=1,
        )
        self.avatar_download_concurrency = self._get_config_int(
            "avatar_download_concurrency",
            AVATAR_DOWNLOAD_CONCURRENCY,
            min_value=1,
        )
        self.avatar_download_retries = self._get_config_int(
            "avatar_download_retries",
            AVATAR_DOWNLOAD_RETRIES,
            min_value=0,
        )
        self.skin_api_url_template = self._normalize_skin_api_url_template(
            self._get_config_str("skin_api_url_template", SKIN_API_URL_TEMPLATE)
        )

    def _get_config_int(self, key: str, default: int, *, min_value: int = 0) -> int:
        """读取整型配置并做下限保护。"""
        raw = None
        if hasattr(self._plugin_config, "get"):
            raw = self._plugin_config.get(key, default)
        elif isinstance(self._plugin_config, dict):
            raw = self._plugin_config.get(key, default)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(value, min_value)

    def _get_config_str(self, key: str, default: str) -> str:
        """读取字符串配置并做空值保护。"""
        raw = None
        if hasattr(self._plugin_config, "get"):
            raw = self._plugin_config.get(key, default)
        elif isinstance(self._plugin_config, dict):
            raw = self._plugin_config.get(key, default)
        if raw is None:
            return default
        value = str(raw).strip()
        return value or default

    @staticmethod
    def _normalize_skin_api_url_template(template: str) -> str:
        """校验皮肤 API URL 模板，必须包含 {uuid} 占位符。"""
        if "{uuid}" not in template:
            return SKIN_API_URL_TEMPLATE
        return template

    def _append_latency(
        self, server_obj: dict[str, Any], latency: int, now_ts: int
    ) -> None:
        """追加延迟历史并裁剪到固定长度。"""
        history = server_obj.setdefault("latency_history", [])
        history.append({"timestamp": now_ts, "latency": int(latency)})
        if len(history) > self.history_limit:
            server_obj["latency_history"] = history[-self.history_limit :]

    @staticmethod
    def _normalize_address(address: str) -> str:
        """标准化服务器地址。

        - 缺省端口时补 25565；
        - 端口非数字时回退为默认端口。
        """
        address = address.strip()
        if ":" not in address:
            return f"{address}:{DEFAULT_PORT}"
        host, port_str = address.rsplit(":", 1)
        if not port_str.isdigit():
            return f"{address}:{DEFAULT_PORT}"
        return f"{host}:{int(port_str)}"

    @staticmethod
    def _address_hash(address: str) -> str:
        """将地址映射为稳定哈希，用作缓存目录名。"""
        return hashlib.sha1(address.encode("utf-8")).hexdigest()

    def _server_cache_dir(self, address: str) -> Path:
        """服务器缓存目录。"""
        return self._cache_root / self._address_hash(address)

    def _icon_cache_path(self, address: str) -> Path:
        """服务器图标缓存路径。"""
        return self._server_cache_dir(address) / "icon.png"

    def _skin_cache_path(self, address: str, uid: str) -> Path:
        """玩家头像缓存路径。"""
        return self._server_cache_dir(address) / "skins" / f"{uid}.png"

    async def _download_and_render_avatar_by_uuid(
        self,
        *,
        uid: str,
        avatar_path: Path,
        semaphore: asyncio.Semaphore,
    ) -> bool:
        """通过 UUID 拉取皮肤并渲染头像。

        流程：
        1) 调用 skin.mualliance.ltd API 获取皮肤图；
        2) 使用 PILSkinMC 渲染成玩家立体头像；
        3) 缩放到 SKIN_SIZE 并缓存为 PNG。
        """
        if not self._session:
            return False

        # Collect compact failure reasons for operation visibility and diagnostics.
        failed_reasons: list[str] = []
        for candidate_uuid in self._build_uuid_candidates(uid):
            url = self.skin_api_url_template.format(uuid=candidate_uuid)
            for attempt in range(self.avatar_download_retries + 1):
                should_retry = attempt < self.avatar_download_retries
                retry_after_seconds: float | None = None
                try:
                    async with semaphore:
                        async with self._session.get(url) as resp:
                            if resp.status == 200:
                                raw = await resp.read()
                                if self._render_avatar_from_skin_bytes(
                                    skin_bytes=raw,
                                    avatar_path=avatar_path,
                                ):
                                    return True
                                # 即使状态码 200，内容也可能非有效皮肤；直接放弃该候选 UUID
                                failed_reasons.append(
                                    f"{candidate_uuid}:200_invalid_skin"
                                )
                                should_retry = False
                                break
                            # 404 表示该 UUID 没有皮肤记录，尝试下一个 UUID 候选
                            if resp.status == 404:
                                failed_reasons.append(f"{candidate_uuid}:404")
                                should_retry = False
                                break
                            if resp.status == 429:
                                failed_reasons.append(f"{candidate_uuid}:429")
                                retry_after_seconds = self._parse_retry_after_seconds(
                                    resp.headers.get("Retry-After")
                                )
                            # 4xx(除 429)通常不适合重试
                            elif resp.status < 500:
                                failed_reasons.append(f"{candidate_uuid}:{resp.status}")
                                should_retry = False
                                break
                            else:
                                failed_reasons.append(f"{candidate_uuid}:{resp.status}")
                except Exception as exc:
                    failed_reasons.append(f"{candidate_uuid}:exc:{type(exc).__name__}")

                if should_retry:
                    # Respect Retry-After on 429 when available; otherwise use short backoff.
                    await asyncio.sleep(
                        retry_after_seconds
                        if retry_after_seconds is not None
                        else 0.2 * (attempt + 1)
                    )
                else:
                    break

        logger.warning(
            "avatar download/render failed for uid=%s, reasons=%s",
            uid,
            "; ".join(failed_reasons[:6]) if failed_reasons else "unknown",
        )
        return False

    @staticmethod
    def _parse_retry_after_seconds(retry_after: str | None) -> float | None:
        """解析 Retry-After 头，返回秒数。"""
        if not retry_after:
            return None
        raw = retry_after.strip()
        try:
            # 数字秒（最常见）
            sec = int(raw)
            return float(max(sec, 0))
        except ValueError:
            pass
        try:
            # HTTP 日期
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            return float(max(delta, 0.0))
        except Exception:
            return None

    def _render_avatar_from_skin_bytes(
        self,
        *,
        skin_bytes: bytes,
        avatar_path: Path,
    ) -> bool:
        """将皮肤图渲染为头像并保存。

        渲染策略：
        1) 优先尝试 PILSkinMC 的对象式 API（兼容部分版本）；
        2) 若对象式 API 不可用，则回退为标准皮肤头部裁剪（含帽子层）；
        3) 若 PILSkinMC 缺失，不影响回退逻辑，仍可生成头像。
        """
        try:
            with Image.open(io.BytesIO(skin_bytes)) as skin_raw:
                skin = skin_raw.convert("RGBA")
                avatar = self._render_avatar_by_pilskinmc_object_api(skin_bytes)
                if avatar is None:
                    avatar = self._render_avatar_head_fallback(skin)
                avatar = avatar.resize((SKIN_SIZE, SKIN_SIZE), Image.Resampling.LANCZOS)
                avatar_path.parent.mkdir(parents=True, exist_ok=True)
                avatar.save(avatar_path, format="PNG")
            return True
        except Exception as exc:
            logger.debug("render avatar from skin failed: %s", exc)
            return False

    def _render_avatar_by_pilskinmc_object_api(
        self,
        skin_bytes: bytes,
    ) -> Image.Image | None:
        """尝试使用 PILSkinMC 的对象式 API 生成头像。

        兼容性说明：
        - 不同版本 PILSkinMC 的入口类/方法可能不同；
        - 这里仅在检测到可用 API 时调用，否则返回 None。
        """
        if _PILSKINMC is None:
            return None

        skin_cls = getattr(_PILSKINMC, "Skin", None)
        if skin_cls is None:
            return None

        try:
            if hasattr(skin_cls, "open") and callable(skin_cls.open):
                skin_obj = skin_cls.open(io.BytesIO(skin_bytes))
            else:
                skin_obj = skin_cls(io.BytesIO(skin_bytes))
        except Exception:
            return None

        # 方法式 API
        for method_name in (
            "get_avatar",
            "render_avatar",
            "render_head",
            "get_head",
        ):
            method = getattr(skin_obj, method_name, None)
            if not callable(method):
                continue
            try:
                sig = inspect.signature(method)
                if "size" in sig.parameters:
                    rendered = method(size=SKIN_SIZE)
                else:
                    rendered = method()
                if isinstance(rendered, Image.Image):
                    return rendered.convert("RGBA")
            except Exception:
                continue

        # 属性式 API（少数实现）
        for attr_name in ("avatar", "head"):
            value = getattr(skin_obj, attr_name, None)
            if isinstance(value, Image.Image):
                return value.convert("RGBA")

        return None

    def _render_avatar_head_fallback(self, skin: Image.Image) -> Image.Image:
        """标准皮肤头像回退渲染（前脸 + 帽子层）。"""
        work_skin = skin
        if _PILSKINMC is not None and hasattr(_PILSKINMC, "fix_legacy"):
            with contextlib.suppress(Exception):
                if work_skin.height == 32:
                    work_skin = _PILSKINMC.fix_legacy(work_skin)

        head = work_skin.crop((8, 8, 16, 16)).convert("RGBA")
        # 叠加帽子层
        if work_skin.width >= 48 and work_skin.height >= 16:
            overlay = work_skin.crop((40, 8, 48, 16)).convert("RGBA")
            head.alpha_composite(overlay)
        return head

    @staticmethod
    def _build_uuid_candidates(uid: str) -> list[str]:
        """构造 UUID 候选格式（兼容带/不带连字符）。"""
        raw = (uid or "").strip().lower()
        if not raw:
            return []
        candidates: list[str] = []
        if raw not in candidates:
            candidates.append(raw)

        no_dash = raw.replace("-", "")
        if len(no_dash) == 32:
            if no_dash not in candidates:
                candidates.append(no_dash)
            hyphen = (
                f"{no_dash[0:8]}-{no_dash[8:12]}-"
                f"{no_dash[12:16]}-{no_dash[16:20]}-{no_dash[20:32]}"
            )
            if hyphen not in candidates:
                candidates.append(hyphen)
        return candidates
