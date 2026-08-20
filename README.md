# astrbot_plugin_get_mc_server_info

一个 AstrBot 插件，用于查询 Minecraft Java 版服务器状态。既可以通过传统命令操作，也支持 AstrBot 的 LLM Tool Calling，让模型直接调用插件完成查询和管理。

服务器数据按会话隔离，不同群聊或私聊之间互不影响。

## 功能

- 按会话添加、删除、重命名、重定向和查询服务器
- 查询单个服务器或当前会话全部服务器
- 获取服务器在线状态、延迟、版本、MOTD、在线玩家
- 下载玩家皮肤并渲染头像
- 渲染查询图片，包含 MOTD、延迟历史曲线、玩家列表
- 缓存近 24 小时延迟历史，可查询趋势、最高值和最低值
- 历史图保留断连/离线区间
- 服务器离线时沿用最近一次 MOTD，无记录时使用默认文案
- 支持多模板和模板热重载
- 提供 LLM Tool Calling 接口
- 缓存 Tool 查询结果、渲染结果、图标和头像
- 对 Tool 调用做限流和并发控制
- 自动清理过期缓存

> 说明：本项目部分代码由 AI 辅助生成，核心逻辑及测试由开发者完成。

## 命令

### 添加服务器

```
/添加服务器 <名称> <地址>
/添加 <名称> <地址>
```

### 查询服务器

```
/查询服务器
/查询服务器 <服务器名称>
/查询服务器 <服务器地址>
/查询
```

- `/查询服务器`：查询当前会话全部服务器。
- `/查询服务器 <服务器名称>`：查询已添加的服务器。
- `/查询服务器 <服务器地址>`：直接查询该地址。
- `/查询`：`/查询服务器` 的简称。

### 删除服务器

```
/删除服务器 <名称>
/删除 <名称>
```

### 重命名服务器

```
/重命名服务器 <旧名称> <新名称>
/重命名 <旧名称> <新名称>
```

### 重定向服务器

```
/重定向 <名称> <新地址>
```

把已保存的服务器地址改为新地址，保存前会先验证新地址能否连接。

### 查看服务器列表

```
/服务器列表
/列表
```

### 模板

```
/模板
/模板 <模板名>
/模板 reload
```

- `/模板`：列出可用模板。
- `/模板 <模板名>`：切换当前会话模板。
- `/模板 reload`：重新加载模板缓存。

### 帮助

```
/帮助
/help
```

## LLM Tool

支持 Function Calling 的模型可以自动调用以下 Tool：

| Tool | 参数 | 功能 |
|------|------|------|
| query_mc_server | server | 查询服务器状态，server 可以是已保存名称或地址 |
| query_history_status | server | 查询已保存服务器近 24 小时的缓存延迟趋势、最大值和最小值 |
| add_mc_server | name, address | 添加服务器 |
| delete_mc_server | server | 删除服务器 |
| rename_mc_server | old_name, new_name | 重命名服务器 |
| redirect_mc_server | name, new_address | 重定向服务器到新地址 |
| list_mc_servers | 无 | 列出当前会话服务器 |
| switch_mc_template | template | 切换查询图片模板 |
| resolve_server_name | hint | 根据模糊描述解析服务器候选 |

使用 LLM Tool 需要 AstrBot v4.18+，并启用 AstrBot 的 Tool 功能。

## 处理流程

命令模式：`/查询 生存服` 进入业务层，查询结果渲染成图片返回。

Tool 模式：用户自然语言交给 LLM，模型调用对应 Tool，插件返回 JSON，最后由 LLM 组织回复。

## 配置

插件配置由 `_conf_schema.json` 定义。

| 配置项 | 说明 |
|---------|------|
| silent_query_interval_seconds | 后台静默轮询间隔（秒） |
| history_limit | 每台服务器保留的延迟历史点数 |
| cache_ttl_seconds | 图标和头像缓存时长（秒） |
| status_timeout_seconds | 单次状态查询超时（秒） |
| auto_append_default_port | 地址未带端口时是否补全 25565 |
| query_all_concurrency | 全服查询并发数 |
| avatar_download_concurrency | 头像下载并发数 |
| avatar_download_retries | 头像下载重试次数 |
| query_result_cache_ttl_seconds | 单服查询渲染图片缓存时长（秒） |
| skin_api_url_template | 获取玩家皮肤的 URL 模板，需包含 {uuid} |

## 模板

模板目录为 `templates/`。每个模板由一个 Python 渲染函数和可选资源组成：

- `default_method.py`：渲染逻辑
- `default_method.png`：背景图
- `.ttf` / `.ttc` / `.otf`：字体文件

模板修改后可以用 `/模板 reload` 热重载，不需要重启 AstrBot。

## 项目结构

```
astrbot_plugin_get_mc_server_info/
├── __init__.py
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── store.py
├── query.py
├── cache.py
├── avatar.py
├── template_loader.py
├── templates/
├── README.md
└── LICENSE
```

## 依赖

```
aiohttp>=3.9.0
mcstatus>=11.1.1
Pillow>=10.0.0
PILSkinMC>=1.0.2
```

安装：

```bash
pip install -r requirements.txt
```

## 模型兼容性

模型支持 Function Calling 时，开启 AstrBot Tool 后即可自动调用插件。模型不支持时，传统命令仍然可用，不需要额外配置。

## FAQ

### AI 没有自动调用

检查 AstrBot 版本、模型是否支持 Function Calling、Tool 是否启用，以及人格 Prompt 是否禁用了 Tool。

### 为什么查询结果是一张图片

命令模式默认返回渲染图。Tool 模式返回 JSON，由 LLM 组织最终回复。

### Tool 会影响原有命令吗

不会。Tool 与命令共用同一套业务逻辑。

## License

MIT License
