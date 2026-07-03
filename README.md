# astrbot_plugin_get_mc_server_info

> 一个支持 **传统命令** 与 **LLM Tool Calling** 的 Minecraft Java 服务器查询插件。

> ⭐ AstrBot v4.18+  
> ⭐ 会话隔离存储  
> ⭐ Function Calling（AI 自动调用）  
> ⭐ 高性能缓存与并发控制

> **说明**
>
> 本项目部分代码由 AI 辅助生成，核心逻辑及测试由开发者完成。

---

# ✨ 功能特色

## 🎮 Minecraft 服务器管理

支持按群聊/私聊（Session）独立管理服务器列表：

- 添加服务器
- 删除服务器
- 重命名服务器
- 查询单个服务器
- 查询全部服务器
- 查看服务器列表

所有数据均按会话隔离，不同群聊互不影响。

---

## 🤖 AI Tool Calling（Function Calling）

本插件已支持 AstrBot 官方 LLM Tool Calling。

支持 Function Calling 的模型（如 GPT、Claude、Gemini、Qwen、DeepSeek 等）可以根据自然语言**自动调用插件**。

例如：

> 用户：

```
帮我看看 Hypixel 在线吗？
```

AI 将自动调用：

```
query_mc_server()
```

无需输入：

```
#查询 Hypixel
```

同样支持：

> 把测试服加入服务器列表

↓

```
add_mc_server()
```

> 删除生存服

↓

```
delete_mc_server()
```

> 当前有哪些服务器？

↓

```
list_mc_servers()
```

整个过程无需用户了解插件命令。

---

## 🚀 高性能设计

插件针对频繁查询进行了优化：

- 查询结果缓存（Tool Cache）
- 渲染结果缓存
- 图标缓存
- 玩家头像缓存
- Tool 调用限流（Rate Limit）
- Tool 并发控制（Semaphore）
- Session 隔离
- 自动清理过期缓存

能够有效减少重复网络请求。

---

# 📦 功能列表

✅ 会话隔离服务器管理

✅ Minecraft Java Server Ping

✅ 玩家头像下载

✅ Motd 渲染

✅ 延迟历史曲线

✅ 多模板渲染

✅ 模板热重载

✅ Tool Calling

✅ 自动缓存

✅ 并发控制

✅ Rate Limit

---

# 📖 命令

## 添加服务器

```
#添加服务器 <名称> <地址>
#添加 <名称> <地址>
```

---

## 查询服务器

```
#查询服务器
```

查询当前会话所有服务器。

```
#查询服务器 <服务器名称>
```

查询已添加服务器。

```
#查询服务器 <服务器地址>
```

直接查询地址。

```
#查询
```

为简称。

---

## 删除服务器

```
#删除服务器 <名称>

#删除 <名称>
```

---

## 重命名服务器

```
#重命名服务器 <旧名称> <新名称>

#重命名 <旧名称> <新名称>
```

---

## 查看服务器列表

```
#服务器列表

#列表
```

---

## 模板

```
#模板

#模板 <模板名>

#模板 reload
```

---

## 帮助

```
#帮助

#help
```

---

# 🤖 AI Tool

插件注册了以下 LLM Tool：

| Tool | 功能 |
|------|------|
| query_mc_server | 查询服务器状态 |
| add_mc_server | 添加服务器 |
| delete_mc_server | 删除服务器 |
| rename_mc_server | 重命名服务器 |
| list_mc_servers | 查看服务器列表 |
| switch_mc_template | 切换模板 |

支持 Tool Calling 的模型将自动调用，无需任何 Prompt。

---

# 🔄 工作流程

## 普通命令

```
用户

↓

#查询 生存服

↓

插件

↓

业务层

↓

图片渲染

↓

返回图片
```

---

## AI 自动调用

```
用户

↓

自然语言

↓

LLM

↓

Tool Calling

↓

业务层

↓

JSON

↓

LLM

↓

自然语言回复
```

无需用户了解插件命令。

---

# ⚙ 配置项

插件配置由 `_conf_schema.json` 定义。

| 配置项 | 说明 |
|---------|------|
| silent_query_interval_seconds | 静默轮询间隔 |
| history_limit | 历史记录长度 |
| cache_ttl_seconds | 图标/头像缓存 |
| status_timeout_seconds | 查询超时 |
| auto_append_default_port | 自动补全25565端口 |
| query_all_concurrency | 全服查询并发 |
| avatar_download_concurrency | 玩家头像下载并发 |
| avatar_download_retries | 下载重试次数 |
| query_result_cache_ttl_seconds | 渲染结果缓存 |

若启用了 Tool Calling，还包括：

- Tool 查询缓存
- Tool Rate Limit
- Tool 并发控制
- Tool 自动缓存清理

---

# 🎨 模板系统

支持自定义模板。

模板目录：

```
templates/
```

支持：

```
default_method.py
```

背景图：

```
default_method.png
```

字体：

```
.ttf
.ttc
.otf
```

模板支持热重载：

```
#模板 reload
```

无需重启 AstrBot。

---

# 📁 项目结构

```
astrbot_plugin_get_mc_server_info/

├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── templates/
├── cache/
├── icons/
├── avatars/
└── README.md
```

---

# 🔧 依赖

```
aiohttp

mcstatus

Pillow

PILSkinMC
```

安装：

```bash
pip install -r requirements.txt
```

---

# 💡 使用建议

## 推荐

支持 Function Calling 的模型：

- GPT
- Claude
- Gemini
- Qwen
- DeepSeek

开启 AstrBot Tool 后即可自动调用插件。

---

## 传统命令

若模型不支持 Function Calling，插件仍可正常使用所有命令。

无需修改任何配置。

---

# ❓ FAQ

### 为什么 AI 不会自动调用？

请确认：

- AstrBot ≥ 4.18
- 当前模型支持 Function Calling
- Tool 已启用
- 人格 Prompt 未禁止 Tool

---

### 为什么查询的是图片？

命令模式默认返回渲染图。

Tool 模式返回 JSON，由 LLM 组织最终回复。

---

### Tool 会影响原有命令吗？

不会。

Tool 与 Regex Command 共用业务层。

两者互不影响。


---

# License

MIT License