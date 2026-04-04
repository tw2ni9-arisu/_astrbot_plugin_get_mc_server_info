# astrbot_plugin_get_mc_server_info

用于按会话（群聊/私聊）管理并查询 Minecraft Java 服务器状态的 AstrBot 插件。

## 功能概览

- 会话隔离存储：不同群聊/私聊的服务器列表互不影响。
- 添加服务器校验：`#添加服务器` 时先连通性检查，失败不入库。
- 静默轮询：按配置间隔轮询服务器并维护延迟历史。
- 主动单服查询（按名称）：`#查询服务器 <服务器名称>` 命中当前会话已添加服务器后返回渲染图。
- 主动单服查询（按地址直连）：`#查询服务器 <服务器地址>` 可直接单次查询，不入库、不拉取玩家头像。
- 主动全服查询：返回多行文本汇总，失败服务器单独提示。
- 模板切换：支持列出模板、切换模板、重载模板缓存。
- 缓存管理：图标与头像缓存按 TTL 自动过期清理。
- 历史补零：静默查询失败或新加服务器历史不足时，渲染历史曲线会自动补齐缺失点为 `0`。

## 命令

- `#添加服务器 <服务器名称> <服务器地址>`
- `#查询服务器 <服务器名称>`
- `#查询服务器 <服务器地址>`
- `#查询服务器`
- `#模板`
- `#模板 <模板名>`
- `#模板 reload`

## 查询行为说明

- `#查询服务器`：查询当前会话内全部已添加服务器（文本汇总）。
- `#查询服务器 <服务器名称>`：优先在当前会话内按名称精确匹配已添加服务器并进行主动单次查询。
- `#查询服务器 <服务器地址>`：当名称未命中时按地址直连查询，不会将该服务器添加到当前会话。
- 地址直连查询不会下载/渲染玩家头像，渲染图中的玩家列表为空。
- 若出现同名服务器（同一会话内重名），会提示改用地址查询以消除歧义。

## 配置说明

插件运行配置由 `_conf_schema.json` 定义，可在 AstrBot 插件配置中修改：（此处默认使用mua联合皮肤站API）

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
- 自定义字体：将字体文件放入 `templates` 目录即可自动生效（优先级高于系统字体）。
  - 支持后缀：`.ttf`、`.ttc`、`.otf`
- 模板背景图：在 `templates` 下放置与模板同名图片即可自动使用，例如：
  - `default_method.py`
  - `default_method.png`（或 jpg/jpeg/webp/bmp）

## 渲染细节

- 头像放大策略：头像采用最近邻缩放（`NEAREST`），不使用插值算法，以保持像素清晰度。
- 历史曲线数据：渲染时会按 `history_limit` 与 `silent_query_interval_seconds` 生成固定长度历史序列，缺失点自动补 `0`。

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
