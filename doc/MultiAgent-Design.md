# CoPaw 多 Agent 协同系统设计文档

版本：v0.2
日期：2026-03-02
作者：zhaorun

> **v0.2 更新说明**：参考 OpenClaw 多 Agent 设计（multi-agent personality + session spawn），
> 增加非阻塞 spawn + announce 模式、并发安全阀、子 Agent 工具 allow/deny 策略、session 可见性、
> bindings / `@agent` 直连路由等机制。**核心不变：每个 Agent 都有独立的灵魂/个性。**

---

## 1. 背景与目标

### 1.1 目标
在 CoPaw 现有单 Agent 架构基础上，扩展为**多 Agent 协同**系统：
- 多个 Agent 各自拥有**独立的灵魂（SOUL.md）、记忆（MEMORY.md）、技能（skills）**
- Agent 可以**并行**执行各自的任务
- 一个**管理者 Agent（Orchestrator）** 负责理解用户意图、调度子 Agent、汇总结果
- 管理者 Agent 是与用户沟通的主要界面
- 用户可以通过 Console / API **配置和管理**每个 Agent
- 管理者 Agent **自主决定**使用哪些子 Agent 完成任务

### 1.2 设计原则
- **最小侵入**：尽量复用现有 `CoPawAgent`、`MemoryManager`、`SkillService` 等组件
- **渐进式**：支持单 Agent 模式（向后兼容），多 Agent 模式按需开启
- **文件驱动**：沿用 CoPaw 的 Markdown 文件定义 Agent 身份的模式
- **松耦合**：子 Agent 之间不直接通信，全部通过 Orchestrator 协调
- **灵魂独立**：每个 Agent（包括 Orchestrator 和所有子 Agent）都拥有独立的 SOUL.md / AGENTS.md / PROFILE.md，具有完整的人格和个性（与“轻量 worker subagent”区分）
- **阻塞 + 非阻塞双模**：同步 `dispatch_to_agents`（等待结果）+ 异步 `spawn_agent`（立即返回 runId，结果通过 announce 回传）
- **安全阀**：全局并发上限、单次 fan-out 上限、超时、级联停止，防止 token 爆炸和资源失控

---

## 2. 现有架构分析

### 2.1 当前数据流
```
用户消息 → Channel → AgentRunner.query_handler()
    → 创建 CoPawAgent（每次请求新建）
    → agent.reply(msgs)（ReAct 循环）
    → 流式返回结果
    → 保存 session state
```

### 2.2 关键组件与文件路径

**Agent 核心：**
- `src/copaw/agents/react_agent.py` — `CoPawAgent` 类，继承 AgentScope 的 `ReActAgent`
- `src/copaw/agents/prompt.py` — `PromptBuilder`，从 WORKING_DIR 读取 AGENTS.md / SOUL.md / PROFILE.md 构建 system prompt
- `src/copaw/agents/model_factory.py` — 创建 LLM model 和 formatter
- `src/copaw/agents/skills_manager.py` — 技能管理（builtin/customized/active 三层）
- `src/copaw/agents/memory/memory_manager.py` — `MemoryManager`（基于 ReMeFs）
- `src/copaw/agents/memory/copaw_memory.py` — `CoPawInMemoryMemory`（会话内记忆）
- `src/copaw/agents/command_handler.py` — 系统命令处理
- `src/copaw/agents/hooks/` — 预推理钩子（bootstrap、memory compaction）

**运行时：**
- `src/copaw/app/runner/runner.py` — `AgentRunner`，处理每个查询请求
- `src/copaw/app/runner/session.py` — `SafeJSONSession`，session 持久化
- `src/copaw/app/runner/manager.py` — `ChatManager`，管理对话元数据

**配置与常量：**
- `src/copaw/config/config.py` — `Config` Pydantic 模型
- `src/copaw/constant.py` — 全局常量（`WORKING_DIR`、`ACTIVE_SKILLS_DIR` 等）

**API 层：**
- `src/copaw/app/routers/agent.py` — Agent 文件管理 API
- `src/copaw/app/routers/__init__.py` — 路由注册
- `src/copaw/app/_app.py` — FastAPI 应用入口，lifespan 管理

**Agent 身份文件：**
- `src/copaw/agents/md_files/{en,zh}/SOUL.md` — 灵魂模板
- `src/copaw/agents/md_files/{en,zh}/AGENTS.md` — 行为规则模板
- `src/copaw/agents/md_files/{en,zh}/PROFILE.md` — 身份档案模板

### 2.3 可复用的设计基础
1. **身份系统是文件驱动的** — `PromptBuilder` 从 `working_dir` 读取 md 文件构建 prompt，只需为每个子 Agent 指定不同的 `working_dir` 即可实现独立身份
2. **技能系统已模块化** — `SkillService` 管理 `active_skills` 目录，可为每个 Agent 配置独立目录
3. **MemoryManager 基于 working_dir** — 构造时传入 `working_dir`，天然支持多实例
4. **Agent 是每次请求创建的** — `runner.py:181` 每次 `query_handler` 创建新 `CoPawAgent`，无全局单例限制
5. **Config 系统基于 Pydantic** — 可以平滑扩展 `config.json` 的 schema

---

## 3. 整体架构设计

### 3.1 系统架构图
```
用户 ──── Channel ────► AgentRunner.query_handler()
                              │
                              ▼
                    ┌─────────────────────┐
                    │  Orchestrator Agent  │  (管理者)
                    │  - 理解用户意图       │
                    │  - 决定调度策略       │
                    │  - 自身独立 SOUL/记忆  │
                    └────────┬────────────┘
                             │
              ┌──────────────┼───────────────────┐
              │              │                   │
   dispatch_to_agents     spawn_agent        @agent/bindings
     (同步等待结果)   (非阻塞 + announce)    (可选直连路由)
              │              │                   │
              ▼              ▼                   ▼
        ┌───────────┐  ┌───────────┐      ┌───────────┐
        │ SubAgent A │  │ SubAgent B │ ...  │ SubAgent N │
        │ 独立灵魂    │  │ 独立灵魂    │      │ 独立灵魂    │
        │ 独立记忆    │  │ 独立记忆    │      │ 独立记忆    │
        │ 独立技能    │  │ 独立技能    │      │ 独立技能    │
        │ ToolPolicy │  │ ToolPolicy │      │ ToolPolicy │
        └─────┬─────┘  └─────┬─────┘      └─────┬─────┘
              │              │                   │
              └──────────────┼───────────────────┘
                             ▼
                    Orchestrator 汇总/编排
                             │
                             ▼
                    返回用户（流式）
```

### 3.1.1 调度模式与路由模式

**同步调度 `dispatch_to_agents`（保留自 v0.1）**
- Orchestrator 调用后**阻塞等待**所有子 Agent 完成
- 适用于：短时任务、需要汇总多个结果再回复用户的场景

**非阻塞调度 `spawn_agent`（v0.2 新增，参考 OpenClaw 的 session spawn）**
- Orchestrator 调用后**立即返回** `runId`
- 子 Agent 在后台执行，完成后通过 `announce` 机制将结果回传到 Orchestrator 的 session（对用户不可见）
- 适用于：长时任务（搜索、文件处理、代码分析等）

**直连路由（v0.2 新增，参考 OpenClaw bindings）**
- `@agent_name`：用户显式指定目标 Agent，跳过 Orchestrator 决策
- `bindings`：基于关键词/前缀的确定性路由（可选）
- 优先级建议：`@agent` > `bindings` > Orchestrator 自主调度

### 3.2 文件目录结构
```
~/.copaw/                           # WORKING_DIR（不变）
├── config.json                     # 全局配置（扩展 agents 定义）
├── SOUL.md                         # Orchestrator 的灵魂
├── AGENTS.md                       # Orchestrator 的行为规则
├── PROFILE.md                      # Orchestrator 的身份档案
├── MEMORY.md                       # Orchestrator 的长期记忆
├── memory/                         # Orchestrator 的日志记忆
├── active_skills/                  # Orchestrator 的技能
├── sessions/                       # 所有 session 持久化
│
├── sub_agents/                     # 子 Agent 目录（新增）
│   ├── code-assistant/             # 子 Agent A
│   │   ├── SOUL.md
│   │   ├── AGENTS.md
│   │   ├── PROFILE.md
│   │   ├── MEMORY.md
│   │   ├── memory/
│   │   └── active_skills/
│   │
│   ├── writer/                     # 子 Agent B
│   │   ├── SOUL.md
│   │   ├── AGENTS.md
│   │   ├── PROFILE.md
│   │   ├── MEMORY.md
│   │   ├── memory/
│   │   └── active_skills/
│   │
│   └── researcher/                 # 子 Agent C
│       ├── ...
│       └── active_skills/
│
├── sub_agents_registry.json        # 子 Agent 注册表（新增）
└── spawn_runs/                     # 非阻塞任务运行记录（新增，v0.2）
    ├── {runId}.json                # 每个 spawn 任务的状态和结果
    └── ...
```

### 3.3 运行模式
- **单 Agent 模式（默认，向后兼容）**：`config.json` 中 `multi_agent.enabled = false`，行为与当前完全一致
- **多 Agent 模式**：`multi_agent.enabled = true`，用户消息先发给 Orchestrator，由其决定是否调度子 Agent

---

## 4. 详细模块设计

### 4.1 配置扩展 — `config.py`

在 `Config` 模型中新增 `multi_agent` 字段：

```python
# src/copaw/config/config.py 新增

class ToolPolicy(BaseModel):
    """子 Agent 的工具权限策略（v0.2 新增，参考 OpenClaw 的 allow/deny 机制）。
    默认采用 deny-first 策略：只允许显式列出的工具。"""
    mode: Literal["allow", "deny"] = "allow"  # allow = 白名单模式, deny = 黑名单模式
    tools: List[str] = []                      # 白名单/黑名单中的工具名
    # mode="allow", tools=["file_reader","web_search"] → 只能用这两个
    # mode="deny",  tools=["shell"]              → 除 shell 外都能用

class AgentBinding(BaseModel):
    """确定性路由绑定（v0.2 新增，参考 OpenClaw bindings）。
    当用户消息匹配 pattern 时，跳过 Orchestrator 决策直接路由到指定 Agent。"""
    pattern: str                                # 正则/前缀匹配
    agent_name: str                             # 目标子 Agent

class SubAgentDefinition(BaseModel):
    """单个子 Agent 的定义。"""
    name: str                           # 唯一标识符，如 "code-assistant"
    display_name: str = ""              # 展示名称
    description: str = ""               # 能力描述（Orchestrator 用此选择 Agent）
    enabled: bool = True                # 是否启用
    working_dir: str = ""               # 工作目录（空 = 自动推导为 sub_agents/{name}）
    model_override: Optional[str] = None  # 可选：使用不同的 LLM 模型
    max_iters: int = 30                 # 子 Agent 最大推理迭代
    skills: List[str] = []             # 该 Agent 激活的技能列表（空 = 使用其 active_skills 目录下的全部）
    tool_policy: ToolPolicy = ToolPolicy()  # v0.2: 工具权限策略
    run_timeout_seconds: int = 300      # v0.2: 单次运行超时（秒）

class ConcurrencyConfig(BaseModel):
    """并发安全阀配置（v0.2 新增，参考 OpenClaw 的 concurrency safety valves）。"""
    max_global_concurrent: int = 5      # 全局最大并发子 Agent 数
    max_children_per_dispatch: int = 3  # 单次 dispatch/spawn 最大 fan-out
    run_timeout_seconds: int = 300      # 子 Agent 运行全局默认超时
    cascade_stop: bool = True           # 父任务取消时是否级联停止所有子 Agent

class MultiAgentConfig(BaseModel):
    """多 Agent 协同配置。"""
    enabled: bool = False               # 是否启用多 Agent 模式
    orchestrator_max_iters: int = 20    # Orchestrator 最大迭代
    max_parallel: int = 3               # 最大并行子 Agent 数（向后兼容，与 concurrency.max_children_per_dispatch 取较小值）
    concurrency: ConcurrencyConfig = ConcurrencyConfig()  # v0.2: 并发安全阀
    bindings: List[AgentBinding] = []   # v0.2: 确定性路由绑定
    sub_agents: List[SubAgentDefinition] = []

class Config(BaseModel):
    # ... 现有字段 ...
    multi_agent: MultiAgentConfig = MultiAgentConfig()
```

### 4.2 子 Agent 注册表 — `sub_agents_registry.json`

存储在 `~/.copaw/sub_agents_registry.json`，由 API 和 CLI 管理：

```json
{
  "version": 1,
  "agents": [
    {
      "name": "code-assistant",
      "display_name": "代码助手",
      "description": "擅长代码阅读、编写、调试、重构。熟悉多种编程语言和框架。",
      "enabled": true,
      "created_at": "2026-03-02T10:00:00Z"
    },
    {
      "name": "writer",
      "display_name": "写作助手",
      "description": "擅长撰写文档、报告、邮件、文案。",
      "enabled": true,
      "created_at": "2026-03-02T10:00:00Z"
    }
  ]
}
```

> 注意：`config.json` 中的 `multi_agent.sub_agents` 定义运行时参数（模型、迭代等），
> `sub_agents_registry.json` 存储元信息。两者通过 `name` 关联。

### 4.3 子 Agent 管理服务 — `SubAgentService`

新增文件：`src/copaw/agents/sub_agent_service.py`

```python
class SubAgentService:
    """管理子 Agent 的创建、配置、初始化。"""

    @staticmethod
    def list_sub_agents() -> list[SubAgentInfo]:
        """列出所有已注册的子 Agent。"""

    @staticmethod
    def create_sub_agent(
        name: str,
        display_name: str,
        description: str,
        soul_content: str = "",        # 可选自定义 SOUL.md
        agents_content: str = "",      # 可选自定义 AGENTS.md
        skills: list[str] = None,      # 初始激活的技能
    ) -> bool:
        """创建新的子 Agent，初始化其工作目录和身份文件。"""

    @staticmethod
    def delete_sub_agent(name: str) -> bool:
        """删除子 Agent 的工作目录和注册信息。"""

    @staticmethod
    def get_sub_agent_working_dir(name: str) -> Path:
        """获取子 Agent 的工作目录路径。"""
        return WORKING_DIR / "sub_agents" / name

    @staticmethod
    def init_sub_agent_workspace(name: str, language: str = "zh") -> None:
        """初始化子 Agent 工作目录，复制模板 md 文件。"""

    @staticmethod
    def build_agent_instance(
        name: str,
        env_context: str = "",
        mcp_clients: list = None,
        max_iters: int = 30,
    ) -> CoPawAgent:
        """创建一个子 Agent 的 CoPawAgent 实例，使用其独立的工作目录。"""
```

核心实现思路：`build_agent_instance` 利用子 Agent 的独立 `working_dir` 构建 system prompt 和记忆。
需要修改 `CoPawAgent.__init__` 以支持自定义 `working_dir` 参数（当前硬编码使用全局 `WORKING_DIR`）。

### 4.4 CoPawAgent 改造

需要对 `src/copaw/agents/react_agent.py` 做少量改造，支持自定义工作目录：

```python
class CoPawAgent(ReActAgent):
    def __init__(
        self,
        # ... 现有参数 ...
        working_dir: Path | None = None,  # 新增：自定义工作目录
        agent_name: str = "Friday",       # 新增：自定义 Agent 名称
        custom_skills_dir: Path | None = None,  # 新增：自定义技能目录
    ):
        self._working_dir = working_dir or WORKING_DIR
        # ...其余初始化逻辑使用 self._working_dir 替代 WORKING_DIR...
```

**需要修改的地方：**
1. `_build_sys_prompt()` — `PromptBuilder(working_dir=self._working_dir)`
2. `_register_skills()` — 从 `self._working_dir / "active_skills"` 加载技能
3. `_register_hooks()` — bootstrap hook 使用 `self._working_dir`
4. `_setup_memory_manager()` — MemoryManager 使用 `self._working_dir`

变更范围约 30-50 行，不涉及逻辑重构。

### 4.5 Orchestrator Agent — 核心调度

新增文件：`src/copaw/agents/orchestrator.py`

Orchestrator 本身也是一个 `CoPawAgent`，但额外注册**调度工具**（同步 + 异步）和 `@agent` 路由解析：

```python
class OrchestratorAgent:
    """
    管理者 Agent。包装 CoPawAgent，额外注册 dispatch_to_agents / spawn_agent 工具。
    """

    def __init__(
        self,
        env_context: str = "",
        mcp_clients: list = None,
        memory_manager: MemoryManager | None = None,
        sub_agent_definitions: list[SubAgentDefinition] = None,
        concurrency_config: ConcurrencyConfig = None,
        bindings: list[AgentBinding] = None,
        max_iters: int = 20,
    ):
        self._sub_agent_defs = sub_agent_definitions or []
        self._concurrency = concurrency_config or ConcurrencyConfig()
        self._bindings = bindings or []

        # v0.2: 全局并发信号量
        self._global_semaphore = asyncio.Semaphore(self._concurrency.max_global_concurrent)
        # v0.2: 后台运行中的 spawn 任务 {runId: asyncio.Task}
        self._spawn_runs: dict[str, asyncio.Task] = {}

        # 构建 Orchestrator 自身的 system prompt 增强部分
        agents_description = self._build_agents_description()

        # 创建底层 CoPawAgent（有自己独立的 SOUL/记忆/技能）
        self._agent = CoPawAgent(
            env_context=env_context + "\n\n" + agents_description,
            mcp_clients=mcp_clients,
            memory_manager=memory_manager,
            max_iters=max_iters,
            agent_name="Orchestrator",
        )

        # 注册调度工具（同步 + 异步）
        self._agent.toolkit.register_tool_function(self._create_dispatch_tool())
        self._agent.toolkit.register_tool_function(self._create_spawn_tool())
        self._agent.toolkit.register_tool_function(self._create_query_run_tool())
        self._agent.toolkit.register_tool_function(self._create_cancel_run_tool())

    def _build_agents_description(self) -> str:
        """构建可用子 Agent 的描述，注入到 system prompt 中。"""
        lines = ["## 可用的子 Agent\n"]
        lines.append("你可以使用 `dispatch_to_agents`（同步等待）或 `spawn_agent`（非阻塞）将任务分派给以下子 Agent：\n")
        for agent_def in self._sub_agent_defs:
            if agent_def.enabled:
                lines.append(f"- **{agent_def.display_name or agent_def.name}** "
                             f"(`{agent_def.name}`): {agent_def.description}")
        lines.append("\n### 调度建议")
        lines.append("- 简单/快速任务：直接自己处理，或 `dispatch_to_agents`（同步）")
        lines.append("- 耗时任务：`spawn_agent`（非阻塞），可用 `query_run` 查询进度")
        lines.append("- 多任务：同时 dispatch 多个子 Agent 并行处理")
        return "\n".join(lines)

    # ── 同步调度（保留自 v0.1）──────────────────────────────

    def _create_dispatch_tool(self):
        """创建 dispatch_to_agents 工具函数：同步等待所有子 Agent 完成。"""

        async def dispatch_to_agents(
            tasks: list[dict],
        ) -> str:
            """
            将任务分派给一个或多个子 Agent 并行执行（同步等待全部完成）。

            Args:
                tasks: 任务列表，每个任务是一个 dict：
                    - agent_name: 目标子 Agent 名称
                    - instruction: 给子 Agent 的具体指令

            Returns:
                每个子 Agent 的执行结果汇总
            """
            # v0.2: 限制单次 fan-out
            max_fan = self._concurrency.max_children_per_dispatch
            if len(tasks) > max_fan:
                return f"错误：单次最多调度 {max_fan} 个子 Agent，当前 {len(tasks)} 个"

            results = await self._execute_tasks(tasks)
            return self._format_results(results)

        return dispatch_to_agents

    async def _execute_tasks(self, tasks: list[dict]) -> list[dict]:
        """并行执行多个子 Agent 任务（受全局信号量和超时保护）。"""

        async def run_one(task: dict) -> dict:
            async with self._global_semaphore:
                agent_name = task.get("agent_name", "")
                instruction = task.get("instruction", "")
                agent_def = self._find_agent_def(agent_name)
                timeout = agent_def.run_timeout_seconds if agent_def else self._concurrency.run_timeout_seconds
                try:
                    result = await asyncio.wait_for(
                        self._run_sub_agent(agent_name, instruction),
                        timeout=timeout,
                    )
                    return {"agent_name": agent_name, "status": "success", "result": result}
                except asyncio.TimeoutError:
                    return {"agent_name": agent_name, "status": "timeout", "error": f"超时（{timeout}s）"}
                except Exception as e:
                    return {"agent_name": agent_name, "status": "error", "error": str(e)}

        return await asyncio.gather(*[run_one(t) for t in tasks])

    # ── 非阻塞调度（v0.2 新增）──────────────────────────────

    def _create_spawn_tool(self):
        """创建 spawn_agent 工具函数：非阻塞启动子 Agent，立即返回 runId。"""

        async def spawn_agent(
            agent_name: str,
            instruction: str,
        ) -> str:
            """
            非阻塞启动一个子 Agent 执行任务。
            立即返回 runId，任务在后台执行，完成后结果通过 announce 机制回传。
            可用 query_run(runId) 查询状态，cancel_run(runId) 取消。

            Args:
                agent_name: 目标子 Agent 名称
                instruction: 给子 Agent 的具体指令

            Returns:
                runId（UUID），可用于后续查询/取消
            """
            run_id = str(uuid.uuid4())
            task = asyncio.create_task(
                self._spawn_and_announce(run_id, agent_name, instruction)
            )
            self._spawn_runs[run_id] = task
            return f"已启动后台任务，runId={run_id}"

        return spawn_agent

    async def _spawn_and_announce(self, run_id: str, agent_name: str, instruction: str):
        """后台运行子 Agent，完成后将结果写入 spawn_runs/{runId}.json（announce）。"""
        run_record = {"run_id": run_id, "agent_name": agent_name, "status": "running",
                      "instruction": instruction, "started_at": datetime.utcnow().isoformat()}
        run_path = WORKING_DIR / "spawn_runs" / f"{run_id}.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps(run_record, ensure_ascii=False))

        agent_def = self._find_agent_def(agent_name)
        timeout = agent_def.run_timeout_seconds if agent_def else self._concurrency.run_timeout_seconds
        try:
            async with self._global_semaphore:
                result = await asyncio.wait_for(
                    self._run_sub_agent(agent_name, instruction),
                    timeout=timeout,
                )
            run_record.update({"status": "completed", "result": result,
                               "finished_at": datetime.utcnow().isoformat()})
        except asyncio.TimeoutError:
            run_record.update({"status": "timeout", "error": f"超时（{timeout}s）",
                               "finished_at": datetime.utcnow().isoformat()})
        except asyncio.CancelledError:
            run_record.update({"status": "cancelled", "finished_at": datetime.utcnow().isoformat()})
        except Exception as e:
            run_record.update({"status": "error", "error": str(e),
                               "finished_at": datetime.utcnow().isoformat()})
        finally:
            run_path.write_text(json.dumps(run_record, ensure_ascii=False))
            self._spawn_runs.pop(run_id, None)

    def _create_query_run_tool(self):
        """创建 query_run 工具：查询 spawn 任务状态/结果。"""

        async def query_run(run_id: str) -> str:
            """查询一个后台任务的状态和结果。
            Args:
                run_id: spawn_agent 返回的 runId
            Returns:
                任务的当前状态、结果或错误信息
            """
            run_path = WORKING_DIR / "spawn_runs" / f"{run_id}.json"
            if not run_path.exists():
                return f"未找到 runId={run_id} 的任务"
            return run_path.read_text(encoding="utf-8")

        return query_run

    def _create_cancel_run_tool(self):
        """创建 cancel_run 工具：取消正在运行的 spawn 任务。"""

        async def cancel_run(run_id: str) -> str:
            """取消一个后台运行中的任务。
            Args:
                run_id: spawn_agent 返回的 runId
            Returns:
                取消结果
            """
            task = self._spawn_runs.get(run_id)
            if task is None:
                return f"runId={run_id} 的任务不存在或已完成"
            task.cancel()
            return f"已请求取消 runId={run_id}"

        return cancel_run

    # ── 级联停止（v0.2）──────────────────────────────

    async def cancel_all_spawns(self):
        """级联停止所有正在运行的 spawn 任务（用于父任务取消时）。"""
        if self._concurrency.cascade_stop:
            for run_id, task in list(self._spawn_runs.items()):
                task.cancel()

    # ── 共用逻辑 ──────────────────────────────────────

    def _find_agent_def(self, agent_name: str) -> SubAgentDefinition | None:
        return next((d for d in self._sub_agent_defs if d.name == agent_name and d.enabled), None)

    async def _run_sub_agent(self, agent_name: str, instruction: str) -> str:
        """运行单个子 Agent（同步 / spawn 共用）。"""
        from .sub_agent_service import SubAgentService

        agent_def = self._find_agent_def(agent_name)
        if agent_def is None:
            raise ValueError(f"子 Agent '{agent_name}' 不存在或未启用")

        # 创建子 Agent 实例（使用其独立的 working_dir、tool_policy）
        sub_agent = SubAgentService.build_agent_instance(
            name=agent_name,
            max_iters=agent_def.max_iters,
            tool_policy=agent_def.tool_policy,  # v0.2: 应用工具权限策略
        )

        msg = Msg(name="orchestrator", content=instruction, role="user")
        response = await sub_agent.reply(msg)
        return response.get_text_content()

    def _format_results(self, results: list[dict]) -> str:
        """格式化子 Agent 执行结果。"""
        parts = []
        for r in results:
            name = r["agent_name"]
            if r["status"] == "success":
                parts.append(f"## {name} 执行结果\n{r['result']}")
            elif r["status"] == "timeout":
                parts.append(f"## {name} 执行超时\n{r['error']}")
            else:
                parts.append(f"## {name} 执行失败\n错误：{r['error']}")
        return "\n\n---\n\n".join(parts)

    # ── @agent 直连路由解析（v0.2）─────────────────────

    def resolve_direct_route(self, user_message: str) -> str | None:
        """
        检查用户消息是否包含 @agent_name 前缀或匹配 bindings。
        返回目标 agent_name 或 None（交由 Orchestrator 自主决策）。
        """
        # 1. @agent 语法
        if user_message.startswith("@"):
            parts = user_message.split(maxsplit=1)
            agent_name = parts[0][1:]  # 去掉 @
            if self._find_agent_def(agent_name):
                return agent_name
        # 2. bindings 匹配
        for binding in self._bindings:
            if re.search(binding.pattern, user_message):
                return binding.agent_name
        return None

    async def reply(self, msg=None, **kwargs):
        """处理用户消息：先检查直连路由，再转发给底层 CoPawAgent。"""
        if msg and hasattr(msg, 'content'):
            direct_target = self.resolve_direct_route(msg.content)
            if direct_target:
                # 直连路由：跳过 Orchestrator 决策，直接调用目标子 Agent
                instruction = msg.content.split(maxsplit=1)[1] if msg.content.startswith("@") else msg.content
                result = await self._run_sub_agent(direct_target, instruction)
                return Msg(name="orchestrator", content=result, role="assistant")
        return await self._agent.reply(msg=msg, **kwargs)

    @property
    def memory(self):
        return self._agent.memory

    # ... 其他必要的属性代理 ...
```

### 4.6 AgentRunner 改造

修改 `src/copaw/app/runner/runner.py` 的 `query_handler()`，根据配置决定创建普通 Agent 还是 Orchestrator：

```python
# runner.py 中 query_handler 的改造（伪代码）

async def query_handler(self, msgs, request=None, **kwargs):
    # ... 前置逻辑不变 ...

    config = load_config()
    ma = config.multi_agent

    if ma.enabled and ma.sub_agents:
        # 多 Agent 模式：创建 Orchestrator
        from ...agents.orchestrator import OrchestratorAgent

        agent = OrchestratorAgent(
            env_context=env_context,
            mcp_clients=mcp_clients,
            memory_manager=self.memory_manager,
            sub_agent_definitions=ma.sub_agents,
            concurrency_config=ma.concurrency,   # v0.2
            bindings=ma.bindings,                 # v0.2
            max_iters=ma.orchestrator_max_iters,
        )
    else:
        # 单 Agent 模式：创建普通 CoPawAgent（现有逻辑）
        agent = CoPawAgent(
            env_context=env_context,
            mcp_clients=mcp_clients,
            memory_manager=self.memory_manager,
            max_iters=max_iters,
            max_input_length=max_input_length,
            fallback_cfgs=fallback_cfgs,
        )

    # ... 后续逻辑不变 ...

    # v0.2: 在请求结束后，如果用户取消，级联停止 spawn 任务
    # if isinstance(agent, OrchestratorAgent) and request_cancelled:
    #     await agent.cancel_all_spawns()
```

### 4.7 子 Agent 的 Session 与可见性管理

子 Agent 不直接与用户交互，但其会话和记忆需要分层管理：

**长期记忆（跨请求持久化）：**
- 每个子 Agent 通过其独立的 `MEMORY.md` 和 `memory/` 目录持久化
- 记忆完全隔离，不与其他 Agent 共享

**会话记忆（按任务生命周期）：**
- `dispatch_to_agents`：会话随任务创建，完成后销毁
- `spawn_agent`：会话随 runId 关联，任务完成后可保留一段时间供查询
- 后续可扩展为子 Agent 独立持久化 session

**可见性范围（v0.2）：**
- 子 Agent 的执行过程对用户**不可见**（用户只看到 Orchestrator 的汇总回复）
- spawn 任务的状态可通过 API 查询（`GET /api/spawn-runs/{runId}`）
- Console 可展示子 Agent 的执行日志（可观测性，阶段四实现）

### 4.8 REST API 扩展

新增文件：`src/copaw/app/routers/sub_agents.py`

```
# 子 Agent 管理
GET    /api/sub-agents                 — 列出所有子 Agent
POST   /api/sub-agents                 — 创建子 Agent
GET    /api/sub-agents/{name}          — 获取子 Agent 详情
PUT    /api/sub-agents/{name}          — 更新子 Agent 配置
DELETE /api/sub-agents/{name}          — 删除子 Agent
GET    /api/sub-agents/{name}/files    — 列出子 Agent 的 md 文件
GET    /api/sub-agents/{name}/files/{filename}  — 读取子 Agent 的 md 文件
PUT    /api/sub-agents/{name}/files/{filename}  — 写入子 Agent 的 md 文件
GET    /api/sub-agents/{name}/skills   — 列出子 Agent 的技能
POST   /api/sub-agents/{name}/skills/{skill_name}/enable  — 启用技能
POST   /api/sub-agents/{name}/skills/{skill_name}/disable — 禁用技能

# v0.2: spawn 任务查询
GET    /api/spawn-runs                 — 列出所有 spawn 任务
GET    /api/spawn-runs/{runId}         — 查询单个 spawn 任务状态/结果
POST   /api/spawn-runs/{runId}/cancel  — 取消 spawn 任务
```

### 4.9 CLI 扩展

新增 CLI 命令组 `copaw agents`：

```bash
copaw agents list                       # 列出所有子 Agent
copaw agents create <name>              # 交互式创建子 Agent
copaw agents delete <name>              # 删除子 Agent
copaw agents config                     # 配置多 Agent 模式
copaw agents enable <name>              # 启用子 Agent
copaw agents disable <name>             # 禁用子 Agent
```

新增文件：`src/copaw/cli/agents_cmd.py`

---

## 5. 实现计划

### 阶段一：基础框架（约 1 周）

**目标：** 多 Agent 框架可运行，Orchestrator 可调度子 Agent

1. **扩展 Config schema**
   - 文件：`src/copaw/config/config.py`
   - 新增 `SubAgentDefinition`、`MultiAgentConfig` 模型
   - 在 `Config` 中新增 `multi_agent` 字段

2. **CoPawAgent 支持自定义 working_dir**
   - 文件：`src/copaw/agents/react_agent.py`
   - 新增 `working_dir` 和 `agent_name` 参数
   - 修改 `_build_sys_prompt()`、`_register_skills()`、`_register_hooks()` 使用 `self._working_dir`

3. **实现 SubAgentService**
   - 新增文件：`src/copaw/agents/sub_agent_service.py`
   - 实现 `create_sub_agent()`、`build_agent_instance()`、`list_sub_agents()` 等核心方法
   - 管理 `sub_agents_registry.json` 和子 Agent 工作目录

4. **实现 OrchestratorAgent**
   - 新增文件：`src/copaw/agents/orchestrator.py`
   - 实现 `dispatch_to_agents`（同步）+ `spawn_agent`（非阻塞）工具
   - 实现 `query_run` / `cancel_run` 工具
   - 实现并发安全阀、超时保护、级联停止
   - 实现 `@agent` / bindings 直连路由解析

5. **改造 AgentRunner**
   - 文件：`src/copaw/app/runner/runner.py`
   - 在 `query_handler()` 中根据 `config.multi_agent.enabled` 选择创建 Orchestrator 或普通 Agent

### 阶段二：管理接口（约 1 周）

**目标：** 用户可以通过 API 和 CLI 管理子 Agent

6. **REST API**
   - 新增文件：`src/copaw/app/routers/sub_agents.py`
   - 注册到 `src/copaw/app/routers/__init__.py`
   - 实现子 Agent CRUD、文件管理、技能管理接口

7. **CLI 命令**
   - 新增文件：`src/copaw/cli/agents_cmd.py`
   - 注册到 `src/copaw/cli/main.py`
   - 实现 `copaw agents list/create/delete/config` 等命令

8. **init 流程扩展**
   - 修改文件：`src/copaw/cli/init_cmd.py`
   - 在 `copaw init` 中新增可选步骤：是否启用多 Agent 模式、创建初始子 Agent

### 阶段三：记忆与技能隔离（约 0.5 周）

**目标：** 每个子 Agent 有独立的记忆和技能

9. **子 Agent MemoryManager 隔离**
   - 每个子 Agent 使用独立的 `MemoryManager(working_dir=sub_agent_dir)`
   - 需要在 `OrchestratorAgent._run_sub_agent()` 中正确初始化和清理

10. **子 Agent 技能隔离**
    - 每个子 Agent 的 `active_skills` 目录独立
    - `SubAgentService.create_sub_agent()` 时从全局技能中选择性复制

### 阶段四：Console 前端（约 0.5 周）

**目标：** Console 页面可管理子 Agent

11. **Console 前端页面**
    - 在 `console/src/pages/` 新增 Agent 管理页面
    - 支持：查看/创建/编辑/删除子 Agent
    - 支持：编辑子 Agent 的 SOUL.md / AGENTS.md / PROFILE.md
    - 支持：管理子 Agent 的技能

### 阶段五：优化与测试（约 0.5 周）

12. **Orchestrator system prompt 优化**
    - 调优 Orchestrator 的调度策略 prompt
    - 测试不同场景下的调度准确性

13. **子 Agent session 持久化（可选）**
    - 如需子 Agent 跨请求保留上下文，为其实现独立 session

14. **错误处理与降级**
    - 子 Agent 执行超时处理
    - 子 Agent 执行失败时 Orchestrator 的降级策略
    - 多 Agent 模式不可用时自动降级为单 Agent 模式

15. **测试**
    - 单元测试：SubAgentService、OrchestratorAgent
    - 集成测试：端到端多 Agent 协同流程

---

## 6. 关键设计决策

### 6.1 Orchestrator 如何调度子 Agent？

通过 **tool_use 机制**。Orchestrator 的 toolkit 中注册多个调度工具，LLM 通过 ReAct 循环自行决定何时调用、传递什么指令。

**优点：**
- 与现有 ReAct 框架完全兼容
- Orchestrator 可以自行判断是否需要调度（简单问题直接回答）
- 支持多轮调度（先调度 A，根据结果再调度 B）
- 无需预定义固定的路由规则（但可通过 bindings 补充确定性路由）

**v0.2 Orchestrator 工具集：**

| 工具 | 模式 | 说明 |
|------|------|------|
| `dispatch_to_agents` | 同步 | 并行调度多个子 Agent，等待全部完成 |
| `spawn_agent` | 异步 | 非阻塞启动单个子 Agent，立即返回 runId |
| `query_run` | 查询 | 查询 spawn 任务状态和结果 |
| `cancel_run` | 控制 | 取消正在运行的 spawn 任务 |

**dispatch_to_agents 工具的输入 schema：**
```json
{
  "tasks": [
    {
      "agent_name": "code-assistant",
      "instruction": "请阅读 src/main.py 并解释其主要功能"
    },
    {
      "agent_name": "writer",
      "instruction": "请将以下技术文档改写为面向非技术人员的说明..."
    }
  ]
}
```

**spawn_agent 工具的输入 schema（v0.2 新增）：**
```json
{
  "agent_name": "researcher",
  "instruction": "帮我搜索近一周关于 AI Agent 框架的最新动态"
}
```
返回：`"已启动后台任务，runId=abc-123"`

### 6.2 子 Agent 的 LLM 可以不同吗？

可以。`SubAgentDefinition.model_override` 字段允许为子 Agent 指定不同的 LLM。例如：
- Orchestrator 使用高性能模型（qwen3-max）做决策
- 代码助手使用代码专用模型
- 简单任务的 Agent 使用轻量模型降低成本

`SubAgentService.build_agent_instance()` 中根据 `model_override` 调用 `create_model_and_formatter()` 时传入不同配置。

### 6.3 子 Agent 之间可以通信吗？

第一阶段不支持。所有通信通过 Orchestrator 中转：
```
Agent A 结果 → Orchestrator → 将 A 的结果作为指令一部分发给 Agent B
```

如后续需要，可扩展 `agent_to_agent_message` 工具。

### 6.4 子 Agent 的安全边界（v0.2 增强）

**工具权限策略（ToolPolicy）：**
- 每个子 Agent 可配置独立的 `tool_policy`（allow/deny 模式）
- 建议默认采用 allow 白名单模式，显式列出允许的工具
- 例如：写作助手只允许 `file_reader`、`docx`、`pdf`，禁止 `shell`
- `SubAgentService.build_agent_instance()` 根据 `tool_policy` 过滤 toolkit

**工作目录隔离：**
- 每个子 Agent 的身份文件、记忆、技能目录完全隔离
- 子 Agent 无法读取其他 Agent 的工作目录

**并发安全：**
- 全局信号量限制总并发数，防止 token 爆炸
- 单次 fan-out 上限限制每次 dispatch/spawn 的 Agent 数
- 超时机制确保子 Agent 不会无限运行
- 级联停止确保父任务取消时子 Agent 也停止

---

## 7. 风险与对策

**1. Orchestrator 调度不准确**
- 风险：LLM 可能选错子 Agent 或给出不明确的指令
- 对策：优化 system prompt 中的 Agent 描述；增加调度示例；允许用户直接指定 Agent（`@code-assistant 帮我看下这个 bug`）

**2. 子 Agent 执行时间过长**
- 风险：并行任务中某个子 Agent 超时拖慢整体
- 对策：v0.2 已实现 `run_timeout_seconds` 超时保护；对耗时任务使用 `spawn_agent` 非阻塞模式，不会阻塞 Orchestrator

**3. Token 消耗增加**
- 风险：Orchestrator + 子 Agent 双重推理消耗更多 token
- 对策：简单问题 Orchestrator 直接回答不调度；子 Agent 使用更低成本的模型

**4. 子 Agent 记忆膨胀**
- 风险：每个子 Agent 的 MEMORY.md 和 memory/ 无限增长
- 对策：复用现有的 memory compaction 机制；可选配置定期清理

---

## 8. 分布式扩展预留设计

### 8.1 背景

当前方案（v0.2）采用**单进程内多 Agent** 架构，所有 Agent 在同一 Python 进程中通过 `asyncio` 并发执行。
这对个人助手场景足够，但未来可能需要扩展到以下场景：

- **企业/团队部署**：多人共享 Agent 集群，按需弹性扩缩
- **异构硬件**：代码 Agent 跑在 GPU 机器，文本 Agent 跑在轻量实例
- **跨机器部署**：单机资源不足时，将 Agent 分布到多台机器
- **故障隔离**：某个 Agent 崩溃不影响其他 Agent 和 Orchestrator

为避免将来迁移时大规模重构，v0.2 在实现中预留 **AgentTransport 抽象层**。

### 8.2 AgentTransport 抽象

Orchestrator 与子 Agent 之间的通信统一通过 `AgentTransport` 接口，不直接依赖进程内调用：

```python
# src/copaw/agents/transport.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class RunResult:
    run_id: str
    agent_name: str
    status: RunStatus
    result: str = ""
    error: str = ""


class AgentTransport(ABC):
    """Agent 间通信的抽象层。

    Orchestrator 通过此接口与子 Agent 通信，
    具体实现可以是进程内调用、HTTP、WebSocket、消息队列等。
    """

    @abstractmethod
    async def send(
        self, agent_name: str, instruction: str, timeout: int = 300
    ) -> RunResult:
        """同步发送指令并等待子 Agent 完成。"""
        ...

    @abstractmethod
    async def spawn(
        self, agent_name: str, instruction: str
    ) -> str:
        """非阻塞发送指令，立即返回 run_id。"""
        ...

    @abstractmethod
    async def query(self, run_id: str) -> RunResult:
        """查询 spawn 任务的状态和结果。"""
        ...

    @abstractmethod
    async def cancel(self, run_id: str) -> bool:
        """取消一个正在运行的 spawn 任务。"""
        ...

    @abstractmethod
    async def list_agents(self) -> list[dict]:
        """列出当前可用的子 Agent。"""
        ...
```

### 8.3 当前实现：LocalTransport

v0.2 阶段只实现 `LocalTransport`，即进程内直接调用子 Agent：

```python
# src/copaw/agents/transport.py

class LocalTransport(AgentTransport):
    """进程内 Agent 通信（v0.2 默认实现）。

    直接在当前进程中创建 CoPawAgent 实例并调用 reply()，
    通过 asyncio.Semaphore 控制并发，asyncio.wait_for 控制超时。
    """

    def __init__(
        self,
        sub_agent_defs: list[SubAgentDefinition],
        concurrency: ConcurrencyConfig,
    ):
        self._sub_agent_defs = sub_agent_defs
        self._concurrency = concurrency
        self._semaphore = asyncio.Semaphore(
            concurrency.max_global_concurrent
        )
        self._spawn_runs: dict[str, asyncio.Task] = {}

    async def send(self, agent_name, instruction, timeout=300):
        async with self._semaphore:
            result = await asyncio.wait_for(
                self._run_local(agent_name, instruction),
                timeout=timeout,
            )
            return RunResult(
                run_id="", agent_name=agent_name,
                status=RunStatus.COMPLETED, result=result,
            )

    async def _run_local(self, agent_name, instruction):
        from .sub_agent_service import SubAgentService
        agent_def = self._find_def(agent_name)
        sub_agent = SubAgentService.build_agent_instance(
            name=agent_name,
            max_iters=agent_def.max_iters,
            tool_policy=agent_def.tool_policy,
        )
        msg = Msg(name="orchestrator", content=instruction, role="user")
        response = await sub_agent.reply(msg)
        return response.get_text_content()

    # spawn / query / cancel 实现同 4.5 节，此处省略
```

### 8.4 未来实现：RemoteTransport（路线图）

当需要分布式部署时，实现 `RemoteTransport`，Orchestrator 的调度逻辑**无需任何修改**：

```
┌─────────────────┐       HTTP/WS/MQ        ┌──────────────────┐
│  Orchestrator    │ ◄──── RemoteTransport ──►│  Agent 实例 (远程) │
│  (主节点)         │                          │  独立进程/容器      │
└─────────────────┘                          └──────────────────┘
```

`RemoteTransport` 需要解决的问题（届时再设计）：

- **服务注册与发现**：Agent 实例启动时向注册中心注册，Orchestrator 动态发现可用 Agent
- **通信协议**：HTTP REST（简单）/ WebSocket（双向）/ 消息队列（解耦），推荐先用 HTTP
- **Agent 实例管理**：每个 Agent 实例暴露统一的 `/chat` 端点接收指令并返回结果
- **心跳与健康检查**：检测 Agent 实例存活状态
- **负载均衡**：同类 Agent 多实例时的请求分配
- **序列化协议**：指令和结果的序列化格式（JSON 即可）

初步设想的分布式架构：

```
                    ┌─────────────────────┐
                    │   Registry Service   │  (Agent 注册表)
                    └──────────┬──────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │                     │                     │
  ┌──────▼───────┐     ┌──────▼───────┐     ┌──────▼───────┐
  │ Machine A     │     │ Machine B     │     │ Machine C     │
  │ Orchestrator  │     │ code-agent    │     │ writer-agent  │
  │ + Console     │     │ (GPU)         │     │ researcher    │
  └──────────────┘     └──────────────┘     └──────────────┘
         │                     │                     │
         └─────── 统一消息协议（类聊天） ─────────────┘
```

### 8.5 对当前实现的要求

为确保预留有效，v0.2 实现阶段需遵守以下约束：

1. **OrchestratorAgent 中所有子 Agent 调用必须经过 `AgentTransport`**，禁止直接 import 并调用 `SubAgentService.build_agent_instance()`
2. **`transport.py` 中定义的 `RunResult` / `RunStatus` 作为统一返回类型**，Orchestrator 的调度工具只依赖这些类型
3. **Transport 实例通过 OrchestratorAgent 构造函数注入**，不在内部硬编码
4. **spawn 任务的状态持久化格式（`spawn_runs/{runId}.json`）与 `RunResult` 字段对齐**，方便未来 RemoteTransport 直接复用

### 8.6 迁移路径

```
v0.2  单进程 LocalTransport（当前）
  │
  ▼  仅需新增 RemoteTransport 实现 + Agent 实例启动脚本
v0.3  单机多进程（同一台机器上多个 Agent 进程，通过 localhost HTTP 通信）
  │
  ▼  新增 Registry Service
v0.4  多机分布式（Agent 分布在不同机器，通过网络通信）
```

每一步只需新增 Transport 实现，Orchestrator 侧零改动。

---

## 9. 后续扩展方向

- **Agent 市场**：预定义一批子 Agent 模板（代码助手、写作助手、数据分析师等），用户一键安装
- **Agent 间直接通信**：支持子 Agent 之间的消息传递（P2P 模式）
- **Agent 独立 session**：子 Agent 可以维护自己的对话历史，实现更复杂的多轮协同
- **Agent 可观测性**：在 Console 中展示 Orchestrator 的调度决策过程和子 Agent 的执行详情
- ~~**用户直接与子 Agent 对话**：支持 `@agent_name` 语法~~（v0.2 已实现）
- **动态 Agent 创建**：Orchestrator 可以在运行时创建临时子 Agent 处理特定任务
- **spawn 任务管理 UI**：在 Console 中展示 spawn 任务的实时状态、结果、取消操作
- **分布式多实例部署**：通过 RemoteTransport 实现跨机器 Agent 集群（详见第 8 节）

---

## 附录 A：需要修改的现有文件清单

- `src/copaw/config/config.py` — 扩展：新增 `ToolPolicy`、`AgentBinding`、`ConcurrencyConfig`、`SubAgentDefinition`、`MultiAgentConfig`
- `src/copaw/constant.py` — 扩展：新增 `SUB_AGENTS_DIR`、`SUB_AGENTS_REGISTRY`、`SPAWN_RUNS_DIR` 常量
- `src/copaw/agents/react_agent.py` — 修改：新增 `working_dir`、`agent_name`、`tool_policy` 参数
- `src/copaw/agents/prompt.py` — 修改（可选）：`build_system_prompt_from_working_dir()` 接受 `working_dir` 参数
- `src/copaw/app/runner/runner.py` — 修改：`query_handler()` 增加 Orchestrator 分支，传入 `concurrency_config` 和 `bindings`
- `src/copaw/app/routers/__init__.py` — 扩展：注册 sub_agents router
- `src/copaw/cli/main.py` — 扩展：注册 agents 命令组
- `src/copaw/agents/__init__.py` — 扩展：导出新增类

## 附录 B：需要新增的文件清单

- `src/copaw/agents/transport.py` — AgentTransport 抽象层 + RunResult/RunStatus + LocalTransport 实现（详见第 8 节）
- `src/copaw/agents/orchestrator.py` — Orchestrator Agent 实现（dispatch + spawn + 路由，通过 AgentTransport 调用子 Agent）
- `src/copaw/agents/sub_agent_service.py` — 子 Agent 管理服务
- `src/copaw/app/routers/sub_agents.py` — 子 Agent REST API + spawn 任务查询 API
- `src/copaw/cli/agents_cmd.py` — 子 Agent CLI 命令

## 附录 C：config.json 完整示例

```json
{
  "channels": { "console": { "enabled": true } },
  "agents": {
    "language": "zh",
    "running": { "max_iters": 50, "max_input_length": 131072 }
  },
  "multi_agent": {
    "enabled": true,
    "orchestrator_max_iters": 20,
    "max_parallel": 3,
    "concurrency": {
      "max_global_concurrent": 5,
      "max_children_per_dispatch": 3,
      "run_timeout_seconds": 300,
      "cascade_stop": true
    },
    "bindings": [
      { "pattern": "^帮我写", "agent_name": "writer" },
      { "pattern": "^搜索", "agent_name": "researcher" }
    ],
    "sub_agents": [
      {
        "name": "code-assistant",
        "display_name": "代码助手",
        "description": "擅长代码阅读、编写、调试、重构、测试。熟悉 Python、Java、C#、TypeScript 等语言。",
        "enabled": true,
        "max_iters": 40,
        "skills": ["file_reader"],
        "tool_policy": { "mode": "deny", "tools": [] },
        "run_timeout_seconds": 600
      },
      {
        "name": "writer",
        "display_name": "写作助手",
        "description": "擅长撰写文档、报告、邮件、技术文档、公众号文章。",
        "enabled": true,
        "max_iters": 30,
        "skills": ["docx", "pdf"],
        "tool_policy": { "mode": "allow", "tools": ["file_reader", "docx", "pdf"] },
        "run_timeout_seconds": 300
      },
      {
        "name": "researcher",
        "display_name": "研究助手",
        "description": "擅长信息搜索、数据分析、市场调研、文献检索。",
        "enabled": true,
        "max_iters": 30,
        "skills": ["browser_visible", "news"],
        "tool_policy": { "mode": "deny", "tools": ["shell"] },
      "run_timeout_seconds": 300
      }
    ]
  }
}
```

## 附录 D：OpenClaw 对比参考

> 参考来源：`docs.openclaw.ai` 的 multi-agent、session-tool、subagents 文档

**OpenClaw 的两层 Agent 设计：**
1. **agents.list[]** — 多人格 Agent，每个 agentId 有独立的 workspace/agentDir/sessions，通过 bindings 做确定性路由
2. **sessions_spawn** — 轻量 worker subagent，非阻塞派发，通过 announce 回传结果

**CoPaw 的差异化决策：**
- CoPaw 的子 Agent 更像 OpenClaw 的 agents.list[]（每个都有独立灵魂/个性），而非轻量 worker
- CoPaw 借鉴了 OpenClaw 的 spawn + announce 模式用于长时任务
- CoPaw 借鉴了 OpenClaw 的并发安全阀、工具 allow/deny、bindings 路由等机制
- CoPaw 保持文件驱动（Markdown 身份文件）的特色，不采用 OpenClaw 的 YAML/JSON 定义方式

## 附录 E：v0.1 → v0.2 变更摘要

| 类别 | v0.1 | v0.2 |
|------|------|------|
| 调度工具 | `dispatch_to_agents`（同步） | + `spawn_agent`（异步）+ `query_run` + `cancel_run` |
| 路由 | Orchestrator 自主决策 | + `@agent` 直连 + `bindings` 确定性路由 |
| 并发 | `asyncio.Semaphore(max_parallel)` | + 全局信号量 + fan-out 上限 + 超时 + 级联停止 |
| 安全 | 共享工具权限 | + `ToolPolicy` allow/deny 模式 |
| Session | 子 Agent 无独立 session | + spawn runId 状态跟踪 + 可见性范围 |
| 配置 | `SubAgentDefinition` + `MultiAgentConfig` | + `ToolPolicy` + `ConcurrencyConfig` + `AgentBinding` |
