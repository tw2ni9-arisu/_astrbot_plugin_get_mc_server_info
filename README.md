# astrbot_plugin_get_mc_server_info

用于按会话（群聊/私聊）管理并查询 Minecraft Java 服务器状态的 AstrBot 插件。

## 功能概览

- 会话隔离存储：不同群聊/私聊的服务器列表互不影响。
- 添加服务器校验：`#添加服务器` 时先连通性检查，失败不入库。
- 静默轮询：按配置间隔轮询服务器并维护延迟历史。
- 主动单服查询：返回渲染图（延迟、在线人数、历史折线、玩家头像）。
- 主动全服查询：返回多行文本汇总，失败服务器单独提示。
- 模板切换：支持列出模板、切换模板、重载模板缓存。
- 缓存管理：图标与头像缓存按 TTL 自动过期清理。

## 命令

- `#添加服务器 <服务器名称> <服务器地址>`
- `#查询服务器 <服务器地址>`
- `#查询服务器`
- `#模板`
- `#模板 <模板名>`
- `#模板 reload`

## 配置说明

插件运行配置由 `_conf_schema.json` 定义，可在 AstrBot 插件配置中修改：

| Key | 默认值 | 说明 |
| --- | --- | --- |
| `silent_query_interval_seconds` | `1800` | 静默轮询间隔（秒），最小 60 |
| `history_limit` | `48` | 单服务器延迟历史保留点数，最小 1 |
| `cache_ttl_seconds` | `86400` | 图标/头像缓存生命周期（秒），最小 60 |
| `status_timeout_seconds` | `10` | 查询 MC 状态超时（秒），最小 1 |
| `query_all_concurrency` | `5` | `#查询服务器`（全服）并发上限，最小 1 |
| `avatar_download_concurrency` | `5` | 玩家皮肤下载并发上限，最小 1 |
| `avatar_download_retries` | `2` | 玩家皮肤下载失败重试次数，最小 0 |
| `skin_api_url_template` | `https://skin.mualliance.ltd/api/union/skin/byuuid/{uuid}` | 皮肤 API 模板，必须包含 `{uuid}` 占位符 |

## 模板与资源

- 默认模板：`templates/default_method.py`
- 默认图标：`templates/default_icon.png`（可选）
- 模板背景图：在 `templates` 下放置与模板同名图片即可自动使用，例如：
  - `default_method.py`
  - `default_method.png`（或 jpg/jpeg/webp/bmp）

## 依赖

见 `requirements.txt`：

- `aiohttp`
- `mcstatus`
- `Pillow`
- `PILSkinMC`

未安装依赖时可在插件目录执行：

```bash
pip install -r requirements.txt
```
