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
import contextlib
import re
import shutil
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from . import avatar as _avatar_mod
from . import cache as _cache_mod
from . import query as _query_mod

# ---- 模块化拆分导入 ----
from . import store as _store_mod
from . import template_loader as _tl_mod

ServerStatus = _query_mod.ServerStatus
McServerConnectionError = _query_mod.McServerConnectionError
McServerTimeoutError = _query_mod.McServerTimeoutError
McServerInvalidAddressError = _query_mod.McServerInvalidAddressError

ADD_SERVER_PATTERN = re.compile(r"^/(?:添加服务器|添加)\s+(\S+)\s+(\S+)\s*$")
BACKUP_SERVER_PATTERN = re.compile(
    r"^/(?:备用线路|备用|bak)\s+(\S+)\s+(\S+)\s*$",
    re.IGNORECASE,
)
QUERY_SERVER_PATTERN = re.compile(r"^/(?:查询服务器|查询)(?:\s+(\S+))?\s*$")
DELETE_SERVER_PATTERN = re.compile(r"^/(?:删除服务器|删除)\s+(\S+)\s*$")
CLEAR_DATA_PATTERN = re.compile(r"^/数据清除\s*$")
RENAME_SERVER_PATTERN = re.compile(r"^/(?:重命名服务器|重命名)\s+(\S+)\s+(\S+)\s*$")
LIST_SERVER_PATTERN = re.compile(r"^/(?:服务器列表|列表)\s*$")
TEMPLATE_PATTERN = re.compile(r"^/模板(?:\s+(\S+))?\s*$")
TEMPLATE_RELOAD_PATTERN = re.compile(r"^/模板重载\s*$")
REDIRECT_SERVER_PATTERN = re.compile(r"^/重定向\s+(\S+)\s+(\S+)\s*$")
HELP_PATTERN = re.compile(r"^/(?:帮助|help)\s*$")
COMMAND_FALLBACK_PATTERN = re.compile(
    r"^/(?:添加服务器|添加|备用线路|备用|bak|查询服务器|查询|删除服务器|删除|数据清除|重命名服务器|重命名|重定向|服务器列表|列表|模板重载|模板|帮助|help)(?:\s+.*)?$",
    re.IGNORECASE,
)

# 是否自动补全默认端口（可被插件配置覆盖）
AUTO_APPEND_DEFAULT_PORT = False
# 群聊中的写操作和未保存地址直连默认仅允许管理员
MUTATION_REQUIRES_ADMIN = True
DIRECT_QUERY_REQUIRES_ADMIN = True
# 静默轮询间隔：30 分钟
SILENT_QUERY_INTERVAL_SECONDS = 30 * 60
# 仅保留最近 48 个延迟点（刚好对应 24 小时，30 分钟/点）
HISTORY_LIMIT = 48
# 图片缓存有效期：24 小时
CACHE_TTL_SECONDS = 24 * 60 * 60
# 向 MC 服务端拉取状态的超时
STATUS_TIMEOUT = 10
# 默认渲染模板（对应 templates/default_method.py）
DEFAULT_TEMPLATE_NAME = "default_method"
# 离线服务器没有实时 Motd 时使用的默认文本
DEFAULT_OFFLINE_MOTD = "邦邦咔邦"
# 全服主动查询并发上限
QUERY_ALL_CONCURRENCY = 5
# 单会话最多保存服务器数
MAX_SERVERS_PER_SESSION = 50
# 头像下载并发上限
AVATAR_DOWNLOAD_CONCURRENCY = 5
# 头像下载重试次数（总尝试次数 = 1 + retries）
AVATAR_DOWNLOAD_RETRIES = 2
# 头像批处理与图片渲染的端到端超时
AVATAR_BATCH_TIMEOUT_SECONDS = 30
RENDER_TIMEOUT_SECONDS = 30
# 皮肤接口（按 UUID 获取玩家皮肤）
SKIN_API_URL_TEMPLATE = "https://skin.mualliance.ltd/api/union/skin/byuuid/{uuid}"
# 单服查询结果渲染缓存时长（秒）
QUERY_RESULT_CACHE_TTL_SECONDS = 10
# 查询渲染缓存清理任务间隔（秒）
QUERY_CACHE_CLEANUP_INTERVAL_SECONDS = 5 * 60
# LLM Tool 返回结构版本
TOOL_VERSION = "1.2"
PLUGIN_VERSION = "v1.9.3"
# Tool 查询状态缓存，避免 Agent 连续追问时重复打到 MC 服务端
TOOL_STATUS_CACHE_TTL_SECONDS = 30
# Tool 列表缓存，避免 Agent 连续追问列表细节时重复读取存储
TOOL_LIST_CACHE_TTL_SECONDS = 5
# Tool 层限流：同一会话窗口内最多允许的工具调用次数
TOOL_RATE_LIMIT_WINDOW_SECONDS = 60
TOOL_RATE_LIMIT_MAX_CALLS = 20
# Tool 层并发查询上限
TOOL_QUERY_CONCURRENCY = 3


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


@dataclass
class SavedServerQueryResult:
    """Result of querying one logical server through its ordered lines."""

    status: ServerStatus | None
    address: str
    line_type: str
    attempted_addresses: list[str]
    error: str | None = None


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
        self._template_renderer_cache: dict[
            str, tuple[float, _tl_mod.TemplateRenderer]
        ] = {}
        # 运行时配置（支持插件配置覆盖）
        self.silent_query_interval_seconds = SILENT_QUERY_INTERVAL_SECONDS
        self.history_limit = HISTORY_LIMIT
        self.cache_ttl_seconds = CACHE_TTL_SECONDS
        self.status_timeout_seconds = STATUS_TIMEOUT
        self.query_all_concurrency = QUERY_ALL_CONCURRENCY
        self.max_servers_per_session = MAX_SERVERS_PER_SESSION
        self.avatar_download_concurrency = AVATAR_DOWNLOAD_CONCURRENCY
        self.avatar_download_retries = AVATAR_DOWNLOAD_RETRIES
        self.avatar_batch_timeout_seconds = AVATAR_BATCH_TIMEOUT_SECONDS
        self.render_timeout_seconds = RENDER_TIMEOUT_SECONDS
        self.skin_api_url_template = SKIN_API_URL_TEMPLATE
        self.auto_append_default_port = AUTO_APPEND_DEFAULT_PORT
        self.mutation_requires_admin = MUTATION_REQUIRES_ADMIN
        self.direct_query_requires_admin = DIRECT_QUERY_REQUIRES_ADMIN
        self.query_result_cache_ttl_seconds = QUERY_RESULT_CACHE_TTL_SECONDS
        self._query_render_cache: dict[
            tuple[str, str, str, str], QueryRenderCacheEntry
        ] = {}
        self._tool_status_cache: dict[str, ToolStatusCacheEntry] = {}
        self._tool_list_cache: dict[str, ToolListCacheEntry] = {}
        self._tool_rate_limit_hits: dict[str, list[float]] = {}
        self._command_rate_limit_hits: dict[str, list[float]] = {}
        self._tool_query_semaphore = asyncio.Semaphore(TOOL_QUERY_CONCURRENCY)
        self._avatar_file_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

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
        self._command_rate_limit_hits.clear()
        self._avatar_file_locks.clear()
        logger.info("astrbot_plugin_get_mc_server_info terminated.")

    @filter.regex(r"^/(?:添加服务器|添加)\s+\S+\s+\S+\s*$")
    async def add_server(self, event: AstrMessageEvent):
        """添加 MC 服务器：/添加服务器 <服务器名称> <服务器地址>；或 /添加 <服务器名称> <服务器地址>"""
        if self._should_ignore_self_event(event):
            return
        if self._is_mutation_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
            return
        if self._check_command_rate_limit(event):
            yield event.plain_result("请求过于频繁，请稍后再试")
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
                "添加失败！服务器地址无效或指向内网/保留网络，请使用公网 host 或 host:数字端口"
            )
            return
        if result.get("error") == "CONNECTION_FAILED":
            yield event.plain_result("添加失败！服务器连接失败")
            return
        if result.get("error") == "SERVER_ALREADY_EXISTS":
            yield event.plain_result("添加失败！该服务器已存在")
            return
        if result.get("error") == "SERVER_LIMIT_REACHED":
            yield event.plain_result(
                f"添加失败！当前会话最多保存 {result.get('limit', self.max_servers_per_session)} 个服务器"
            )
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

    @filter.regex(r"(?i)^/(?:备用线路|备用|bak)\s+\S+\s+\S+\s*$")
    async def add_backup_server(self, event: AstrMessageEvent):
        """为已保存服务器添加备用线路。"""
        if self._should_ignore_self_event(event):
            return
        if self._is_group_admin_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
            return
        if self._check_command_rate_limit(event):
            yield event.plain_result("请求过于频繁，请稍后再试")
            return

        matched = BACKUP_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
            return

        server_name = matched.group(1).strip()
        raw_address = matched.group(2).strip()
        result = await self._add_backup_server_data(
            event.unified_msg_origin,
            server_name,
            raw_address,
        )
        error = result.get("error")
        if error == "SERVER_NOT_FOUND":
            yield event.plain_result(
                f"备用线路添加失败！当前会话内不存在名为 [{server_name}] 的服务器"
            )
            return
        if error == "AMBIGUOUS_SERVER_NAME":
            yield event.plain_result(
                f"备用线路添加失败！检测到多个同名服务器 [{server_name}]"
            )
            return
        if error == "INVALID_ADDRESS":
            yield event.plain_result(
                "备用线路添加失败！地址无效或指向内网/保留网络"
            )
            return
        if error == "CONNECTION_TIMEOUT":
            yield event.plain_result("备用线路添加失败！服务器连接超时")
            return
        if error == "CONNECTION_FAILED":
            yield event.plain_result("备用线路添加失败！服务器连接失败")
            return
        if error == "SERVER_ADDRESS_ALREADY_EXISTS":
            yield event.plain_result("备用线路添加失败！该地址已被当前会话使用")
            return
        if error == "SAVE_FAILED":
            yield event.plain_result("备用线路添加失败！服务器保存失败，请稍后重试")
            return
        if not result.get("ok"):
            yield event.plain_result("备用线路添加失败！")
            return

        final_name = str(result.get("server", server_name))
        final_address = str(result.get("address", raw_address))
        yield event.plain_result(
            f"备用线路添加成功！服务器 [{final_name}] 已添加备用地址 [{final_address}]"
        )

    @filter.regex(r"^/(?:查询服务器|查询)(?:\s+\S+)?\s*$")
    async def query_server(self, event: AstrMessageEvent):
        """查询 MC 服务器：/查询服务器 [服务器名称|服务器地址]；或 /查询 [服务器名称|服务器地址]"""
        if self._should_ignore_self_event(event):
            return
        if self._check_command_rate_limit(event):
            yield event.plain_result("请求过于频繁，请稍后再试")
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

            normalized_query = self._normalize_address(query_token)
            primary_address = _store_mod.find_server_primary_by_line(
                servers,
                normalized_query,
            )
            if primary_address is not None:
                yield await self._query_single_server(event, primary_address)
                return

            # 未命中已添加的服务器名称时，尝试按地址直连查询；
            # 若输入看起来是名称而非地址，则直接反馈不存在。
            if "." not in query_token and ":" not in query_token:
                yield event.plain_result(
                    f"当前会话内不存在名为 [{query_token}] 的服务器"
                )
                return

            if self.direct_query_requires_admin and self._is_group_admin_denied(event):
                yield event.plain_result("权限不足：直连未保存地址仅限管理员")
                return

            yield await self._query_direct_address(
                event,
                normalized_query,
            )
            return

        summary, failures = await self._query_all_servers(event)
        messages_to_send = failures + [summary]
        final_message = "\n".join(messages_to_send)
        yield event.plain_result(final_message)

    @filter.regex(r"^/模板(?:\s+\S+)?\s*$")
    async def switch_template(self, event: AstrMessageEvent):
        """模板切换命令。

        - `/模板`：列出 templates 目录下的全部模板名（不带 .py）。
        - `/模板 <模板名>`：切换当前会话模板。
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

        if self._is_mutation_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
            return
        if self._check_command_rate_limit(event):
            yield event.plain_result("请求过于频繁，请稍后再试")
            return

        if (
            template_name == "reload"
            and not self._template_file_path(template_name).is_file()
        ):
            self._reload_template_caches()
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

    @filter.regex(r"^/模板重载\s*$")
    async def reload_templates(self, event: AstrMessageEvent):
        """Reload template and rendered-image caches."""
        if self._should_ignore_self_event(event):
            return
        if self._is_mutation_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
            return

        self._reload_template_caches()
        yield event.plain_result("模板缓存已重载")

    @filter.regex(r"^/(?:重命名服务器|重命名)\s+\S+\s+\S+\s*$")
    async def rename_server(self, event: AstrMessageEvent):
        """重命名当前会话中的服务器：/重命名服务器 <旧名称> <新名称>；或 /重命名 <旧名称> <新名称>"""
        if self._should_ignore_self_event(event):
            return
        if self._is_mutation_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
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

    @filter.regex(r"^/(?:删除服务器|删除)\s+\S+\s*$")
    async def delete_server(self, event: AstrMessageEvent):
        """删除当前会话中的服务器：/删除服务器 <服务器名称>；或 /删除 <服务器名称>"""
        if self._should_ignore_self_event(event):
            return
        if self._is_mutation_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
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
        if result.get("error") == "AMBIGUOUS_SERVER_NAME":
            yield event.plain_result(
                f"删除失败！检测到多个同名服务器 [{target_name}]，请使用服务器地址删除"
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

    # 安全边界：该高风险操作仅允许消息命令触发，禁止注册为 LLM Tool。
    @filter.regex(r"^/数据清除\s*$")
    async def clear_session_data(self, event: AstrMessageEvent):
        """清除当前会话保存的全部服务器及不再被引用的对应缓存。"""
        if self._should_ignore_self_event(event):
            return
        # 批量清除始终要求群管理员；私聊会话不需要管理员角色。
        if self._is_group_admin_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
            return

        result = await self._clear_session_data(event.unified_msg_origin)
        if result.get("error") == "SAVE_FAILED":
            yield event.plain_result("数据清除失败！服务器数据保存失败，请稍后重试")
            return
        if not result.get("ok"):
            yield event.plain_result("数据清除失败！请稍后重试")
            return

        removed_count = int(result.get("removed_count", 0) or 0)
        if removed_count == 0:
            yield event.plain_result("当前会话暂无服务器数据可清除")
            return
        yield event.plain_result(
            f"数据清除成功！已删除当前会话内 {removed_count} 个服务器，并清理对应缓存"
        )

    @filter.regex(r"^/(?:服务器列表|列表)\s*$")
    async def list_servers(self, event: AstrMessageEvent):
        """列出当前会话内服务器：/服务器列表；或 /列表"""
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
        r"^/(?:添加服务器|添加|查询服务器|查询|删除服务器|删除|数据清除|重命名服务器|重命名|重定向|服务器列表|列表|模板|帮助|help)(?:\s+.*)?$"
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
            BACKUP_SERVER_PATTERN,
            QUERY_SERVER_PATTERN,
            DELETE_SERVER_PATTERN,
            CLEAR_DATA_PATTERN,
            RENAME_SERVER_PATTERN,
            LIST_SERVER_PATTERN,
            TEMPLATE_PATTERN,
            TEMPLATE_RELOAD_PATTERN,
            REDIRECT_SERVER_PATTERN,
        )
        if any(pattern.match(message) for pattern in valid_patterns):
            return

        yield event.plain_result(self._build_help_message())

    @filter.regex(r"^/重定向\s+\S+\s+\S+\s*$")
    async def redirect_server(self, event: AstrMessageEvent):
        """重定向 MC 服务器地址：/重定向 <服务器名称> <新地址>"""
        if self._should_ignore_self_event(event):
            return
        if self._is_mutation_denied(event):
            yield event.plain_result("权限不足：该操作仅限管理员")
            return
        if self._check_command_rate_limit(event):
            yield event.plain_result("请求过于频繁，请稍后再试")
            return
        matched = REDIRECT_SERVER_PATTERN.match(event.message_str.strip())
        if not matched:
            yield event.plain_result(self._build_help_message())
            return
        server_name = matched.group(1).strip()
        new_raw_address = matched.group(2).strip()
        result = await self._redirect_server_data(
            event.unified_msg_origin, server_name, new_raw_address
        )
        if result.get("error") == "SERVER_NOT_FOUND":
            yield event.plain_result(f'重定向失败！服务器 "{server_name}" 不存在')
            return
        if result.get("error") == "AMBIGUOUS_SERVER_NAME":
            yield event.plain_result(
                f'重定向失败！检测到多个同名服务器 "{server_name}"，请先处理重名后再重试'
            )
            return
        if result.get("error") == "CONNECTION_TIMEOUT":
            yield event.plain_result("重定向失败！新地址连接超时")
            return
        if result.get("error") == "CONNECTION_FAILED":
            yield event.plain_result("重定向失败！新地址无法连接")
            return
        if result.get("error") == "INVALID_ADDRESS":
            yield event.plain_result("重定向失败！新地址无效或指向内网/保留网络")
            return
        if not result.get("ok"):
            yield event.plain_result("重定向失败！")
            return
        yield event.plain_result(
            f"{result['server_name']} 重定向 {result['old_address']} -> {result['new_address']}，新地址延迟: {result['latency']} ms"
        )

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
            allow_direct = (
                not self.direct_query_requires_admin
                or not self._is_group_admin_denied(event)
            )
            cache_key = (
                self._build_tool_status_cache_key(session_key, server)
                + f"|direct={int(allow_direct)}"
            )
            cached = self._try_get_tool_status_cache(cache_key)
            if cached is not None:
                return self._with_tool_meta(cached | {"cached": True})
            async with self._tool_query_semaphore:
                result = await self._query_server_data(
                    session_key,
                    server,
                    allow_direct=allow_direct,
                )
            if result.get("ok"):
                self._set_tool_status_cache(cache_key, result)
            return self._with_tool_meta(result | {"cached": False})
        except Exception as exc:
            return self._tool_internal_error("query_mc_server", exc)

    @filter.llm_tool(name="query_history_status")
    async def query_history_status_tool(
        self, event: AstrMessageEvent, server: str
    ) -> dict[str, Any]:
        """查询已保存 Minecraft 服务器在当前配置窗口内的缓存延迟历史。

        当用户询问某个服务器的延迟趋势、最高延迟、最低延迟或历史延迟
        记录时调用。窗口由历史点数与静默轮询间隔共同决定。server 只能传
        当前会话中已保存的服务器名称。

        Args:
            server(string): 已保存的服务器名称，例如：生存服、测试服。

        Examples:
            生存服
            测试服

        不要用于查询服务器当前是否在线或当前延迟；当前状态请调用
        query_mc_server。
        """
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            result = await self._query_history_status_data(
                event.unified_msg_origin, server
            )
            return self._with_tool_meta(result)
        except Exception as exc:
            return self._tool_internal_error("query_history_status", exc)

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
        if self._is_mutation_denied(event):
            return self._tool_permission_denied()
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            return self._with_tool_meta(
                await self._add_server_data(event.unified_msg_origin, name, address)
            )
        except Exception as exc:
            return self._tool_internal_error("add_mc_server", exc)

    @filter.llm_tool(name="add_mc_server_backup")
    async def add_mc_server_backup_tool(
        self,
        event: AstrMessageEvent,
        server: str,
        address: str,
    ) -> dict[str, Any]:
        """为当前会话中已保存的 Minecraft 服务器添加备用线路。

        当用户要求为一个已保存服务器添加备用、备选或故障转移地址时调用。
        添加前会验证新地址可连接。群聊仅管理员可用，私聊不要求管理员权限。

        Args:
            server(string): 当前会话中已保存的服务器名称，例如：生存服。
            address(string): 与会话内所有已保存线路不同的新地址，例如：backup.example.com:25565。
        """
        if self._is_group_admin_denied(event):
            return self._tool_permission_denied()
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            result = await self._add_backup_server_data(
                event.unified_msg_origin,
                server,
                address,
            )
            return self._with_tool_meta(result)
        except Exception as exc:
            return self._tool_internal_error("add_mc_server_backup", exc)

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
        if self._is_mutation_denied(event):
            return self._tool_permission_denied()
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
        if self._is_mutation_denied(event):
            return self._tool_permission_denied()
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

        不要用于切换 Minecraft 客户端版本、Java 版本、材质包、
        光影包、Mod 或服务器自身插件。
        """
        if self._is_mutation_denied(event):
            return self._tool_permission_denied()
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

    @filter.llm_tool(name="redirect_mc_server")
    async def redirect_mc_server_tool(
        self, event: AstrMessageEvent, name: str, new_address: str
    ) -> dict[str, Any]:
        """重定向已保存的 Minecraft 服务器到新地址。更换前会先验证新地址能否连接。

        Args:
            name(string): 当前会话中已保存的服务器名称
            new_address(string): 新的服务器地址
        """
        session_key = event.unified_msg_origin
        if self._is_mutation_denied(event):
            return self._tool_permission_denied()
        rate_limited = self._check_tool_rate_limit(self._build_tool_actor_key(event))
        if rate_limited:
            return rate_limited
        try:
            result = await self._redirect_server_data(session_key, name, new_address)
            return self._with_tool_meta(result)
        except Exception as exc:
            return self._tool_internal_error("redirect_mc_server", exc)

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

    def _check_command_rate_limit(self, event: AstrMessageEvent) -> bool:
        actor_key = self._build_tool_actor_key(event)
        now = time.monotonic()
        window_start = now - TOOL_RATE_LIMIT_WINDOW_SECONDS
        hits = [
            hit
            for hit in self._command_rate_limit_hits.get(actor_key, [])
            if hit >= window_start
        ]
        self._command_rate_limit_hits[actor_key] = hits
        if len(hits) >= TOOL_RATE_LIMIT_MAX_CALLS:
            return True
        hits.append(now)
        return False

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
        token = (server or "").strip()
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
        window_start = time.monotonic() - TOOL_RATE_LIMIT_WINDOW_SECONDS
        removed = 0
        for key, entry in list(self._tool_status_cache.items()):
            if entry.expires_at <= now:
                self._tool_status_cache.pop(key, None)
                removed += 1
        for key, entry in list(self._tool_list_cache.items()):
            if entry.expires_at <= now:
                self._tool_list_cache.pop(key, None)
                removed += 1
        for actor_key, hits in list(self._tool_rate_limit_hits.items()):
            active_hits = [hit for hit in hits if hit >= window_start]
            removed += len(hits) - len(active_hits)
            if active_hits:
                self._tool_rate_limit_hits[actor_key] = active_hits
            else:
                self._tool_rate_limit_hits.pop(actor_key, None)
        for actor_key, hits in list(self._command_rate_limit_hits.items()):
            active_hits = [hit for hit in hits if hit >= window_start]
            removed += len(hits) - len(active_hits)
            if active_hits:
                self._command_rate_limit_hits[actor_key] = active_hits
            else:
                self._command_rate_limit_hits.pop(actor_key, None)
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

    def _tool_permission_denied(self) -> dict[str, Any]:
        return self._with_tool_meta(
            {
                "ok": False,
                "error": "PERMISSION_DENIED",
                "message": "administrator permission is required",
            }
        )

    async def _query_server_data(
        self,
        session_key: str,
        server: str,
        *,
        allow_direct: bool = True,
    ) -> dict[str, Any]:
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

        if matched_addresses:
            primary_address = matched_addresses[0]
        else:
            normalized_address = self._normalize_address(query_token)
            primary_address = _store_mod.find_server_primary_by_line(
                servers,
                normalized_address,
            )
        managed = primary_address is not None
        address = primary_address if managed else self._normalize_address(query_token)
        if not managed and not allow_direct:
            return {
                "ok": False,
                "online": False,
                "server": query_token,
                "address": address,
                "managed": False,
                "error": "PERMISSION_DENIED",
                "message": "administrator permission is required for direct queries",
            }
        saved_server = servers.get(address, {})
        display_name = str(
            saved_server.get("name", query_token if managed else address)
        )

        if managed:
            query_result = await self._query_saved_server_lines(
                address,
                saved_server,
                need_players=False,
            )
            if query_result.status is None:
                error = query_result.error or "CONNECTION_FAILED"
                messages = {
                    "INVALID_ADDRESS": (
                        "server address must resolve to public IP addresses"
                    ),
                    "CONNECTION_TIMEOUT": "server connection timed out",
                    "CONNECTION_FAILED": "server connection failed",
                }
                return {
                    "ok": False,
                    "online": False,
                    "server": display_name,
                    "primary_address": address,
                    "address": query_result.address,
                    "line_type": query_result.line_type,
                    "attempted_addresses": query_result.attempted_addresses,
                    "managed": True,
                    "error": error,
                    "message": messages.get(error, "server connection failed"),
                }
            status = query_result.status
            actual_address = query_result.address
            line_type = query_result.line_type
            attempted_addresses = query_result.attempted_addresses
        else:
            try:
                status = await self._fetch_server_status(
                    address,
                    need_players=False,
                )
            except McServerInvalidAddressError:
                return {
                    "ok": False,
                    "online": False,
                    "server": display_name,
                    "address": address,
                    "managed": False,
                    "error": "INVALID_ADDRESS",
                    "message": "server address must resolve to public IP addresses",
                }
            except McServerTimeoutError:
                return {
                    "ok": False,
                    "online": False,
                    "server": display_name,
                    "address": address,
                    "managed": False,
                    "error": "CONNECTION_TIMEOUT",
                    "message": "server connection timed out",
                }
            except McServerConnectionError:
                return {
                    "ok": False,
                    "online": False,
                    "server": display_name,
                    "address": address,
                    "managed": False,
                    "error": "CONNECTION_FAILED",
                    "message": "server connection failed",
                }
            actual_address = address
            line_type = "direct"
            attempted_addresses = [address]

        now = int(time.time())
        await self._cache_server_icon(actual_address, status.icon_base64)

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
                real_server_obj["motd"] = str(status.motd or "")
                self._append_latency(real_server_obj, status.latency, now)
                await self._save_store(store)
            self._clear_tool_list_cache(session_key)

        await self._cleanup_expired_cache()
        result = self._server_status_to_tool_data(
            status,
            server_name=display_name,
            address=actual_address,
            managed=managed,
        )
        result.update(
            {
                "primary_address": address,
                "line_type": line_type,
                "attempted_addresses": attempted_addresses,
            }
        )
        return result

    async def _query_history_status_data(
        self, session_key: str, server: str
    ) -> dict[str, Any]:
        """读取当前会话中指定服务器在配置窗口内的缓存延迟历史。"""
        query_name = (server or "").strip()
        if not query_name:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "请提供服务器名称",
            }

        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = dict(session_obj.get("servers", {}))
            matched_addresses = self._find_server_addresses_by_name(servers, query_name)
            if not matched_addresses:
                return {
                    "ok": False,
                    "server": query_name,
                    "error": "SERVER_NOT_FOUND",
                    "message": f"未匹配到服务器名称“{query_name}”",
                }
            if len(matched_addresses) > 1:
                return {
                    "ok": False,
                    "server": query_name,
                    "error": "AMBIGUOUS_SERVER_NAME",
                    "message": f"匹配到多个同名服务器“{query_name}”",
                }

            address = matched_addresses[0]
            server_obj = servers.get(address, {})
            history_points = server_obj.get("latency_history", [])
            if not isinstance(history_points, list):
                history_points = []

        window_seconds = max(int(self.history_limit), 1) * max(
            int(self.silent_query_interval_seconds), 1
        )
        window = _query_mod.format_history_window(window_seconds)
        history_status = _query_mod.build_history_status(
            history_points,
            window_seconds=window_seconds,
        )
        message = (
            f"服务器“{query_name}”近{window}暂无缓存延迟记录"
            if not history_status["history"]
            else (
                f"服务器“{query_name}”近{window}暂无有效延迟记录"
                if history_status["max_latency"] is None
                else f"已返回服务器“{query_name}”近{window}缓存延迟记录"
            )
        )
        return {
            "ok": True,
            "server": query_name,
            "address": address,
            "window": window,
            "window_seconds": window_seconds,
            "message": message,
            **history_status,
        }

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
        if len(desired_name) > _store_mod.MAX_SERVER_NAME_LENGTH:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "server name is too long",
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
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            existing_servers: dict[str, dict[str, Any]] = session_obj["servers"]
            existing_primary = _store_mod.find_server_primary_by_line(
                existing_servers,
                address,
            )
            if existing_primary is not None:
                return {
                    "ok": False,
                    "server": str(
                        existing_servers[existing_primary].get("name", desired_name)
                    ),
                    "address": address,
                    "error": "SERVER_ALREADY_EXISTS",
                    "message": "server already exists",
                }
            if len(existing_servers) >= self.max_servers_per_session:
                return {
                    "ok": False,
                    "server": desired_name,
                    "address": address,
                    "error": "SERVER_LIMIT_REACHED",
                    "message": "saved server limit reached for this session",
                    "limit": self.max_servers_per_session,
                }

        try:
            status = await self._fetch_server_status(address, need_players=False)
        except McServerInvalidAddressError:
            return {
                "ok": False,
                "server": desired_name,
                "address": address,
                "error": "INVALID_ADDRESS",
                "message": "server address must resolve to public IP addresses",
            }
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
            existing_primary = _store_mod.find_server_primary_by_line(servers, address)
            if existing_primary is not None:
                return {
                    "ok": False,
                    "server": str(servers[existing_primary].get("name", desired_name)),
                    "address": address,
                    "error": "SERVER_ALREADY_EXISTS",
                    "message": "server already exists",
                }
            if len(servers) >= self.max_servers_per_session:
                return {
                    "ok": False,
                    "server": desired_name,
                    "address": address,
                    "error": "SERVER_LIMIT_REACHED",
                    "message": "saved server limit reached for this session",
                    "limit": self.max_servers_per_session,
                }

            final_name, name_duplicated = self._resolve_unique_server_name(
                desired_name,
                servers,
            )
            now = int(time.time())
            servers[address] = {
                "name": final_name,
                "address": address,
                "backup_addresses": [],
                "latency_history": [],
                "last_latency": status.latency,
                "motd": str(status.motd or ""),
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

    async def _add_backup_server_data(
        self,
        session_key: str,
        server_name: str,
        raw_address: str,
    ) -> dict[str, Any]:
        """Validate and append an ordered backup line to a saved server."""
        server_name = (server_name or "").strip()
        raw_address = (raw_address or "").strip()
        if not server_name or not raw_address:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "server and address are required",
            }
        if not self.auto_append_default_port and self._has_invalid_port_segment(
            raw_address
        ):
            return {
                "ok": False,
                "server": server_name,
                "address": raw_address,
                "error": "INVALID_ADDRESS",
                "message": "server address port is invalid",
            }

        address = self._normalize_address(raw_address)
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = session_obj["servers"]
            matched = self._find_server_addresses_by_name(servers, server_name)
            if not matched:
                return {
                    "ok": False,
                    "server": server_name,
                    "address": address,
                    "error": "SERVER_NOT_FOUND",
                    "message": "saved server not found in current session",
                }
            if len(matched) > 1:
                return {
                    "ok": False,
                    "server": server_name,
                    "address": address,
                    "error": "AMBIGUOUS_SERVER_NAME",
                    "message": "multiple saved servers use this name",
                }
            primary_address = matched[0]
            if _store_mod.is_server_line_address_in_use(servers, address):
                return {
                    "ok": False,
                    "server": server_name,
                    "primary_address": primary_address,
                    "address": address,
                    "error": "SERVER_ADDRESS_ALREADY_EXISTS",
                    "message": "address already belongs to a saved server line",
                }

        try:
            status = await self._fetch_server_status(address, need_players=False)
        except McServerInvalidAddressError:
            return {
                "ok": False,
                "server": server_name,
                "address": address,
                "error": "INVALID_ADDRESS",
                "message": "server address must resolve to public IP addresses",
            }
        except McServerTimeoutError:
            return {
                "ok": False,
                "server": server_name,
                "address": address,
                "error": "CONNECTION_TIMEOUT",
                "message": "server connection timed out",
            }
        except McServerConnectionError:
            return {
                "ok": False,
                "server": server_name,
                "address": address,
                "error": "CONNECTION_FAILED",
                "message": "server connection failed",
            }

        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers = session_obj["servers"]
            matched = self._find_server_addresses_by_name(servers, server_name)
            if not matched:
                return {
                    "ok": False,
                    "server": server_name,
                    "address": address,
                    "error": "SERVER_NOT_FOUND",
                    "message": "saved server was removed during validation",
                }
            if len(matched) > 1:
                return {
                    "ok": False,
                    "server": server_name,
                    "address": address,
                    "error": "AMBIGUOUS_SERVER_NAME",
                    "message": "multiple saved servers use this name",
                }
            primary_address = matched[0]
            if _store_mod.is_server_line_address_in_use(servers, address):
                return {
                    "ok": False,
                    "server": server_name,
                    "primary_address": primary_address,
                    "address": address,
                    "error": "SERVER_ADDRESS_ALREADY_EXISTS",
                    "message": "address already belongs to a saved server line",
                }

            server_obj = servers[primary_address]
            backup_addresses = list(server_obj.get("backup_addresses", []))
            backup_addresses.append(address)
            server_obj["backup_addresses"] = backup_addresses
            server_obj["last_latency"] = status.latency
            server_obj["motd"] = str(status.motd or "")
            final_name = str(server_obj.get("name", server_name) or server_name)
            try:
                await self._save_store(store)
            except Exception as exc:
                logger.exception("save backup server line failed: %s", exc)
                return {
                    "ok": False,
                    "server": final_name,
                    "primary_address": primary_address,
                    "address": address,
                    "error": "SAVE_FAILED",
                    "message": "server save failed",
                }

        self._clear_query_render_cache(session_key, primary_address)
        self._clear_tool_status_cache(session_key)
        self._clear_tool_list_cache(session_key)
        await self._cache_server_icon(address, status.icon_base64)
        await self._cleanup_expired_cache()
        return {
            "ok": True,
            "server": final_name,
            "primary_address": primary_address,
            "address": address,
            "line_type": "backup",
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
            primary_addresses = self._find_server_addresses_by_name(servers, target)
            if not primary_addresses:
                # 名称未命中时按规范化地址匹配（补全默认端口后与存储键对齐）
                normalized_target = self._normalize_address(target)
                matched_primary = _store_mod.find_server_primary_by_line(
                    servers,
                    normalized_target,
                )
                if matched_primary is not None:
                    primary_addresses = [matched_primary]
            if not primary_addresses:
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
            if len(primary_addresses) > 1:
                return {
                    "ok": False,
                    "server": target,
                    "error": "AMBIGUOUS_SERVER_NAME",
                    "message": "multiple saved servers use this name",
                }

            removed: list[dict[str, Any]] = []
            removed_line_addresses: list[str] = []
            for primary_address in primary_addresses:
                server_obj = servers.pop(primary_address, None)
                if server_obj:
                    line_addresses = _store_mod.get_server_line_addresses(
                        primary_address,
                        server_obj,
                    )
                    removed_line_addresses.extend(line_addresses)
                    removed.append(
                        {
                            "name": str(server_obj.get("name", target)),
                            "address": primary_address,
                            "backup_addresses": line_addresses[1:],
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
            referenced_addresses = self._collect_referenced_addresses(store)

        for primary_address in primary_addresses:
            self._clear_query_render_cache(session_key, primary_address)
        for address in removed_line_addresses:
            if address not in referenced_addresses:
                self._delete_server_cache(address)
        self._clear_tool_status_cache(session_key)
        self._clear_tool_list_cache(session_key)

        return {
            "ok": True,
            "server": target,
            "removed_count": len(removed),
            "removed": removed,
        }

    async def _clear_session_data(self, session_key: str) -> dict[str, Any]:
        """Clear all saved servers in one session after a successful store write."""
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers: dict[str, dict[str, Any]] = session_obj.get("servers", {})
            removed_count = len(servers)
            addresses = _store_mod.get_session_server_addresses(servers)

            if addresses:
                session_obj["servers"] = {}
                try:
                    await self._save_store(store)
                except Exception as exc:
                    logger.exception("clear session data save failed: %s", exc)
                    return {
                        "ok": False,
                        "error": "SAVE_FAILED",
                        "message": "session server data save failed",
                    }

                referenced_addresses = self._collect_referenced_addresses(store)
                unreferenced_addresses = [
                    address
                    for address in addresses
                    if address not in referenced_addresses
                ]
            else:
                unreferenced_addresses = []

            for key in list(self._query_render_cache):
                if key[0] == session_key:
                    self._query_render_cache.pop(key, None)
            self._clear_tool_status_cache(session_key)
            self._clear_tool_list_cache(session_key)

            if unreferenced_addresses:

                def _delete_caches() -> None:
                    for address in unreferenced_addresses:
                        self._delete_server_cache(address)

                await asyncio.to_thread(_delete_caches)

        return {
            "ok": True,
            "removed_count": removed_count,
            "already_empty": removed_count == 0,
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
            if not addresses:
                # 名称未命中时按规范化地址匹配（补全默认端口后与存储键对齐）
                normalized_old = self._normalize_address(old_name)
                if normalized_old in servers:
                    addresses = [normalized_old]
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

        self._clear_query_render_cache(session_key, address)
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

    async def _redirect_server_data(
        self, session_key: str, server_name: str, new_raw_address: str
    ) -> dict[str, Any]:
        """重定向已保存服务器到新地址（先验证新地址可连再更新）。"""
        server_name = (server_name or "").strip()
        new_raw_address = (new_raw_address or "").strip()
        if not server_name or not new_raw_address:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENT",
                "message": "name and new_address are required",
            }
        new_address = self._normalize_address(new_raw_address)
        if not self.auto_append_default_port and self._has_invalid_port_segment(
            new_raw_address
        ):
            return {
                "ok": False,
                "error": "INVALID_ADDRESS",
                "message": "new address port is invalid",
            }

        # 1) 先在锁内查服务器信息，但不在锁内做网络 I/O
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers = session_obj["servers"]
            matched = self._find_server_addresses_by_name(servers, server_name)
            if not matched:
                return {
                    "ok": False,
                    "server_name": server_name,
                    "error": "SERVER_NOT_FOUND",
                    "message": f"server '{server_name}' not found",
                }
            if len(matched) > 1:
                return {
                    "ok": False,
                    "server_name": server_name,
                    "error": "AMBIGUOUS_SERVER_NAME",
                    "message": f"multiple saved servers use name '{server_name}'",
                }
            old_address = matched[0]
            if _store_mod.is_server_line_address_in_use(
                servers,
                new_address,
                exclude_primary=old_address,
            ):
                return {
                    "ok": False,
                    "server_name": server_name,
                    "error": "SERVER_ALREADY_EXISTS",
                    "message": "new address already belongs to a saved server line",
                }

        # 2) 锁外验证新地址（网络 I/O，不阻塞其他操作）
        try:
            status = await self._fetch_server_status(new_address, need_players=False)
        except McServerInvalidAddressError:
            return {
                "ok": False,
                "server_name": server_name,
                "error": "INVALID_ADDRESS",
                "message": "new address must resolve to public IP addresses",
            }
        except McServerTimeoutError:
            return {
                "ok": False,
                "server_name": server_name,
                "error": "CONNECTION_TIMEOUT",
                "message": "new address connection timed out",
            }
        except McServerConnectionError:
            return {
                "ok": False,
                "server_name": server_name,
                "error": "CONNECTION_FAILED",
                "message": "new address connection failed",
            }

        # 3) 锁内快速写入
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            servers = session_obj["servers"]

            # 二次校验并重新读取最新记录，避免覆盖网络等待期间的更新。
            current_server = servers.get(old_address)
            if not current_server:
                return {
                    "ok": False,
                    "server_name": server_name,
                    "error": "SERVER_NOT_FOUND",
                    "message": "server was removed during redirect",
                }

            # 地址冲突检测
            if _store_mod.is_server_line_address_in_use(
                servers,
                new_address,
                exclude_primary=old_address,
            ):
                return {
                    "ok": False,
                    "server_name": server_name,
                    "error": "SERVER_ALREADY_EXISTS",
                    "message": "new address already belongs to a saved server line",
                }

            moved_server = dict(current_server)
            del servers[old_address]
            servers[new_address] = moved_server
            servers[new_address]["address"] = new_address
            servers[new_address]["motd"] = str(status.motd or "")
            servers[new_address]["last_latency"] = status.latency
            try:
                await self._save_store(store)
            except Exception as exc:
                logger.exception("redirect save failed: %s", exc)
                return {
                    "ok": False,
                    "server_name": server_name,
                    "error": "SAVE_FAILED",
                    "message": "server save failed",
                }
            referenced_addresses = self._collect_referenced_addresses(store)

        self._clear_tool_status_cache(session_key)
        self._clear_tool_list_cache(session_key)
        self._clear_query_render_cache(session_key, old_address)
        self._clear_query_render_cache(session_key, new_address)
        await self._cache_server_icon(new_address, status.icon_base64)
        if old_address != new_address and old_address not in referenced_addresses:
            self._delete_server_cache(old_address)
        return {
            "ok": True,
            "server_name": moved_server.get("name", server_name),
            "old_address": old_address,
            "new_address": new_address,
            "latency": status.latency,
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

    async def _query_saved_server_lines(
        self,
        primary_address: str,
        server_obj: dict[str, Any],
        *,
        need_players: bool,
    ) -> SavedServerQueryResult:
        """Query a logical server's primary and backups in saved order."""
        attempted_addresses: list[str] = []
        last_error = "CONNECTION_FAILED"
        for index, line_address in enumerate(
            _store_mod.get_server_line_addresses(primary_address, server_obj)
        ):
            attempted_addresses.append(line_address)
            try:
                status = await self._fetch_server_status(
                    line_address,
                    need_players=need_players,
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
                    attempted_addresses=attempted_addresses,
                )

        return SavedServerQueryResult(
            status=None,
            address=primary_address,
            line_type="primary",
            attempted_addresses=attempted_addresses,
            error=last_error,
        )

    def _find_cached_server_icon(
        self,
        primary_address: str,
        server_obj: dict[str, Any],
    ) -> Path | None:
        """Return the first cached icon following primary/backup order."""
        for line_address in _store_mod.get_server_line_addresses(
            primary_address,
            server_obj,
        ):
            icon_path = self._icon_cache_path(line_address)
            if icon_path.is_file():
                return icon_path
        return None

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

        server_name = str(server_obj.get("name", address) or address)
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

        # 1) 按主线路、备用线路顺序拉取服务端状态（含玩家 sample）
        query_result = await self._query_saved_server_lines(
            address,
            server_obj,
            need_players=True,
        )
        status = query_result.status
        if status is None:
            self._clear_query_render_cache(session_key, address)
            history = server_obj.get("latency_history", [])
            if not isinstance(history, list):
                history = []
            cached_motd = str(server_obj.get("motd", "") or "").strip()
            if not cached_motd:
                cached_motd = DEFAULT_OFFLINE_MOTD
            now = int(time.time())
            icon_path = self._find_cached_server_icon(address, server_obj)
            renderer = await self._get_template_renderer(template_name)
            image_b64 = await self._call_template_renderer(
                renderer,
                server_name=server_name,
                server_address=address,
                latency="Offline",
                offline=True,
                players_online=0,
                players_max=0,
                server_version="Unknown",
                motd=cached_motd,
                history=self._build_render_history(
                    [*history, {"timestamp": now, "latency": 0}],
                    now_ts=now,
                ),
                history_title=self._build_history_title(),
                icon_path=str(icon_path) if icon_path is not None else None,
                players=[],
            )
            return event.make_result().base64_image(image_b64)

        # 2) 写回最新延迟、历史与 Motd
        actual_address = query_result.address
        now = int(time.time())
        async with self._store_lock:
            store = await self._load_store()
            session_obj = self._get_or_create_session(store, session_key)
            real_server_obj = session_obj["servers"].get(address)
            if not real_server_obj:
                return event.plain_result("查询失败！群聊内无该服务器")
            real_server_obj["last_latency"] = status.latency
            real_server_obj["last_active_query_at"] = now
            real_server_obj["motd"] = str(status.motd or "")
            self._append_latency(real_server_obj, status.latency, now)
            history = list(real_server_obj["latency_history"])
            await self._save_store(store)

        # 3) 刷新图标与玩家头像缓存，清理过期缓存并生成渲染图
        await self._cache_server_icon(actual_address, status.icon_base64)
        players_for_render = await self._cache_and_collect_player_avatars(
            actual_address,
            status.players,
        )
        await self._cleanup_expired_cache()
        icon_path = self._icon_cache_path(actual_address)
        render_history = self._build_render_history(history, now_ts=now)
        renderer = await self._get_template_renderer(template_name)
        image_b64 = await self._call_template_renderer(
            renderer,
            server_name=server_name,
            server_address=actual_address,
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
            status = await self._fetch_server_status(address, need_players=True)
        except McServerInvalidAddressError:
            self._clear_query_render_cache(session_key, address)
            return event.plain_result(
                f"服务器 [{address}] 地址不允许查询内网或保留网络"
            )
        except Exception:
            self._clear_query_render_cache(session_key, address)
            return event.plain_result(f"服务器 [{address}] 查询失败！")

        now = int(time.time())
        await self._cache_server_icon(address, status.icon_base64)
        players_for_render = await self._cache_and_collect_player_avatars(
            address,
            status.players,
        )
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
            # 直连查询不写入持久化历史，但当前查询结果仍需显示在图表最新点。
            history=self._build_render_history(
                [{"timestamp": now, "latency": status.latency}],
                now_ts=now,
            ),
            history_title=self._build_history_title(),
            icon_path=str(icon_path) if icon_path.exists() else None,
            players=players_for_render,
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
                server_name = str(server_obj.get("name", address) or address)
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
                        f"服务器 [{server_name}] 查询失败！",
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
            server_name = str(server_obj.get("name", address) or address)
            results.append(
                f"{server_name}: 延迟 : {status.latency}ms | 玩家人数 : {status.players_online}/{status.players_max}"
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
                    real_server_obj["motd"] = str(status.motd or "")
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
            if not isinstance(session_key, str) or not isinstance(session_obj, dict):
                continue
            servers = session_obj.get("servers", {})
            if not isinstance(servers, dict):
                continue
            for address in servers:
                if not isinstance(address, str):
                    continue
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
                    server_obj["motd"] = str(status.motd or "")
                    self._append_latency(server_obj, status.latency, now)
                await self._save_store(store)

    async def _fetch_server_status(
        self,
        address: str,
        *,
        need_players: bool,
    ) -> ServerStatus:
        """Delegate status fetching to the query module."""
        return await _query_mod.fetch_server_status(
            address=address,
            need_players=need_players,
            status_timeout=self.status_timeout_seconds,
        )

    async def _cache_server_icon(self, address: str, icon_base64: str | None) -> None:
        """Delegate icon caching to the cache module."""
        await _cache_mod.cache_server_icon(self._cache_root, address, icon_base64)

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

        bounded_players: list[dict[str, str]] = []
        for player in players[: _query_mod.MAX_PLAYER_SAMPLES]:
            if not isinstance(player, dict):
                continue
            name = str(player.get("name", "") or "")[
                : _query_mod.MAX_PLAYER_NAME_LENGTH
            ]
            if not name:
                continue
            bounded_players.append(
                {"name": name, "uid": str(player.get("uid", "") or "")[:64]}
            )
        if not bounded_players:
            return []

        if not self._session:
            return [
                {"name": player["name"], "avatar_path": ""}
                for player in bounded_players
            ]

        now = int(time.time())
        semaphore = self._avatar_download_semaphore or asyncio.Semaphore(
            self.avatar_download_concurrency
        )

        async def _resolve_one(player: dict[str, str]) -> dict[str, str]:
            name = player["name"]
            try:
                uid = _cache_mod.normalize_player_uid(player.get("uid", ""), name)
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
                        and now - int(avatar_path.stat().st_mtime)
                        <= self.cache_ttl_seconds
                    ):
                        return {"name": name, "avatar_path": str(avatar_path)}
                    _ = await _avatar_mod.download_and_render_avatar_by_uuid(
                        uid=uid,
                        avatar_path=avatar_path,
                        skin_api_url_template=self.skin_api_url_template,
                        avatar_download_retries=self.avatar_download_retries,
                        semaphore=semaphore,
                        session=self._session,
                    )

                return {
                    "name": name,
                    "avatar_path": str(avatar_path) if avatar_path.exists() else "",
                }
            except Exception as exc:
                logger.debug("avatar cache failed for %r: %s", name, exc)
                return {"name": name, "avatar_path": ""}

        fallback = [
            {"name": player["name"], "avatar_path": ""} for player in bounded_players
        ]
        try:
            return await asyncio.wait_for(
                asyncio.gather(*[_resolve_one(player) for player in bounded_players]),
                timeout=max(float(self.avatar_batch_timeout_seconds), 0.001),
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("avatar batch timed out for %s", address)
            return fallback

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
                if not isinstance(session_obj, dict):
                    continue
                servers = session_obj.get("servers", {})
                if not isinstance(servers, dict):
                    continue
                for primary_address, server_obj in servers.items():
                    if not isinstance(primary_address, str) or not isinstance(
                        server_obj, dict
                    ):
                        continue
                    for address in _store_mod.get_server_line_addresses(
                        primary_address,
                        server_obj,
                    ):
                        current = session_server_map.get(address)
                        if current is None or self._server_last_touch(
                            server_obj
                        ) > self._server_last_touch(current):
                            session_server_map[address] = server_obj

        for address, server_obj in session_server_map.items():
            cache_dir = self._server_cache_dir(address)
            if not cache_dir.exists():
                continue

            last_touch_ts = self._server_last_touch(server_obj)
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

        # 直连查询（/查询 <地址>）产生的缓存目录不在存储中，按目录内最新文件
        # 修改时间清理：全部文件超过 TTL 未更新则整体删除；否则仅清理内部过期文件。
        # 注意不能用目录 mtime——覆盖已有文件（如 icon.png、skins/*.png）不会刷新它。
        if not self._cache_root.is_dir():
            return
        managed_dirs = {
            self._server_cache_dir(address) for address in session_server_map
        }
        for cache_dir in self._cache_root.iterdir():
            if not cache_dir.is_dir() or cache_dir in managed_dirs:
                continue
            try:
                latest_mtime = 0
                for file_path in cache_dir.rglob("*"):
                    if file_path.is_file():
                        latest_mtime = max(latest_mtime, int(file_path.stat().st_mtime))
            except OSError:
                continue
            # 空目录或全部文件过期：整体回收
            if latest_mtime == 0 or now - latest_mtime > self.cache_ttl_seconds:
                shutil.rmtree(cache_dir, ignore_errors=True)
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

    @staticmethod
    def _server_last_touch(server_obj: dict[str, Any]) -> int:
        try:
            last_active = int(server_obj.get("last_active_query_at", 0) or 0)
        except (TypeError, ValueError):
            last_active = 0
        try:
            created_at = int(server_obj.get("created_at", 0) or 0)
        except (TypeError, ValueError):
            created_at = 0
        return last_active if last_active > 0 else created_at

    @staticmethod
    def _collect_referenced_addresses(store: dict[str, Any]) -> set[str]:
        addresses: set[str] = set()
        sessions = store.get("sessions", {})
        if not isinstance(sessions, dict):
            return addresses
        for session_obj in sessions.values():
            if not isinstance(session_obj, dict):
                continue
            servers = session_obj.get("servers", {})
            if not isinstance(servers, dict):
                continue
            typed_servers = {
                address: server_obj
                for address, server_obj in servers.items()
                if isinstance(address, str) and isinstance(server_obj, dict)
            }
            addresses.update(_store_mod.get_session_server_addresses(typed_servers))
        return addresses

    async def _load_store(self) -> dict[str, Any]:
        """读取插件存储。

        统一保证返回至少包含：
            {"sessions": {}}
        """
        data = await self.get_kv_data("session_servers", {"sessions": {}})
        if not isinstance(data, dict):
            return {"sessions": {}}
        if not isinstance(data.get("sessions"), dict):
            data["sessions"] = {}
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
        current_sessions = current.get("sessions")
        if not isinstance(current_sessions, dict):
            current_sessions = {}
            current["sessions"] = current_sessions
        incoming_sessions = data.get("sessions", {})
        if isinstance(incoming_sessions, dict):
            current_sessions.update(incoming_sessions)
        await self.put_kv_data("session_servers", current)

    @staticmethod
    def _get_or_create_session(
        store: dict[str, Any], session_key: str
    ) -> dict[str, Any]:
        """Delegate session normalization to the store module."""
        return _store_mod.get_or_create_session(store, session_key)

    def _list_templates(self) -> list[str]:
        """委托到 template_loader 模块。"""
        return _tl_mod.list_templates(self._templates_dir)

    @staticmethod
    def _is_valid_template_name(name: str) -> bool:
        """委托到 template_loader 模块。"""
        return _tl_mod.is_valid_template_name(name)

    def _template_file_path(self, template_name: str) -> Path:
        """委托到 template_loader 模块。"""
        return _tl_mod.template_file_path(self._templates_dir, template_name)

    async def _get_template_renderer(
        self, template_name: str
    ) -> _tl_mod.TemplateRenderer:
        """Delegate renderer loading to the template loader module."""
        return await _tl_mod.get_template_renderer(
            template_name,
            self._templates_dir,
            self._template_renderer_cache,
        )

    def _load_runtime_config(self) -> None:
        """读取插件配置并覆盖运行时参数。"""
        self.silent_query_interval_seconds = self._get_config_int(
            "silent_query_interval_seconds",
            SILENT_QUERY_INTERVAL_SECONDS,
            min_value=60,
            max_value=7 * 24 * 60 * 60,
        )
        self.history_limit = self._get_config_int(
            "history_limit",
            HISTORY_LIMIT,
            min_value=1,
            max_value=2_048,
        )
        self.cache_ttl_seconds = self._get_config_int(
            "cache_ttl_seconds",
            CACHE_TTL_SECONDS,
            min_value=60,
            max_value=30 * 24 * 60 * 60,
        )
        self.status_timeout_seconds = self._get_config_int(
            "status_timeout_seconds",
            STATUS_TIMEOUT,
            min_value=1,
            max_value=60,
        )
        self.query_all_concurrency = self._get_config_int(
            "query_all_concurrency",
            QUERY_ALL_CONCURRENCY,
            min_value=1,
            max_value=20,
        )
        self.max_servers_per_session = self._get_config_int(
            "max_servers_per_session",
            MAX_SERVERS_PER_SESSION,
            min_value=1,
            max_value=500,
        )
        self.avatar_download_concurrency = self._get_config_int(
            "avatar_download_concurrency",
            AVATAR_DOWNLOAD_CONCURRENCY,
            min_value=1,
            max_value=20,
        )
        self.avatar_download_retries = self._get_config_int(
            "avatar_download_retries",
            AVATAR_DOWNLOAD_RETRIES,
            min_value=0,
            max_value=5,
        )
        self.avatar_batch_timeout_seconds = self._get_config_int(
            "avatar_batch_timeout_seconds",
            AVATAR_BATCH_TIMEOUT_SECONDS,
            min_value=1,
            max_value=120,
        )
        self.render_timeout_seconds = self._get_config_int(
            "render_timeout_seconds",
            RENDER_TIMEOUT_SECONDS,
            min_value=1,
            max_value=120,
        )
        self.query_result_cache_ttl_seconds = self._get_config_int(
            "query_result_cache_ttl_seconds",
            QUERY_RESULT_CACHE_TTL_SECONDS,
            min_value=1,
            max_value=300,
        )
        self.skin_api_url_template = self._normalize_skin_api_url_template(
            self._get_config_str("skin_api_url_template", SKIN_API_URL_TEMPLATE)
        )
        self.auto_append_default_port = self._get_config_bool(
            "auto_append_default_port",
            AUTO_APPEND_DEFAULT_PORT,
        )
        self.mutation_requires_admin = self._get_config_bool(
            "mutation_requires_admin",
            MUTATION_REQUIRES_ADMIN,
        )
        self.direct_query_requires_admin = self._get_config_bool(
            "direct_query_requires_admin",
            DIRECT_QUERY_REQUIRES_ADMIN,
        )

    def _get_config_int(
        self,
        key: str,
        default: int,
        *,
        min_value: int = 0,
        max_value: int | None = None,
    ) -> int:
        """Read an integer setting and clamp it to a safe range."""
        raw = (
            self._plugin_config.get(key, default)
            if hasattr(self._plugin_config, "get")
            else default
        )
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        value = max(value, min_value)
        return min(value, max_value) if max_value is not None else value

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
        """Validate a bounded HTTP(S) skin URL with only a UUID placeholder."""
        if not template or len(template) > 2_048:
            return SKIN_API_URL_TEMPLATE
        try:
            fields = []
            for _, field_name, format_spec, conversion in Formatter().parse(template):
                if field_name is None:
                    continue
                if field_name != "uuid" or format_spec or conversion:
                    return SKIN_API_URL_TEMPLATE
                fields.append(field_name)
            if not fields:
                return SKIN_API_URL_TEMPLATE
            preview = template.format(uuid="0" * 32)
            parsed = urlsplit(preview)
        except (KeyError, TypeError, ValueError):
            return SKIN_API_URL_TEMPLATE
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() or ord(char) < 32 for char in preview)
        ):
            return SKIN_API_URL_TEMPLATE
        return template

    def _append_latency(
        self, server_obj: dict[str, Any], latency: int, now_ts: int
    ) -> None:
        """Delegate latency retention to the store module."""
        _store_mod.append_latency(
            server_obj,
            latency,
            now_ts,
            self.history_limit,
            bucket_seconds=self.silent_query_interval_seconds,
        )

    def _build_render_history(
        self,
        history_points: list[dict[str, Any]],
        *,
        now_ts: int | None = None,
    ) -> list[dict[str, int]]:
        """Delegate render-history construction to the query module."""
        return _query_mod.build_render_history(
            history_points,
            now_ts=now_ts,
            history_limit=self.history_limit,
            silent_query_interval_seconds=self.silent_query_interval_seconds,
        )

    @staticmethod
    def _find_server_addresses_by_name(
        servers: dict[str, dict[str, Any]], query_name: str
    ) -> list[str]:
        """按显示名称匹配会话内已添加服务器。"""
        return _store_mod.find_server_addresses_by_name(servers, query_name)

    @staticmethod
    def _resolve_unique_server_name(
        desired_name: str,
        servers: dict[str, dict[str, Any]],
        *,
        exclude_address: str | None = None,
    ) -> tuple[str, bool]:
        """会话内服务器名称去重。"""
        return _store_mod.resolve_unique_server_name(
            desired_name, servers, exclude_address=exclude_address
        )

    def _normalize_address(self, address: str) -> str:
        """标准化服务器地址。"""
        return _store_mod.normalize_address(address, self.auto_append_default_port)

    @staticmethod
    def _address_hash(address: str) -> str:
        """将地址映射为稳定哈希。"""
        return _store_mod.address_hash(address)

    def _server_cache_dir(self, address: str) -> Path:
        return _cache_mod.server_cache_dir(self._cache_root, address)

    def _icon_cache_path(self, address: str) -> Path:
        return _cache_mod.icon_cache_path(self._cache_root, address)

    def _skin_cache_path(self, address: str, uid: str) -> Path:
        return _cache_mod.skin_cache_path(self._cache_root, address, uid)

    def _delete_server_cache(self, address: str) -> None:
        _cache_mod.delete_server_cache(self._cache_root, address)

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
    def _is_admin_event(event: AstrMessageEvent) -> bool:
        try:
            checker = getattr(event, "is_admin", None)
            return bool(checker()) if callable(checker) else False
        except Exception:
            return False

    @staticmethod
    def _is_private_event(event: AstrMessageEvent) -> bool:
        try:
            checker = getattr(event, "is_private_chat", None)
            return bool(checker()) if callable(checker) else False
        except Exception:
            return False

    def _is_group_admin_denied(self, event: AstrMessageEvent) -> bool:
        return not self._is_private_event(event) and not self._is_admin_event(event)

    def _is_mutation_denied(self, event: AstrMessageEvent) -> bool:
        return self.mutation_requires_admin and self._is_group_admin_denied(event)

    @staticmethod
    def _build_help_message() -> str:
        """构建命令帮助文案。"""
        return (
            "命令用法：\n"
            "/添加服务器 <服务器名称> <服务器地址>\n"
            "/添加 <服务器名称> <服务器地址>\n"
            "/备用线路 <服务器名称> <备用地址>\n"
            "/备用 <服务器名称> <备用地址>\n"
            "/bak <服务器名称> <备用地址>\n"
            "/查询服务器 [服务器名称|服务器地址]\n"
            "/查询 [服务器名称|服务器地址]\n"
            "/删除服务器 <服务器名称>\n"
            "/删除 <服务器名称>\n"
            "/数据清除\n"
            "/重命名服务器 <旧名称> <新名称>\n"
            "/重命名 <旧名称> <新名称>\n"
            "/服务器列表\n"
            "/列表\n"
            "/重定向 <服务器名称> <新地址>\n"
            "/模板 [模板名|reload]\n"
            "/模板重载\n"
            "/帮助 或 /help"
        )

    @staticmethod
    def _has_invalid_port_segment(address: str) -> bool:
        """Return whether the server address syntax or port is invalid."""
        try:
            _store_mod.parse_server_address(address)
        except _store_mod.InvalidServerAddressError:
            return True
        return False

    def _build_history_title(self) -> str:
        """委托到 query 模块。"""
        return _query_mod.build_history_title(
            self.history_limit, self.silent_query_interval_seconds
        )

    async def _call_template_renderer(
        self,
        renderer: _tl_mod.TemplateRenderer,
        **kwargs: Any,
    ) -> str:
        """Render within a fixed total timeout."""
        return await asyncio.wait_for(
            _tl_mod.call_template_renderer(renderer, **kwargs),
            timeout=max(float(self.render_timeout_seconds), 0.001),
        )

    @staticmethod
    def _build_query_cache_key(
        *,
        session_key: str,
        address: str,
        template_name: str,
        mode: str,
    ) -> tuple[str, str, str, str]:
        return session_key, address, template_name, mode

    def _try_get_query_render_cache(
        self, cache_key: tuple[str, str, str, str]
    ) -> str | None:
        now = time.time()
        entry = self._query_render_cache.get(cache_key)
        if not entry:
            return None
        if entry.expires_at <= now:
            self._query_render_cache.pop(cache_key, None)
            return None
        return entry.image_b64

    def _set_query_render_cache(
        self,
        cache_key: tuple[str, str, str, str],
        image_b64: str,
    ) -> None:
        self._query_render_cache[cache_key] = QueryRenderCacheEntry(
            expires_at=time.time() + float(self.query_result_cache_ttl_seconds),
            image_b64=image_b64,
        )

    def _reload_template_caches(self) -> None:
        self._template_renderer_cache.clear()
        self._query_render_cache.clear()

    def _clear_query_render_cache(self, session_key: str, address: str) -> None:
        for key in list(self._query_render_cache.keys()):
            if key[0] == session_key and key[1] == address:
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
