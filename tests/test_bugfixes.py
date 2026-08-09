"""针对 bug 修复的单元测试(不依赖真实 astrbot/mcstatus 环境,全部 mock)。

运行: python -m unittest tests.test_bugfixes -v
"""
import asyncio  # noqa: F401  (确保 asyncio 在 mock 注入前可用)
import copy
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PARENT_DIR = PLUGIN_DIR.parent

# 测试专用临时根目录,asyncTearDown 统一清理,避免在用户机器上残留缓存
_TEMP_ROOT = tempfile.mkdtemp(prefix="astrbot_plugin_test_")


def _make_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ---- astrbot 相关 mock ----
class _FakeLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass


logger_mod = types.ModuleType("astrbot.api.logger")
logger_mod.info = _FakeLogger().info
logger_mod.warning = _FakeLogger().warning
logger_mod.error = _FakeLogger().error
logger_mod.debug = _FakeLogger().debug
logger_mod.exception = _FakeLogger().exception
sys.modules["astrbot.api.logger"] = logger_mod

_make_module("astrbot.api")
_make_module("astrbot.core")
_make_module("astrbot.core.utils")
_make_module(
    "astrbot.core.utils.astrbot_path",
    get_astrbot_temp_path=lambda: _TEMP_ROOT,
)


def _regex_deco(pattern):
    def deco(fn):
        return fn
    return deco


def _llm_tool_deco(**kw):
    def deco(fn):
        return fn
    return deco


filter_mod = _make_module("astrbot.api.event.filter")
filter_mod.regex = _regex_deco
filter_mod.llm_tool = _llm_tool_deco

event_mod = _make_module("astrbot.api.event")
event_mod.AstrMessageEvent = object
event_mod.filter = filter_mod


class _KV:
    def __init__(self):
        self.data = {"sessions": {}}

    async def get(self, key, default):
        return copy.deepcopy(self.data) if key == "session_servers" else default

    async def put(self, key, value):
        self.data = copy.deepcopy(value)


KV = _KV()


class FakeStar:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config

    async def get_kv_data(self, key, default=None):
        return await KV.get(key, default)

    async def put_kv_data(self, key, value):
        await KV.put(key, value)


star_mod = _make_module("astrbot.api.star")
star_mod.Context = object
star_mod.Star = FakeStar

# mcstatus mock(正常路径不触发真实 status 请求)
_make_module("mcstatus", JavaServer=object)

sys.path.insert(0, str(PARENT_DIR))
from astrbot_plugin_get_mc_server_info import main as plugin_main  # noqa: E402
from astrbot_plugin_get_mc_server_info.query import McServerTimeoutError  # noqa: E402


def make_server(name, address):
    return {
        "name": name,
        "address": address,
        "latency_history": [],
        "last_latency": 5,
        "last_silent_query_at": 0,
        "last_active_query_at": 1,
        "created_at": 1,
    }


def fake_status(address="x:25565"):
    return SimpleNamespace(
        address=address,
        latency=12,
        version="1.21",
        players_online=3,
        players_max=20,
        icon_base64=None,
        motd="hello",
        players=[],
    )


class BugFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = plugin_main.Main(
            context=object(), config={"auto_append_default_port": True}
        )
        self.plugin._load_runtime_config()  # 模拟 initialize 阶段的配置加载
        self.assertIs(self.plugin.auto_append_default_port, True)

    async def asyncTearDown(self):
        shutil.rmtree(_TEMP_ROOT, ignore_errors=True)

    def seed(self, servers: dict):
        KV.data = {"sessions": {"sess-1": {"template": "default_method", "servers": servers}}}

    @property
    def stored(self):
        return KV.data["sessions"]["sess-1"]["servers"]

    async def test_delete_by_normalized_address(self):
        """开启端口补全时,按不带端口的地址删除应命中已存键。"""
        self.seed(
            {
                "play.example.com:25565": make_server("生存服", "play.example.com:25565"),
                "mc2.example.com": make_server("测试服", "mc2.example.com"),
            }
        )
        r = await self.plugin._delete_server_data(
            "sess-1", "play.example.com", idempotent=True
        )
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("removed_count"), 1, r)
        self.assertNotIn("play.example.com:25565", self.stored)

    async def test_delete_ambiguous_name(self):
        """重名删除应返回 AMBIGUOUS_SERVER_NAME 且不删除任何服务器。"""
        self.seed(
            {
                "a.com": make_server("同名服", "a.com"),
                "b.com": make_server("同名服", "b.com"),
            }
        )
        r = await self.plugin._delete_server_data("sess-1", "同名服")
        self.assertEqual(r.get("error"), "AMBIGUOUS_SERVER_NAME", r)
        self.assertEqual(len(self.stored), 2)

    async def test_rename_by_normalized_address(self):
        """开启端口补全时,按不带端口的地址重命名应命中已存键。"""
        self.seed(
            {"play.example.com:25565": make_server("生存服", "play.example.com:25565")}
        )
        r = await self.plugin._rename_server_data("sess-1", "play.example.com", "新生存服")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.stored["play.example.com:25565"]["name"], "新生存服")

    async def test_redirect_timeout_error_code(self):
        """redirect 超时应返回 CONNECTION_TIMEOUT 而非 CONNECTION_FAILED。"""
        self.seed({"play.example.com:25565": make_server("生存服", "play.example.com:25565")})

        async def timeout_fetch(address, *, need_players):
            raise McServerTimeoutError("timed out")

        self.plugin._fetch_server_status = timeout_fetch
        r = await self.plugin._redirect_server_data(
            "sess-1", "生存服", "new.example.com:25565"
        )
        self.assertEqual(r.get("error"), "CONNECTION_TIMEOUT", r)

    async def test_redirect_ambiguous_name(self):
        """redirect 重名应返回 AMBIGUOUS_SERVER_NAME 而非静默取第一个。"""
        self.seed(
            {
                "a.com": make_server("同名服", "a.com"),
                "b.com": make_server("同名服", "b.com"),
            }
        )
        r = await self.plugin._redirect_server_data("sess-1", "同名服", "new.example.com:25565")
        self.assertEqual(r.get("error"), "AMBIGUOUS_SERVER_NAME", r)

    async def test_redirect_success(self):
        """redirect 正常路径应更新存储键。"""
        self.seed({"play.example.com:25565": make_server("生存服", "play.example.com:25565")})

        async def ok_fetch(address, *, need_players):
            return fake_status(address)

        self.plugin._fetch_server_status = ok_fetch
        r = await self.plugin._redirect_server_data("sess-1", "生存服", "new.example.com:25565")
        self.assertTrue(r.get("ok"), r)
        self.assertIn("new.example.com:25565", self.stored)
        self.assertNotIn("play.example.com:25565", self.stored)

    async def test_unchanged_when_port_append_disabled(self):
        """未开启端口补全时,精确地址删除与 idempotent 语义保持不变。"""
        plugin2 = plugin_main.Main(
            context=object(), config={"auto_append_default_port": False}
        )
        plugin2._load_runtime_config()
        KV.data = {
            "sessions": {
                "sess-1": {
                    "template": "default_method",
                    "servers": {"mc2.example.com": make_server("测试服", "mc2.example.com")},
                }
            }
        }
        r = await plugin2._delete_server_data("sess-1", "mc2.example.com", idempotent=True)
        self.assertTrue(r.get("ok") and r.get("removed_count") == 1, r)
        r = await plugin2._delete_server_data("sess-1", "不存在", idempotent=True)
        self.assertTrue(r.get("already_deleted") is True and r.get("ok") is True, r)


    async def test_cleanup_orphan_cache_dirs(self):
        """直连查询产生的缓存目录应被清理:过期整体删除,未过期保留并清理内部过期文件。"""
        import os
        import time as time_mod

        from astrbot_plugin_get_mc_server_info.cache import server_cache_dir

        self.seed({})  # 空会话,无受管服务器,所有缓存目录都按孤儿处理
        root = self.plugin._cache_root
        old_dir = server_cache_dir(root, "old.example.com")
        new_dir = server_cache_dir(root, "new.example.com")
        old_skin = old_dir / "skins" / "a.png"
        old_icon = old_dir / "icon.png"
        new_skin = new_dir / "skins" / "b.png"
        new_icon = new_dir / "icon.png"
        for p in (old_skin, old_icon, new_skin, new_icon):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"png")

        # old 目录整体过期(2 倍 TTL 前),内部文件同样过期
        past = time_mod.time() - self.plugin.cache_ttl_seconds * 2
        os.utime(old_dir, (past, past))
        for p in (old_skin, old_icon):
            os.utime(p, (past, past))
        # new 目录未过期(目录 mtime 保持最新写入),但内部 skin 文件过期
        os.utime(new_skin, (past, past))

        await self.plugin._cleanup_expired_cache()

        self.assertFalse(old_dir.exists(), "过期的孤儿缓存目录应整体删除")
        self.assertTrue(new_dir.exists(), "未过期的孤儿缓存目录应保留")
        self.assertFalse(new_skin.exists(), "未过期目录内的过期皮肤文件应删除")
        self.assertTrue(new_icon.exists(), "未过期目录内的新图标应保留")

    async def test_cleanup_orphan_dir_with_fresh_files(self):
        """目录 mtime 旧但内部文件新(覆盖写入不刷新目录 mtime)时不应整体删除。"""
        import os
        import time as time_mod

        from astrbot_plugin_get_mc_server_info.cache import server_cache_dir

        self.seed({})
        root = self.plugin._cache_root
        cache_dir = server_cache_dir(root, "busy.example.com")
        icon = cache_dir / "icon.png"
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(b"png")  # 最新写入,文件 mtime = now
        old_skin = cache_dir / "skins" / "old.png"
        old_skin.parent.mkdir(parents=True, exist_ok=True)
        old_skin.write_bytes(b"png")

        # 把目录 mtime 拨回 2 倍 TTL 之前,模拟“文件被反复覆盖、目录 mtime 停滞”
        past = time_mod.time() - self.plugin.cache_ttl_seconds * 2
        os.utime(cache_dir, (past, past))
        os.utime(old_skin, (past, past))  # 旧皮肤文件单独过期

        await self.plugin._cleanup_expired_cache()

        self.assertTrue(cache_dir.exists(), "内部有新文件的目录不应被整体删除")
        self.assertTrue(icon.exists(), "新图标不应被删除")
        self.assertFalse(old_skin.exists(), "保留目录内过期的旧皮肤仍应被清理")

    async def test_cleanup_managed_dir_untouched(self):
        """受管(已保存)服务器的缓存目录不应被孤儿逻辑删除。"""
        import os
        import time as time_mod

        from astrbot_plugin_get_mc_server_info.cache import server_cache_dir

        self.seed({"saved.example.com": make_server("保存服", "saved.example.com")})
        root = self.plugin._cache_root
        managed = server_cache_dir(root, "saved.example.com")
        icon = managed / "icon.png"
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(b"png")
        # 即使目录很久没写入,受管目录也只按 last_active_query_at 规则清理
        past = time_mod.time() - self.plugin.cache_ttl_seconds * 2
        os.utime(managed, (past, past))
        os.utime(icon, (past, past))

        await self.plugin._cleanup_expired_cache()

        # last_active_query_at=1(seed 中)距今已超 TTL,受管目录应清空文件但保留目录
        self.assertTrue(managed.exists(), "受管目录不应被整体删除")
        self.assertFalse(icon.exists(), "受管目录内过期图标应被清理")


if __name__ == "__main__":
    unittest.main()
