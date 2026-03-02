# CoPaw — AGENTS.md

CoPaw（v0.0.4b2）是一个可本地/云端部署的个人 AI 助手，通过 Skills 扩展能力，支持 DingTalk、Feishu、QQ、Discord、iMessage 等多渠道，内置 Cron 调度。

## 技术栈

**后端**：Python 3.10~3.13 · FastAPI · agentscope==1.0.16.dev0 · agentscope-runtime==1.1.0 · APScheduler ≥3.11,<4 · Click · uvicorn
**前端（Console）**：React 18 · TypeScript 5.8 · Vite 6 · Ant Design 5 · react-router-dom v7 · Less
**本地模型**：llama.cpp（`copaw[llamacpp]`）· MLX（`copaw[mlx]`，Apple Silicon）
**部署**：Docker（`deploy/Dockerfile`）· supervisord

## 构建与测试命令

```bash
# 安装后端（开发模式）
pip install -e ".[dev]"

# 运行测试
pytest

# 单独运行某测试文件
pytest src/tests/test_foo.py

# 后端格式化 / lint（pre-commit 也会自动跑）
black --line-length=79 src/
flake8 src/
pre-commit run --all-files

# 前端（Console）
cd console
npm ci
npm run build       # 生产构建 → console/dist/
npm run lint        # ESLint
npm run format      # Prettier

# 启动开发服务器（需先 pip install -e .）
copaw init --defaults
copaw app            # 开启 http://127.0.0.1:8088/
```

## 目录结构

```
src/copaw/
  agents/       CoPawAgent（ReActAgent 子类）、工具注册、Skills 加载器
  app/          FastAPI 应用、渠道管理（ChannelManager）、Cron、路由
  cli/          Click CLI（app/init/cron/skills/channels/models/env/…）
  config/       配置加载（config.json）、ConfigWatcher
  providers/    LLM Provider 注册表、Ollama 管理
  local_models/ llama.cpp / MLX 本地模型
  envs/         持久化环境变量存储
  tokenizer/    内置分词器（tiktoken 兼容）
console/        React 前端（Web UI）
website/        文档站
deploy/         Dockerfile + supervisord 模板
doc/            设计文档（非自动生成，人工维护）
scripts/        构建 / 发布脚本
```

**运行时工作目录**（默认 `~/.copaw/`，可用 `COPAW_WORKING_DIR` 覆盖）：

```
~/.copaw/
  config.json          主配置文件
  active_skills/       已激活的 Skills
  customized_skills/   用户自定义 Skills
  memory/              长期记忆文件
  models/              本地模型文件
  jobs.json            Cron 任务
  chats.json           会话记录
```

## 编码规范

**Python**
- 格式化：black，`--line-length=79`
- 行宽：79 字符（flake8 同步）
- 类型检查：mypy（`--ignore-missing-imports`，`--follow-imports=skip`）
- 文件头：`# -*- coding: utf-8 -*-`
- Skills 目录（`**/skills/**`）豁免所有 lint，不要在其中强加规范

**TypeScript / React**
- 格式化：prettier 3.0.0（`console/` 根目录配置）
- Lint：eslint 9 + typescript-eslint + react-hooks + react-refresh
- 组件：函数式组件 + hooks，不用类组件
- 样式：Less（Ant Design token 优先）

**通用**
- Skills 最小单元：含 `SKILL.md`（带 YAML front matter `name` + `description`）的目录
- 工具注册统一在 `src/copaw/agents/react_agent.py` 的 `_create_toolkit()` 中
- 渠道（Channel）通过 `COPAW_ENABLED_CHANNELS` 环境变量控制

**核心模式示例**

```python
# 新增 CLI 命令
import click
from ..cli.main import cli

@click.command("my-cmd")
@click.pass_context
def my_cmd(ctx: click.Context) -> None:
    """My command description."""
    host = ctx.obj["host"]
    port = ctx.obj["port"]
    ...

cli.add_command(my_cmd)
```

## 关键环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `COPAW_WORKING_DIR` | 运行时工作目录 | `~/.copaw` |
| `COPAW_LOG_LEVEL` | 日志级别 | `info` |
| `DASHSCOPE_API_KEY` | DashScope LLM 密钥 | — |
| `COPAW_OPENAPI_DOCS` | 开启 /docs（仅开发） | `false` |
| `COPAW_ENABLED_CHANNELS` | 限制启用的渠道（逗号分隔） | 全部 |

## 权限边界

### 始终可以（无需确认）
- 读取文件、列出目录、搜索代码
- 运行 `black` / `flake8` / `prettier` / `eslint` 单文件检查
- 运行单个 pytest 测试
- 读取 `config.json`（不修改）

### 先问再做
- 安装新 pip 或 npm 依赖
- 删除或重命名文件
- 修改 `deploy/` 下的 Docker/supervisord 配置
- 运行 `pytest`（全量）或 `npm run build`
- 执行 `git push` 或提交变更

### 绝对禁止
- 在代码中硬编码 API Key 或任何凭证
- 修改 `~/.copaw/` 运行时数据（memory、config.json 等）
- 删除 `active_skills/` 中的 Skill 目录
- 修改 `src/copaw/tokenizer/` 静态数据文件
