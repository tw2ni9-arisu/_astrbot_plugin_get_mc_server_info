"""Minecraft 服务器管理的主要插件工作流程。

这个模块主要关注编排和状态管理：
1. 会话范围的服务器存储（组/私有隔离）。
2. 定期静默轮询和延迟历史更新。
3. 活跃查询（单个/全部）及结果组装。
4. 图标/皮肤/头像资源的缓存生命周期管理。
5. 图片渲染的模板分发。

渲染细节委托给 `templates/default_method.py`。
持久化数据通过插件 KV 存储（`get_kv_data` / `put_kv_data`）。
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
import shutil
import time
import uuid
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

ADD_SERVER_PATTERN = re.compile(r"^#(?:添加服务器|添加)\s+(\S+)\s+(\S+)\s*$")
QUERY_SERVER_PATTERN = re.compile(r"^#(?:查询服务器|查询)(?:\s+(\S+))?\s*$")
DELETE_SERVER_PATTERN = re.compile(r"^#(?:删除服务器|删除)\s+(\S+)\s*$")
RENAME_SERVER_PATTERN = re.compile(r"^#(?:重命名服务器|重命名)\s+(\S+)\s+(\S+)\s*$")
LIST_SERVER_PATTERN = re.compile(r"^#(?:服务器列表|列表)\s*$")
TEMPLATE_PATTERN = re.compile(r"^#模板(?:\s+(\S+))?\s*$")
HELP_PATTERN = re.compile(r"^#(?:帮助|help)\s*$")
COMMAND_FALLBACK_PATTERN = re.compile(
    r"^#(?:添加服务器|添加|查询服务器|查询|删除服务器|删除|重命名服务器|重命名|服务器列表|列表|模板|帮助|help)(?:\s+.*)?$"
)
MOTD_FORMAT_CODE_PATTERN = re.compile(r"§.")

# 默认补全端口（Minecraft Java Edition 常见端口）
DEFAULT_PORT = 25565
# 是否自动补全默认端口（可被插件配置覆盖）
AUTO_APPEND_DEFAULT_PORT = False
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
# 单服查询结果渲染缓存时长（秒）
QUERY_RESULT_CACHE_TTL_SECONDS = 10
# 查询渲染缓存清理任务间隔（秒）
QUERY_CACHE_CLEANUP_INTERVAL_SECONDS = 5 * 60
# LLM Tool 返回结构版本
TOOL_VERSION = "1.2"
PLUGIN_VERSION = "v1.6.0"
# Tool 查询状态缓存，避免 Agent 连续追问时重复打到 MC 服务端
TOOL_STATUS_CACHE_TTL_SECONDS = 30
# Tool 列表缓存，避免 Agent 连续追问列表细节时重复读取存储
TOOL_LIST_CACHE_TTL_SECONDS = 5
# Tool 层限流：同一会话窗口内最多允许的工具调用次数
TOOL_RATE_LIMIT_WINDOW_SECONDS = 60
TOOL_RATE_LIMIT_MAX_CALLS = 20
# Tool 层并发查询上限
TOOL_QUERY_CONCURRENCY = 3


class McServerConnectionError(RuntimeError):
    """MC server lookup/status request failed."""


class McServerTimeoutError(McServerConnectionError):
    """MC server status request timed out."""


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
    motd: str


@dataclass
class TemplateRendererEntry:
    """模板渲染器缓存项。"""

    mtime: float
    renderer: Callable[..., Awaitable[str]]


@dataclass
class QueryRenderCacheEntry:
    """单服查询渲染结果缓存项。"""

    expires_at: float
    image_b64: str


@dataclass
class ToolStatusCacheEntry:
    """LLM Tool 查询状态缓存项。"""

    expires_at: float
    data: dict[str, Any]


@dataclass
class ToolListCacheEntry:
    """LLM Tool 列表缓存项。"""

    expires_at: float
    servers: list[dict[str, Any]]


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
        # 查询渲染缓存清理后台任务
        self._query_cache_cleanup_task: asyncio.Task | None = None
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
        self.auto_append_default_port = AUTO_APPEND_DEFAULT_PORT
        self.query_result_cache_ttl_seconds = QUERY_RESULT_CACHE_TTL_SECONDS
        self._query_render_cache: dict[str, QueryRenderCacheEntry] = {}
        self._tool_status_cache: dict[str, ToolStatusCacheEntry] = {}
        self._tool_list_cache: dict[str, ToolListCacheEntry] = {}
        self._tool_rate_limit_hits: dict[str, list[float]] = {}
        self._tool_query_semaphore = asyncio.Semaphore(TOOL_QUERY_CONCURRENCY)
        self._avatar_file_locks: dict[str, asyncio.Lock] = {}

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
        if (
            self._query_cache_cleanup_task is None
            or self._query_cache_cleanup_task.done()
        ):
            self._query_cache_cleanup_task = asyncio.create_task(
                self._query_render_cache_cleanup_loop()
            )
        logger.info("astrbot_plugin_get_mc_server_info initialized.")

    async def terminate(self) -> None:
        """插件销毁：优雅停止后台任务并释放网络资源。"""
        if self._silent_task:
            self._silent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._silent_task
            self._silent_task = None
        if self._query_cache_cleanup_task:
            self._query_cache_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._query_cache_cleanup_task
            self._query_cache_cleanup_task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._avatar_download_semaphore = None
        self._query_render_cache.clear()
        self._tool_status_cache.clear()
        self._tool_list_cache.clear()
        self._tool_rate_limit_hits.clear()
        self._avatar_file_locks.clear()
        logger.info("astrbot_plugin_get_mc_server_info terminated.")

    @filter.regex(r"^#(?:添加服务器|添加)\s+\S+\s+\S+\s*$")
    async def add_server(self, event: AstrMessageEvent):
        """添加 MC 服务器：#添加服务器 <服务器名称> <服务器地址> / #添加 <服务器名称> <服务器地址>"""
        if self._should_ignore_self_event(event):
            return

        # 1) 解析与格式校验
        matched = ADD_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
            return

        desired_name = matched.group(1).strip()
        raw_address = matched.group(2).strip()
        result = await self._add_server_data(
            event.unified_msg_origin,
            desired_name,
            raw_address,
        )
        if result.get("error") == "INVALID_ADDRESS":
            yield event.plain_result(
                "添加失败！服务器地址端口无效，请使用 host 或 host:数字端口，"
                "或在配置中开启 auto_append_default_port。"
            )
            return
        if result.get("error") == "CONNECTION_FAILED":
            yield event.plain_result("添加失败！服务器连接失败")
            return
        if result.get("error") == "SERVER_ALREADY_EXISTS":
            yield event.plain_result("添加失败！该服务器已存在")
            return
        if result.get("error") == "SAVE_FAILED":
            yield event.plain_result("添加失败！服务器保存失败，请稍后重试")
            return
        if not result.get("ok"):
            yield event.plain_result("添加失败！")
            return

        final_name = str(result.get("server", desired_name))
        if result.get("name_adjusted"):
            yield event.plain_result(
                f"名称重复，已自动调整为 [{final_name}]。\n"
                f"添加成功！服务器 [{final_name}] 已添加"
            )
            return
        yield event.plain_result(f"添加成功！服务器 [{final_name}] 已添加")

    @filter.regex(r"^#(?:查询服务器|查询)(?:\s+\S+)?\s*$")
    async def query_server(self, event: AstrMessageEvent):
        """查询 MC 服务器：#查询服务器 [服务器名称|服务器地址] / #查询 [服务器名称|服务器地址]"""
        if self._should_ignore_self_event(event):
            return

        # 无参数 => 查询当前会话全部服务器
        # 有参数 => 先按名称匹配当前会话已添加服务器，未命中则按地址直连查询
        matched = QUERY_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
            return

        query_arg = matched.group(1)
        if query_arg:
            query_token = query_arg.strip()
            session_key = event.unified_msg_origin
            async with self._store_lock:
                store = await self._load_store()
                session_obj = self._get_or_create_session(store, session_key)
                servers: dict[str, dict[str, Any]] = dict(
                    session_obj.get("servers", {})
                )

            matched_addresses = self._find_server_addresses_by_name(
                servers,
                query_token,
            )
            if len(matched_addresses) > 1:
                return_message = f"查询失败！检测到多个同名服务器 [{query_token}]，请使用服务器地址查询"
                yield event.plain_result(return_message)
                return
            if len(matched_addresses) == 1:
                yield await self._query_single_server(event, matched_addresses[0])
                return

            # 未命中已添加的服务器名称时，按地址直接进行一次主动查询：
            # - 不写入会话服务器列表
            # - 不拉取/缓存玩家头像
            # - 仅渲染并返回本次查询结果
            yield await self._query_direct_address(
                event,
                self._normalize_address(query_token),
            )
            return

        summary, failures = await self._query_all_servers(event)
        messages_to_send = failures + [summary]
        final_message = "\n".join(messages_to_send)
        yield event.plain_result(final_message)

    @filter.regex(r"^#模板(?:\s+\S+)?\s*$")
    async def switch_template(self, event: AstrMessageEvent):
        """模板切换命令。

        - `#模板`：列出 templates 目录下的全部模板名（不带 .py）。
        - `#模板 <模板名>`：切换当前会话模板。
        """
        if self._should_ignore_self_event(event):
            return

        matched = TEMPLATE_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
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
            await self._switch_template_data(event.unified_msg_origin, template_name)
            yield event.plain_result("模板缓存已重载")
            return

        result = await self._switch_template_data(
            event.unified_msg_origin, template_name
        )
        if result.get("error") == "TEMPLATE_NOT_FOUND":
            yield event.plain_result("切换失败！未找到模板！")
            return
        if not result.get("ok"):
            yield event.plain_result("切换失败！未找到模板！")
            return

        yield event.plain_result(f"已切换至 {template_name}")

    @filter.regex(r"^#(?:重命名服务器|重命名)\s+\S+\s+\S+\s*$")
    async def rename_server(self, event: AstrMessageEvent):
        """重命名当前会话中的服务器：#重命名服务器 <旧名称> <新名称> / #重命名 <旧名称> <新名称>"""
        if self._should_ignore_self_event(event):
            return

        matched = RENAME_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
            return

        old_name = matched.group(1).strip()
        new_name = matched.group(2).strip()
        result = await self._rename_server_data(
            event.unified_msg_origin,
            old_name,
            new_name,
        )
        if result.get("error") == "SERVER_NOT_FOUND":
            yield event.plain_result(
                f"重命名失败！当前会话内不存在名为 [{old_name}] 的服务器"
            )
            return
        if result.get("error") == "AMBIGUOUS_SERVER_NAME":
            yield event.plain_result(
                f"重命名失败！检测到多个同名服务器 [{old_name}]，请先处理重名后再重命名"
            )
            return
        if result.get("error") == "SAVE_FAILED":
            yield event.plain_result("重命名失败！服务器保存失败，请稍后重试")
            return
        if not result.get("ok"):
            yield event.plain_result(
                f"重命名失败！当前会话内不存在名为 [{old_name}] 的服务器"
            )
            return

        previous_name = str(result.get("old_name", old_name))
        final_name = str(result.get("new_name", new_name))
        if result.get("name_adjusted"):
            yield event.plain_result(
                f"名称重复，已自动调整为 [{final_name}]。\n"
                f"重命名成功！服务器 [{previous_name}] 已重命名为 [{final_name}]"
            )
            return
        yield event.plain_result(
            f"重命名成功！服务器 [{previous_name}] 已重命名为 [{final_name}]"
        )

    @filter.regex(r"^#(?:删除服务器|删除)\s+\S+\s*$")
    async def delete_server(self, event: AstrMessageEvent):
        """删除当前会话中的服务器：#删除服务器 <服务器名称> / #删除 <服务器名称>"""
        if self._should_ignore_self_event(event):
            return

        matched = DELETE_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
            return

        target_name = matched.group(1).strip()
        result = await self._delete_server_data(event.unified_msg_origin, target_name)
        if result.get("error") == "SERVER_NOT_FOUND":
            yield event.plain_result(
                f"删除失败！当前会话内不存在名为 [{target_name}] 的服务器"
            )
            return
        if result.get("error") == "SAVE_FAILED":
            yield event.plain_result("删除失败！服务器保存失败，请稍后重试")
            return
        if not result.get("ok"):
            yield event.plain_result(
                f"删除失败！当前会话内不存在名为 [{target_name}] 的服务器"
            )
            return

        yield event.plain_result(
            f"删除成功！已删除服务器 [{target_name}] 共 {result.get('removed_count', 0)} 个，并清理对应缓存"
        )

    @filter.regex(r"^#(?:服务器列表|列表)\s*$")
    async def list_servers(self, event: AstrMessageEvent):
        """列出当前会话内服务器：#服务器列表 / #列表"""
        if self._should_ignore_self_event(event):
            return

        matched = LIST_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
            return

        servers = await self._list_servers_data(event.unified_msg_origin)
        if not servers:
            yield event.plain_result("当前会话暂无已添加服务器")
            return

        lines: list[str] = []
        for index, server_obj in enumerate(servers, start=1):
            name = str(server_obj.get("name", "Unknown"))
            address = str(server_obj.get("address", "Unknown"))
            try:
                last_latency = int(server_obj.get("latency", 0) or 0)
            except Exception:
                last_latency = 0
            lines.append(
                f"{index}. {name} : {address} | 最近延迟 : {max(last_latency, 0)}ms"
            )

        yield event.plain_result("\n".join(lines))

    @filter.regex(
        r"^#(?:添加服务器|添加|查询服务器|查询|删除服务器|删除|重命名服务器|重命名|服务器列表|列表|模板|帮助|help)(?:\s+.*)?$"
    )
    async def command_help_and_format_guard(self, event: AstrMessageEvent):
        """命令帮助与格式兜底。"""
        if self._should_ignore_self_event(event):
            return

        message = event.message_str.strip()
        if not COMMAND_FALLBACK_PATTERN.match(message):
            return

        # 显式帮助命令
        if HELP_PATTERN.match(message):
            yield event.plain_result(self._build_help_message())
            return

        # 合法命令留给专用 handler 处理；仅兜底错误格式。
        valid_patterns = (
            ADD_SERVER_PATTERN,
            QUERY_SERVER_PATTERN,
            DELETE_SERVER_PATTERN,
            RENAME_SERVER_PATTERN,
            LIST_SERVER_PATTERN,
            TEMPLATE_PATTERN,
        )
        if any(pattern.match(message) for pattern in valid_patterns):
            return

        yield event.plain_result(self._build_help_message())

    @filter.llm_tool(name="query_mc_server")
    async def query_mc_server_tool(
        self, event: AstrMessageEvent, server: str
    ) -> dict[str, Any]:
        """查询 Minecraft Java 服务器状态。

        当用户询问服务器是否在线、延迟、版本、MOTD、在线人数或最大人数时调用。
        server 可以是当前会话已保存的服务器名称，也可以是服务器地址。

        Args:
            server(string): 服务器名称或地址，例如：生存服、Hypixel、play.example.com、play.example.com:25565。

        Examples:
            生存服
            Hypixel
            play.example.com

        不要用于 Minecraft 客户端安装、Java 环境、Mod、插件配置、
        游戏攻略、账号登录或非服务器状态查询问题。
        """
        session_key = event.unified_msg_origin
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            cache_key = self._build_tool_status_cache_key(session_key, server)
            cached = self._try_get_tool_status_cache(cache_key)
            if cached is not None:
                return self._with_tool_meta(cached | {"cached": True})
            async with self._tool_query_semaphore:
                result = await self._query_server_data(session_key, server)
            if result.get("ok"):
                self._set_tool_status_cache(cache_key, result)
            return self._with_tool_meta(result | {"cached": False})
        except Exception as exc:
            return self._tool_internal_error("query_mc_server", exc)

    @filter.llm_tool(name="add_mc_server")
    async def add_mc_server_tool(
        self, event: AstrMessageEvent, name: str, address: str
    ) -> dict[str, Any]:
        """添加 Minecraft Java 服务器到当前会话的服务器列表。

        当用户要求添加、保存、记住一个 Minecraft 服务器时调用。
        不同群聊和私聊使用不同会话，服务器列表互相隔离。

        Args:
            name(string): 要保存的服务器显示名称，例如：生存服、测试服。
            address(string): Minecraft Java 服务器地址，可带端口，例如：play.example.com、play.example.com:25565。

        Examples:
            name=生存服, address=play.example.com
            name=测试服, address=127.0.0.1:25565

        不要用于查询服务器状态、Minecraft 客户端安装、Java 环境、
        Mod、插件配置或游戏攻略问题。
        """
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            return self._with_tool_meta(
                await self._add_server_data(event.unified_msg_origin, name, address)
            )
        except Exception as exc:
            return self._tool_internal_error("add_mc_server", exc)

    @filter.llm_tool(name="delete_mc_server")
    async def delete_mc_server_tool(
        self, event: AstrMessageEvent, server: str
    ) -> dict[str, Any]:
        """删除当前会话中已保存的 Minecraft 服务器。

        当用户要求删除、移除、不再保存某个服务器时调用。
        server 可以是已保存的服务器名称，也可以是精确保存的地址。

        Args:
            server(string): 服务器名称或精确保存的服务器地址，例如：生存服、play.example.com。

        Examples:
            生存服
            play.example.com

        不要用于查询服务器状态、卸载 Minecraft 客户端、删除 Mod、
        删除 AstrBot 插件或游戏存档管理。
        """
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            return self._with_tool_meta(
                await self._delete_server_data(
                    event.unified_msg_origin, server, idempotent=True
                )
            )
        except Exception as exc:
            return self._tool_internal_error("delete_mc_server", exc)

    @filter.llm_tool(name="rename_mc_server")
    async def rename_mc_server_tool(
        self, event: AstrMessageEvent, old_name: str, new_name: str
    ) -> dict[str, Any]:
        """重命名当前会话中已保存的 Minecraft 服务器。

        当用户要求把某个服务器改名、重命名、换显示名称时调用。
        old_name 可以是已保存的服务器名称，也可以是精确保存的地址。

        Args:
            old_name(string): 当前服务器名称或精确保存的地址，例如：测试服。
            new_name(string): 新服务器显示名称，例如：生存服。

        Examples:
            old_name=测试服, new_name=生存服

        不要用于修改 Minecraft 用户名、服务器地址、Mod 名称、
        插件名称或非本插件保存的服务器名称。
        """
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            return self._with_tool_meta(
                await self._rename_server_data(
                    event.unified_msg_origin, old_name, new_name
                )
            )
        except Exception as exc:
            return self._tool_internal_error("rename_mc_server", exc)

    @filter.llm_tool(name="list_mc_servers")
    async def list_mc_servers_tool(self, event: AstrMessageEvent) -> dict[str, Any]:
        """列出当前会话中已保存的全部 Minecraft 服务器。

        当用户询问当前保存了哪些服务器、服务器列表、有哪些服时调用。

        Args:

        Examples:
            列出服务器
            有哪些服务器

        不要用于查询服务器是否在线、Minecraft 客户端安装、Java 环境、
        Mod、插件配置或游戏攻略问题。
        """
        session_key = event.unified_msg_origin
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            cached_servers = self._try_get_tool_list_cache(session_key)
            if cached_servers is not None:
                return self._with_tool_meta(
                    {
                        "ok": True,
                        "servers": cached_servers,
                        "total": len(cached_servers),
                        "cached": True,
                    }
                )
            servers = await self._list_servers_data(session_key)
            self._set_tool_list_cache(session_key, servers)
            return self._with_tool_meta(
                {
                    "ok": True,
                    "servers": servers,
                    "total": len(servers),
                    "cached": False,
                }
            )
        except Exception as exc:
            return self._tool_internal_error("list_mc_servers", exc)

    @filter.llm_tool(name="switch_mc_template")
    async def switch_mc_template_tool(
        self, event: AstrMessageEvent, template: str
    ) -> dict[str, Any]:
        """切换当前会话的 Minecraft 查询图片渲染模板。

        当用户要求把模板切换为某个模板，或更换查询图片样式时调用。

        Args:
            template(string): 模板名称，不包含 .py 后缀，例如：default_method。

        Examples:
            default_method
            reload

        不要用于切换 Minecraft 客户端版本、Java 版本、材质包、
        光影包、Mod 或服务器自身插件。
        """
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            return self._with_tool_meta(
                await self._switch_template_data(event.unified_msg_origin, template)
            )
        except Exception as exc:
            return self._tool_internal_error("switch_mc_template", exc)

    @filter.llm_tool(name="resolve_server_name")
    async def resolve_server_name_tool(
        self, event: AstrMessageEvent, hint: str = ""
    ) -> dict[str, Any]:
        """根据用户的模糊说法解析当前会话里可能指向的服务器。

        当用户说“刚才那个服”“昨天那个服”“第一个服”“生存相关的服”
        等模糊指代，而模型需要先查看当前会话保存的服务器候选时调用。
        返回结果按最近查询时间和创建时间排序，便于模型选择。

        Args:
            hint(string): 用户给出的模糊线索，可以为空，例如：昨天那个服、生存、第一个。

        Examples:
            昨天那个服
            生存
            第一个

        不要用于直接查询服务器在线状态；确认具体服务器后再调用
        query_mc_server。
        """
        session_key = event.unified_msg_origin
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            candidates = await self._resolve_server_name_data(session_key, hint)
            return self._with_tool_meta(
                {
                    "ok": True,
                    "hint": hint,
                    "candidates": candidates,
                    "total": len(candidates),
                }
            )
        except Exception as exc:
            return self._tool_internal_error("resolve_server_name", exc)

    def _check_tool_rate_limit(self, actor_key: str) -> dict[str, Any] | None:
        """Apply a small per-sender rate limit for LLM tool calls."""
        now = time.monotonic()
        window_start = now - TOOL_RATE_LIMIT_WINDOW_SECONDS
        hits = [
            hit
            for hit in self._tool_rate_limit_hits.get(actor_key, [])
            if hit >= window_start
        ]
        self._tool_rate_limit_hits[actor_key] = hits
        if len(hits) >= TOOL_RATE_LIMIT_MAX_CALLS:
            return self._with_tool_meta(
                {
                    "ok": False,
                    "error": "RATE_LIMITED",
                    "message": "tool call rate limit exceeded for this sender",
                }
            )
        hits.append(now)
        return None

    def _build_tool_actor_key(self, event: AstrMessageEvent) -> str:
        sender_id = self._get_event_sender_id(event)
        if not sender_id:
            sender_id = "unknown"
        return f"{event.unified_msg_origin}|{sender_id}"

    @staticmethod
    def _get_event_sender_id(event: AstrMessageEvent) -> str:
        try:
            sender_getter = getattr(event, "get_sender_id", None)
            if callable(sender_getter):
                return str(sender_getter() or "").strip()
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            return str(getattr(sender, "user_id", "") or "").strip()
        except Exception:
            return ""

    def _build_tool_status_cache_key(self, session_key: str, server: str) -> str:
        return f"{session_key}|{self._normalize_tool_server_token(server)}"

    def _normalize_tool_server_token(self, server: str) -> str:
        token = (server or "").strip().lower().rstrip(":")
        if not token:
            return token
        return self._normalize_address(token)

    def _try_get_tool_status_cache(self, cache_key: str) -> dict[str, Any] | None:
        now = time.time()
        entry = self._tool_status_cache.get(cache_key)
        if not entry:
            return None
        if entry.expires_at <= now:
            self._tool_status_cache.pop(cache_key, None)
            return None
        return dict(entry.data)

    def _set_tool_status_cache(self, cache_key: str, data: dict[str, Any]) -> None:
        self._tool_status_cache[cache_key] = ToolStatusCacheEntry(
            expires_at=time.time() + TOOL_STATUS_CACHE_TTL_SECONDS,
            data=dict(data),
        )

    def _try_get_tool_list_cache(self, session_key: str) -> list[dict[str, Any]] | None:
        now = time.time()
        entry = self._tool_list_cache.get(session_key)
        if not entry:
            return None
        if entry.expires_at <= now:
            self._tool_list_cache.pop(session_key, None)
            return None
        return [dict(server) for server in entry.servers]

    def _set_tool_list_cache(
        self, session_key: str, servers: list[dict[str, Any]]
    ) -> None:
        self._tool_list_cache[session_key] = ToolListCacheEntry(
            expires_at=time.time() + TOOL_LIST_CACHE_TTL_SECONDS,
            servers=[dict(server) for server in servers],
        )

    def _clear_tool_status_cache(self, session_key: str) -> None:
        prefix = f"{session_key}|"
        for key in list(self._tool_status_cache.keys()):
            if key.startswith(prefix):
                self._tool_status_cache.pop(key, None)

    def _clear_tool_list_cache(self, session_key: str) -> None:
        self._tool_list_cache.pop(session_key, None)

    def _cleanup_tool_caches(self) -> int:
        now = time.time()
        removed = 0
        for key, entry in list(self._tool_status_cache.items()):
            if entry.expires_at <= now:
                self._tool_status_cache.pop(key, None)
                removed += 1
        for key, entry in list(self._tool_list_cache.items()):
            if entry.expires_at <= now:
                self._tool_list_cache.pop(key, None)
                removed += 1
        return removed

    @staticmethod
    def _is_retryable_tool_error(error: str | None) -> bool:
        return error in {
            "CONNECTION_FAILED",
            "CONNECTION_TIMEOUT",
            "RATE_LIMITED",
            "SAVE_FAILED",
            "INTERNAL_ERROR",
        }

    def _with_tool_meta(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize LLM Tool responses while keeping existing adapter fields."""
        normalized = dict(data)
        ok = bool(normalized.get("ok", False))
        error = str(normalized.get("error", "") or "")
        normalized.setdefault("success", ok)
        if ok:
            if "online" in normalized:
                normalized.setdefault(
                    "status", "online" if normalized["online"] else "offline"
                )
            else:
                normalized.setdefault("status", "ok")
        else:
            normalized.setdefault("status", "error")
            normalized.setdefault("retryable", self._is_retryable_tool_error(error))
        normalized.setdefault("tool_version", TOOL_VERSION)
        normalized.setdefault("plugin_version", PLUGIN_VERSION)
        normalized.setdefault("request_id", uuid.uuid4().hex)
        return normalized

    def _tool_internal_error(self, tool_name: str, exc: Exception) -> dict[str, Any]:
        logger.exception("%s failed with internal error: %s", tool_name, exc)
        return self._with_tool_meta(
            {
                "ok": False,
                "error": "INTERNAL_ERROR",
                "message": "tool internal error",
            }
        )

    async def _query_server_data(self, session_key: str, server: str) -> dict[str, Any]:
        """Query server data for LLM tools without rendering images."""
        query_token = (server or "").strip()
        if not query_token:
            return {
                "ok": False,
                "online": False,
                "error": "INVALID_ARGUMENT",
                "message": "server is required",
            }

        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = dict(session_obj.get("servers", {}))

        matched_addresses = self._find_server_addresses_by_name(servers, query_token)
        if len(matched_addresses) > 1:
            return {
                "ok": False,
                "online": False,
                "server": query_token,
                "error": "AMBIGUOUS_SERVER_NAME",
                "message": "multiple saved servers use this name; query by address",
            }

        managed = len(matched_addresses) == 1
        address = (
            matched_addresses[0] if managed else self._normalize_address(query_token)
        )
        saved_server = servers.get(address, {})
        display_name = str(
            saved_server.get("name", query_token if managed else address)
        )

        try:
            status = await self._fetch_server_status(address, need_players=False)
        except McServerTimeoutError:
            return {
                "ok": False,
                "online": False,
                "server": display_name,
                "address": address,
                "managed": managed,
                "error": "CONNECTION_TIMEOUT",
                "message": "server connection timed out",
            }
        except McServerConnectionError:
            return {
                "ok": False,
                "online": False,
                "server": display_name,
                "address": address,
                "managed": managed,
                "error": "CONNECTION_FAILED",
                "message": "server connection failed",
            }

        now = int(time.time())
        await self._cache_server_icon(address, status.icon_base64)

        if managed:
            async with self._store_lock:
                store = await self._load_store()
                session_obj = self._get_or_create_session(store, session_key)
                real_server_obj = session_obj["servers"].get(address)
                if not real_server_obj:
                    return {
                        "ok": False,
                        "online": False,
                        "server": display_name,
                        "address": address,
                        "managed": True,
                        "error": "SERVER_NOT_FOUND",
                        "message": "saved server no longer exists",
                    }
                real_server_obj["last_latency"] = status.latency
                real_server_obj["last_active_query_at"] = now
                self._append_latency(real_server_obj, status.latency, now)
                await self._save_store(store)
            self._clear_tool_list_cache(session_key)

        await self._cleanup_expired_cache()
        return self._server_status_to_tool_data(
            status,
            server_name=display_name,
            address=address,
            managed=managed,
        )

    async def _add_server_data(
        self, session_key: str, name: str, raw_address: str
    ) -> dict[str, Any]:
        """Add a server and return structured data for LLM tools."""
        desired_name = (name or "").strip()
        raw_address = (raw_address or "").strip()
        if not desired_name or not raw_address:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "name and address are required",
            }
        if not self.auto_append_default_port and self._has_invalid_port_segment(
            raw_address
        ):
            return {
                "ok": False,
                "server": desired_name,
                "address": raw_address,
                "error": "INVALID_ADDRESS",
                "message": "server address port is invalid",
            }

        address = self._normalize_address(raw_address)
        try:
            status = await self._fetch_server_status(address, need_players=False)
        except McServerTimeoutError:
            return {
                "ok": False,
                "server": desired_name,
                "address": address,
                "error": "CONNECTION_TIMEOUT",
                "message": "server connection timed out",
            }
        except McServerConnectionError:
            return {
                "ok": False,
                "server": desired_name,
                "address": address,
                "error": "CONNECTION_FAILED",
                "message": "server connection failed",
            }

        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = session_obj["servers"]
            if address in servers:
                return {
                    "ok": False,
                    "server": str(servers[address].get("name", desired_name)),
                    "address": address,
                    "error": "SERVER_ALREADY_EXISTS",
                    "message": "server already exists",
                }

            final_name, name_duplicated = self._resolve_unique_server_name(
                desired_name,
                servers,
            )
            now = int(time.time())
            servers[address] = {
                "name": final_name,
                "address": address,
                "latency_history": [],
                "last_latency": status.latency,
                "last_silent_query_at": 0,
                "last_active_query_at": 0,
                "created_at": now,
            }
            self._append_latency(servers[address], status.latency, now)
            try:
                await self._save_store(store)
            except Exception as exc:
                logger.exception("save server failed: %s", exc)
                return {
                    "ok": False,
                    "server": final_name,
                    "address": address,
                    "error": "SAVE_FAILED",
                    "message": "server save failed",
                }

        self._clear_query_render_cache(session_key, address)
        self._clear_tool_status_cache(session_key)
        self._clear_tool_list_cache(session_key)
        await self._cache_server_icon(address, status.icon_base64)
        await self._cleanup_expired_cache()
        return {
            "ok": True,
            "server": final_name,
            "requested_name": desired_name,
            "address": address,
            "name_adjusted": name_duplicated,
            "latency": status.latency,
            "players_online": status.players_online,
            "players_max": status.players_max,
            "version": status.version,
            "motd": status.motd,
        }

    async def _delete_server_data(
        self, session_key: str, server: str, *, idempotent: bool = False
    ) -> dict[str, Any]:
        """Delete saved servers and return structured data for LLM tools."""
        target = (server or "").strip()
        if not target:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "server is required",
            }

        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = session_obj.get("servers", {})
            addresses = self._find_server_addresses_by_name(servers, target)
            if not addresses and target in servers:
                addresses = [target]
            if not addresses:
                if idempotent:
                    return {
                        "ok": True,
                        "server": target,
                        "removed_count": 0,
                        "removed": [],
                        "already_deleted": True,
                        "message": "saved server already absent",
                    }
                return {
                    "ok": False,
                    "server": target,
                    "error": "SERVER_NOT_FOUND",
                    "message": "saved server not found in current session",
                }

            removed: list[dict[str, Any]] = []
            for address in addresses:
                server_obj = servers.pop(address, None)
                if server_obj:
                    removed.append(
                        {
                            "name": str(server_obj.get("name", target)),
                            "address": address,
                        }
                    )
            try:
                await self._save_store(store)
            except Exception as exc:
                logger.exception("delete server save failed: %s", exc)
                return {
                    "ok": False,
                    "server": target,
                    "error": "SAVE_FAILED",
                    "message": "server save failed",
                }

        for address in addresses:
            self._clear_query_render_cache(session_key, address)
            self._delete_server_cache(address)
        self._clear_tool_status_cache(session_key)
        self._clear_tool_list_cache(session_key)

        return {
            "ok": True,
            "server": target,
            "removed_count": len(removed),
            "removed": removed,
        }

    async def _rename_server_data(
        self, session_key: str, old_name: str, new_name: str
    ) -> dict[str, Any]:
        """Rename a saved server and return structured data for LLM tools."""
        old_name = (old_name or "").strip()
        new_name = (new_name or "").strip()
        if not old_name or not new_name:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "old_name and new_name are required",
            }

        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = session_obj.get("servers", {})
            addresses = self._find_server_addresses_by_name(servers, old_name)
            if not addresses and old_name in servers:
                addresses = [old_name]
            if not addresses:
                return {
                    "ok": False,
                    "server": old_name,
                    "error": "SERVER_NOT_FOUND",
                    "message": "saved server not found in current session",
                }
            if len(addresses) > 1:
                return {
                    "ok": False,
                    "server": old_name,
                    "error": "AMBIGUOUS_SERVER_NAME",
                    "message": "multiple saved servers use this name",
                }

            address = addresses[0]
            server_obj = servers.get(address)
            if not server_obj:
                return {
                    "ok": False,
                    "server": old_name,
                    "address": address,
                    "error": "SERVER_NOT_FOUND",
                    "message": "saved server not found in current session",
                }

            previous_name = str(server_obj.get("name", "")).strip() or old_name
            final_name, name_duplicated = self._resolve_unique_server_name(
                new_name,
                servers,
                exclude_address=address,
            )
            server_obj["name"] = final_name
            try:
                await self._save_store(store)
            except Exception as exc:
                logger.exception("rename server save failed: %s", exc)
                return {
                    "ok": False,
                    "server": old_name,
                    "address": address,
                    "error": "SAVE_FAILED",
                    "message": "server save failed",
                }

        self._clear_tool_status_cache(session_key)
        self._clear_tool_list_cache(session_key)
        return {
            "ok": True,
            "address": address,
            "old_name": previous_name,
            "new_name": final_name,
            "requested_new_name": new_name,
            "name_adjusted": name_duplicated,
        }

    async def _list_servers_data(self, session_key: str) -> list[dict[str, Any]]:
        """List saved servers as JSON-serializable data for LLM tools."""
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = dict(session_obj.get("servers", {}))

        results: list[dict[str, Any]] = []
        for server_obj in servers.values():
            try:
                last_latency = int(server_obj.get("last_latency", 0) or 0)
            except Exception:
                last_latency = 0
            results.append(
                {
                    "name": str(server_obj.get("name", "Unknown")),
                    "address": str(server_obj.get("address", "Unknown")),
                    "latency": max(last_latency, 0),
                }
            )
        return results

    async def _resolve_server_name_data(
        self, session_key: str, hint: str
    ) -> list[dict[str, Any]]:
        """Return saved server candidates ordered for fuzzy reference resolution."""
        hint_text = (hint or "").strip().lower()
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = dict(session_obj.get("servers", {}))

        candidates: list[dict[str, Any]] = []
        for server_obj in servers.values():
            name = str(server_obj.get("name", "Unknown"))
            address = str(server_obj.get("address", "Unknown"))
            try:
                last_latency = int(server_obj.get("last_latency", 0) or 0)
            except Exception:
                last_latency = 0
            try:
                last_active_query_at = int(
                    server_obj.get("last_active_query_at", 0) or 0
                )
            except Exception:
                last_active_query_at = 0
            try:
                last_silent_query_at = int(
                    server_obj.get("last_silent_query_at", 0) or 0
                )
            except Exception:
                last_silent_query_at = 0
            try:
                created_at = int(server_obj.get("created_at", 0) or 0)
            except Exception:
                created_at = 0

            haystack = f"{name} {address}".lower()
            score = 0
            if hint_text and hint_text in haystack:
                score += 100
            if hint_text and name.lower().startswith(hint_text):
                score += 50
            recent_ts = max(last_active_query_at, last_silent_query_at, created_at)
            candidates.append(
                {
                    "name": name,
                    "address": address,
                    "latency": max(last_latency, 0),
                    "last_active_query_at": last_active_query_at,
                    "last_silent_query_at": last_silent_query_at,
                    "created_at": created_at,
                    "score": score,
                    "matched": bool(score),
                    "recent_ts": recent_ts,
                }
            )

        candidates.sort(
            key=lambda item: (
                int(item.get("score", 0) or 0),
                int(item.get("recent_ts", 0) or 0),
            ),
            reverse=True,
        )
        return candidates[:10]

    async def _switch_template_data(
        self, session_key: str, template: str
    ) -> dict[str, Any]:
        """Switch template and return structured data for LLM tools."""
        template_name = (template or "").strip()
        if not template_name:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "template is required",
                "available_templates": self._list_templates(),
            }
        if template_name == "reload":
            self._template_renderer_cache.clear()
            return {
                "ok": True,
                "template": template_name,
                "reloaded": True,
                "available_templates": self._list_templates(),
            }
        if not self._is_valid_template_name(template_name):
            return {
                "ok": False,
                "template": template_name,
                "error": "TEMPLATE_NOT_FOUND",
                "message": "template not found",
                "available_templates": self._list_templates(),
            }

        try:
            await self._get_template_renderer(template_name)
        except Exception as exc:
            logger.warning("template load failed: %s", exc)
            return {
                "ok": False,
                "template": template_name,
                "error": "TEMPLATE_NOT_FOUND",
                "message": "template not found",
                "available_templates": self._list_templates(),
            }

        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            current_template = str(
                session_obj.get("template", DEFAULT_TEMPLATE_NAME)
                or DEFAULT_TEMPLATE_NAME
            )
            if current_template == template_name:
                return {
                    "ok": True,
                    "template": template_name,
                    "already_active": True,
                    "message": "template already active",
                }
            session_obj["template"] = template_name
            try:
                await self._save_store(store)
            except Exception as exc:
                logger.exception("switch template save failed: %s", exc)
                return {
                    "ok": False,
                    "template": template_name,
                    "error": "SAVE_FAILED",
                    "message": "template save failed",
                }

        return {"ok": True, "template": template_name, "already_active": False}

    @staticmethod
    def _server_status_to_tool_data(
        status: ServerStatus,
        *,
        server_name: str,
        address: str,
        managed: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "server": server_name,
            "address": address,
            "managed": managed,
            "online": True,
            "latency": status.latency,
            "players_online": status.players_online,
            "players_max": status.players_max,
            "version": status.version,
            "motd": status.motd,
        }

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

        cache_key = self._build_query_cache_key(
            session_key=session_key,
            address=address,
            template_name=template_name,
            mode="managed",
        )
        cached_image = self._try_get_query_render_cache(cache_key)
        if cached_image is not None:
            return (
                event.make_result()
                .message(f"缓存结果（{self.query_result_cache_ttl_seconds}秒内）")
                .base64_image(cached_image)
            )

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
        render_history = self._build_render_history(history, now_ts=now)
        renderer = await self._get_template_renderer(template_name)
        image_b64 = await self._call_template_renderer(
            renderer,
            server_name=server_obj["name"],
            server_address=address,
            latency=status.latency,
            players_online=status.players_online,
            players_max=status.players_max,
            server_version=status.version,
            motd=status.motd,
            history=render_history,
            history_title=self._build_history_title(),
            icon_path=str(icon_path) if icon_path.exists() else None,
            players=players_for_render,
        )
        self._set_query_render_cache(cache_key, image_b64)
        return event.make_result().base64_image(image_b64)

    async def _query_direct_address(self, event: AstrMessageEvent, address: str):
        """对指定地址执行一次临时主动查询，不写入会话存储。"""
        session_key = event.unified_msg_origin
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            template_name = str(
                session_obj.get("template", DEFAULT_TEMPLATE_NAME)
                or DEFAULT_TEMPLATE_NAME
            )

        cache_key = self._build_query_cache_key(
            session_key=session_key,
            address=address,
            template_name=template_name,
            mode="direct",
        )
        cached_image = self._try_get_query_render_cache(cache_key)
        if cached_image is not None:
            return (
                event.make_result()
                .message(f"缓存结果（{self.query_result_cache_ttl_seconds}秒内）")
                .base64_image(cached_image)
            )

        try:
            # 地址直连查询不拉取玩家 sample，避免触发头像下载链路。
            status = await self._fetch_server_status(address, need_players=False)
        except Exception:
            return event.plain_result(f"服务器 [{address}] 查询失败！")

        now = int(time.time())
        await self._cache_server_icon(address, status.icon_base64)
        await self._cleanup_expired_cache()

        icon_path = self._icon_cache_path(address)
        renderer = await self._get_template_renderer(template_name)
        image_b64 = await self._call_template_renderer(
            renderer,
            server_name=address,
            server_address=address,
            latency=status.latency,
            players_online=status.players_online,
            players_max=status.players_max,
            server_version=status.version,
            motd=status.motd,
            history=self._build_render_history([], now_ts=now),
            history_title=self._build_history_title(),
            icon_path=str(icon_path) if icon_path.exists() else None,
            players=[],
        )
        self._set_query_render_cache(cache_key, image_b64)
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
        try:
            server = await JavaServer.async_lookup(address)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise McServerTimeoutError("server lookup timed out") from exc
        except OSError as exc:
            raise McServerConnectionError("server lookup failed") from exc
        except Exception as exc:
            raise McServerConnectionError("server lookup failed") from exc

        try:
            status = await asyncio.wait_for(
                server.async_status(), timeout=self.status_timeout_seconds
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise McServerTimeoutError("server status timed out") from exc
        except (OSError, ConnectionError) as exc:
            raise McServerConnectionError("server status failed") from exc
        except Exception as exc:
            raise McServerConnectionError("server status failed") from exc

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
        motd = self._extract_motd_text(getattr(status, "description", None))
        return ServerStatus(
            address=address,
            latency=max(latency, 0),
            version=version,
            players_online=int(getattr(status.players, "online", 0) or 0),
            players_max=int(getattr(status.players, "max", 0) or 0),
            icon_base64=icon_base64,
            players=players,
            motd=motd,
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
            file_lock = self._avatar_file_locks.setdefault(
                str(avatar_path), asyncio.Lock()
            )
            async with file_lock:
                if (
                    avatar_path.exists()
                    and now - int(avatar_path.stat().st_mtime) <= self.cache_ttl_seconds
                ):
                    return {"name": name, "avatar_path": str(avatar_path)}
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
        self.query_result_cache_ttl_seconds = self._get_config_int(
            "query_result_cache_ttl_seconds",
            QUERY_RESULT_CACHE_TTL_SECONDS,
            min_value=1,
        )
        self.skin_api_url_template = self._normalize_skin_api_url_template(
            self._get_config_str("skin_api_url_template", SKIN_API_URL_TEMPLATE)
        )
        self.auto_append_default_port = self._get_config_bool(
            "auto_append_default_port",
            AUTO_APPEND_DEFAULT_PORT,
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

    def _get_config_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置并兼容常见字符串/数字表示。"""
        raw = None
        if hasattr(self._plugin_config, "get"):
            raw = self._plugin_config.get(key, default)
        elif isinstance(self._plugin_config, dict):
            raw = self._plugin_config.get(key, default)

        if isinstance(raw, bool):
            return raw
        if raw is None:
            return default
        if isinstance(raw, (int, float)):
            return bool(raw)

        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return default

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

    def _build_render_history(
        self,
        history_points: list[dict[str, Any]],
        *,
        now_ts: int | None = None,
    ) -> list[dict[str, int]]:
        """构建用于渲染的固定长度历史序列，缺失点补零。"""
        limit = max(int(self.history_limit), 1)
        interval = max(int(self.silent_query_interval_seconds), 1)
        end_ts = int(now_ts if now_ts is not None else time.time())
        start_ts = end_ts - (limit - 1) * interval

        series = [
            {"timestamp": start_ts + index * interval, "latency": 0}
            for index in range(limit)
        ]
        latest_by_slot: dict[int, tuple[int, int]] = {}

        for point in history_points:
            try:
                ts = int(point.get("timestamp", 0) or 0)
                latency = int(point.get("latency", 0) or 0)
            except Exception:
                continue

            if ts < start_ts or ts > end_ts + interval:
                continue

            # 用“最接近槽位”策略映射时间点，避免采样抖动导致的错槽。
            slot = int((ts - start_ts + interval // 2) // interval)
            slot = max(0, min(slot, limit - 1))
            previous = latest_by_slot.get(slot)
            if previous is None or ts >= previous[0]:
                latest_by_slot[slot] = (ts, max(latency, 0))

        for slot, (ts, latency) in latest_by_slot.items():
            series[slot]["timestamp"] = ts
            series[slot]["latency"] = latency

        return series

    @staticmethod
    def _find_server_addresses_by_name(
        servers: dict[str, dict[str, Any]],
        query_name: str,
    ) -> list[str]:
        """按显示名称匹配会话内已添加服务器，返回命中的地址列表。"""
        target = query_name.strip()
        if not target:
            return []

        addresses: list[str] = []
        for address, server_obj in servers.items():
            server_name = str(server_obj.get("name", "")).strip()
            if server_name == target:
                addresses.append(address)
        return addresses

    @staticmethod
    def _resolve_unique_server_name(
        desired_name: str,
        servers: dict[str, dict[str, Any]],
        *,
        exclude_address: str | None = None,
    ) -> tuple[str, bool]:
        """会话内服务器名称去重，必要时自动追加序号后缀。"""
        base = desired_name.strip()
        if not base:
            base = "未命名服务器"

        existing_names: set[str] = set()
        for address, server_obj in servers.items():
            if exclude_address and address == exclude_address:
                continue
            existing_name = str(server_obj.get("name", "")).strip()
            if existing_name:
                existing_names.add(existing_name)

        if base not in existing_names:
            return base, False

        index = 1
        while True:
            candidate = f"{base}({index})"
            if candidate not in existing_names:
                return candidate, True
            index += 1

    def _normalize_address(self, address: str) -> str:
        """标准化服务器地址。

        - 当 `auto_append_default_port` 为 True 时：
          - 缺省端口补 25565；
          - 端口非数字时回退为默认端口。
        - 当 `auto_append_default_port` 为 False 时，保持原样。
        """
        address = address.strip()
        if not address:
            return address
        if not self.auto_append_default_port:
            return address
        if ":" not in address:
            return f"{address}:{DEFAULT_PORT}"
        host, port_str = address.rsplit(":", 1)
        if not port_str.isdigit():
            return f"{host}:{DEFAULT_PORT}"
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

    def _delete_server_cache(self, address: str) -> None:
        """删除指定服务器的全部缓存目录。"""
        cache_dir = self._server_cache_dir(address)
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

    @staticmethod
    def _should_ignore_self_event(event: AstrMessageEvent) -> bool:
        """若消息来自机器人自身则忽略。"""
        sender_id = ""
        self_id = ""
        try:
            sender_getter = getattr(event, "get_sender_id", None)
            if callable(sender_getter):
                sender_id = str(sender_getter() or "").strip()
            else:
                sender = getattr(getattr(event, "message_obj", None), "sender", None)
                sender_id = str(getattr(sender, "user_id", "") or "").strip()
        except Exception:
            sender_id = ""

        try:
            self_getter = getattr(event, "get_self_id", None)
            if callable(self_getter):
                self_id = str(self_getter() or "").strip()
            else:
                self_id = str(
                    getattr(getattr(event, "message_obj", None), "self_id", "") or ""
                ).strip()
        except Exception:
            self_id = ""

        return bool(sender_id and self_id and sender_id == self_id)

    @staticmethod
    def _build_help_message() -> str:
        """构建命令帮助文案。"""
        return (
            "命令用法：\n"
            "#添加服务器 <服务器名称> <服务器地址>\n"
            "#添加 <服务器名称> <服务器地址>\n"
            "#查询服务器 [服务器名称|服务器地址]\n"
            "#查询 [服务器名称|服务器地址]\n"
            "#删除服务器 <服务器名称>\n"
            "#删除 <服务器名称>\n"
            "#重命名服务器 <旧名称> <新名称>\n"
            "#重命名 <旧名称> <新名称>\n"
            "#服务器列表\n"
            "#列表\n"
            "#模板 [模板名|reload]\n"
            "#帮助 / #help"
        )

    @staticmethod
    def _has_invalid_port_segment(address: str) -> bool:
        """校验简单 host:port 形式下的端口是否合法。"""
        raw = (address or "").strip()
        if raw.count(":") != 1:
            return False
        host, port_str = raw.rsplit(":", 1)
        if not host:
            return True
        return not port_str.isdigit()

    def _build_history_title(self) -> str:
        """构建历史图标题文本（随配置动态变化）。"""
        points = max(int(self.history_limit), 1)
        interval = max(int(self.silent_query_interval_seconds), 1)
        total_seconds = points * interval
        if total_seconds % 3600 == 0:
            total_window = f"{total_seconds // 3600}h"
        else:
            total_window = f"{total_seconds // 60}m"
        return f"历史延迟（{total_window} / {points}点）"

    async def _call_template_renderer(
        self,
        renderer: Callable[..., Awaitable[str]],
        **kwargs: Any,
    ) -> str:
        """按模板函数签名过滤参数，兼容旧模板。"""
        try:
            sig = inspect.signature(renderer)
        except Exception:
            return await renderer(**kwargs)

        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_kwargs:
            return await renderer(**kwargs)

        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return await renderer(**filtered)

    @staticmethod
    def _build_query_cache_key(
        *,
        session_key: str,
        address: str,
        template_name: str,
        mode: str,
    ) -> str:
        return f"{session_key}|{address}|{template_name}|{mode}"

    def _try_get_query_render_cache(self, cache_key: str) -> str | None:
        now = time.time()
        entry = self._query_render_cache.get(cache_key)
        if not entry:
            return None
        if entry.expires_at <= now:
            self._query_render_cache.pop(cache_key, None)
            return None
        return entry.image_b64

    def _set_query_render_cache(self, cache_key: str, image_b64: str) -> None:
        self._query_render_cache[cache_key] = QueryRenderCacheEntry(
            expires_at=time.time() + float(self.query_result_cache_ttl_seconds),
            image_b64=image_b64,
        )

    def _clear_query_render_cache(self, session_key: str, address: str) -> None:
        prefix = f"{session_key}|{address}|"
        for key in list(self._query_render_cache.keys()):
            if key.startswith(prefix):
                self._query_render_cache.pop(key, None)

    def _cleanup_query_render_cache(self) -> int:
        """删除已过期的查询渲染缓存项。"""
        now = time.time()
        removed = 0
        for key, entry in list(self._query_render_cache.items()):
            if entry.expires_at <= now:
                self._query_render_cache.pop(key, None)
                removed += 1
        return removed

    async def _query_render_cache_cleanup_loop(self) -> None:
        """定期清理查询渲染缓存和 LLM Tool 缓存中的过期条目。"""
        while True:
            try:
                removed = self._cleanup_query_render_cache()
                removed += self._cleanup_tool_caches()
                if removed:
                    logger.debug("cleaned %s expired cache entries", removed)
            except Exception as exc:
                logger.debug("cache cleanup failed: %s", exc)
            await asyncio.sleep(QUERY_CACHE_CLEANUP_INTERVAL_SECONDS)

    def _extract_motd_text(self, description: Any) -> str:
        """提取并归一化服务端 Motd。"""
        if description is None:
            return ""
        try:
            to_plain = getattr(description, "to_plain", None)
            if callable(to_plain):
                text = self._strip_minecraft_format_codes(str(to_plain() or "")).strip()
                if text:
                    return text
        except Exception:
            pass

        text = self._strip_minecraft_format_codes(
            self._flatten_motd_node(description)
        ).strip()
        return text[:300]

    @staticmethod
    def _strip_minecraft_format_codes(text: str) -> str:
        """移除 Minecraft Motd 文本中的格式控制码（§x）。"""
        if not text:
            return ""
        cleaned = MOTD_FORMAT_CODE_PATTERN.sub("", text)
        # 处理末尾孤立的 §
        return cleaned.replace("§", "")

    def _flatten_motd_node(self, node: Any) -> str:
        if node is None:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            parts: list[str] = []
            if "text" in node:
                parts.append(self._flatten_motd_node(node.get("text")))
            if "extra" in node:
                parts.append(self._flatten_motd_node(node.get("extra")))
            if "translate" in node and not parts:
                parts.append(self._flatten_motd_node(node.get("translate")))
            return "".join(parts)
        if isinstance(node, (list, tuple)):
            return "".join(self._flatten_motd_node(item) for item in node)
        return str(node)

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
                # 头像使用最近邻放大，保留像素边缘清晰度。
                avatar = avatar.resize((SKIN_SIZE, SKIN_SIZE), Image.Resampling.NEAREST)
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
