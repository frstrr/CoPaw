# CoPaw 改动生效机制

> 本文档说明在开发/运行 CoPaw 时，修改哪些内容需要重新编译重启、哪些只需重启、哪些无需重启即可生效。
>
> **前提**：以下分析基于 **editable 安装**（`pip install -e .`）的开发模式。

---

## 核心架构说明

理解生效机制的关键有三点：

1. **每次 query 都新建 `CoPawAgent` 实例**（`app/runner/runner.py`），因此大部分运行时配置在下次发消息时即可重新加载。
2. **`ConfigWatcher` 和 `MCPConfigWatcher`** 分别每 2 秒轮询 `config.json`，检测到变更后自动热重载对应模块。
3. **前端静态文件**（`console/dist/`）由 FastAPI `StaticFiles` 按需从磁盘读取，重建后刷新浏览器即可，不需要重启后端。

---

## ❶ 需要重新编译 + 重启

| 内容 | 操作 | 说明 |
|---|---|---|
| `console/` 前端源码（`.tsx` / `.ts` 等） | `npm run build` → 刷新浏览器 | 后端 serve 的是 `console/dist/`，静态文件按需读取，**不需重启后端** |
| `pyproject.toml`（新增依赖、package-data 等） | `pip install -e .` → 重启 | 依赖变更或包数据文件路径变更时需要重新安装 |

> **注意**：前端重建后无需重启后端，刷新浏览器即可看到新版本。

---

## ❷ 只需重启进程

以下内容修改后，只需重启 `copaw` 服务（无需重新 `pip install`）：

| 内容 | 原因 |
|---|---|
| `src/copaw/**/*.py` 所有 Python 源码 | Python 模块在进程生命周期内只 import 一次 |
| `src/copaw/constant.py` | `WORKING_DIR`、`ACTIVE_SKILLS_DIR` 等常量在启动时确定 |
| `.env` 文件中的环境变量 | `load_envs_into_environ()` 在 `_app.py` 模块导入时执行，仅执行一次 |
| `config.json` 中的 `last_api.host` / `last_api.port` | 进程启动时绑定监听地址，改了必须重启 |
| `src/copaw/agents/md_files/` 内置 MD 模板（非 editable 安装） | 非 editable 安装下文件已被 copy 到包目录，需重新安装 |

---

## ❸ 无需重启，自动热加载或下次请求生效

### 2 秒内自动热重载（后台 Watcher 轮询）

| 内容 | 机制 |
|---|---|
| `config.json` **channels** 部分（enabled、token、app_id 等） | `ConfigWatcher`（`config/watcher.py`）每 2 秒轮询，自动调用 `replace_channel()` |
| `config.json` **mcp** 部分（MCP clients 增删改） | `MCPConfigWatcher`（`app/mcp/watcher.py`）每 2 秒轮询，自动 reload/add/remove clients |

### 下次 query 时自动生效（每请求重新加载）

| 内容 | 机制 |
|---|---|
| `config.json` 运行时字段（`agents.running.max_iters`、`max_input_length`、`show_tool_details`、`agents.language` 等） | `runner.py` 每次请求执行 `load_config()`，重新读取整个 config |
| `providers.json`（切换模型、更改 API key、base_url、active_llm、fallback_llms） | 每次请求创建 `CoPawAgent` → `create_model_and_formatter()` → `load_providers_json()` |
| `~/.copaw/AGENTS.md`、`SOUL.md`、`PROFILE.md` | 每次请求调用 `build_system_prompt_from_working_dir()` 重新读取文件 |
| `~/.copaw/active_skills/` 或 `~/.copaw/customized_skills/` 下的 Skills | 每次请求 `_register_skills()` 调用 `list_available_skills()` 重新扫描目录 |

### 即时生效

| 内容 | 机制 |
|---|---|
| Cron jobs（`~/.copaw/jobs.json`） | 通过 API 增删改直接持久化，`CronManager` 实时响应 |
| `console/dist/` 静态资源 | FastAPI `StaticFiles` 从磁盘按需读取，`npm run build` 后刷新浏览器即可 |

---

## 快速参考

```
修改类型                         所需操作
─────────────────────────────────────────────────────
Python 源码                      重启服务
.env 环境变量                    重启服务
config.json host/port            重启服务
─────────────────────────────────────────────────────
config.json channels/mcp 部分    等待 ≤2 秒（自动热重载）
─────────────────────────────────────────────────────
config.json 其他运行时字段       发下一条消息即生效
providers.json                   发下一条消息即生效
~/.copaw/*.md (系统提示词)        发下一条消息即生效
~/.copaw/active_skills/          发下一条消息即生效
─────────────────────────────────────────────────────
console/ 前端代码                npm run build + 刷新浏览器
pyproject.toml 依赖              pip install -e . + 重启服务
```
