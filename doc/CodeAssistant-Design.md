# CoPaw 代码开发自主智能体设计规划文档

版本：v0.1  
日期：2026-03-01  
作者：zhaorun（基于当前 CoPaw 架构整理）

---

## 1. 背景与目标
目标是在 CoPaw 基础上扩展“个人代码助手”，满足：
- **跨平台**（Windows/macOS/Linux）  
- **本地推理优先**（可选云端）  
- **CLI + Web 控制台**  
- **自动改代码 / 运行测试**（大部分时间无需人工干预）  
- **多语言仓库支持**（Unity + Java 起步，未来可扩展）  
- **Skills 扩展机制**（避免绑定语言/框架）

最终产物不仅能个人使用，还应满足**公司内推广**的可配置与可治理要求。

---

## 2. 现状与约束（基于仓库代码）
### 2.1 Skills 机制
- Skills 从 `active_skills` 目录加载，`customized_skills` 用于自定义覆盖。  
  见：`src/copaw/agents/skills_manager.py`、`website/public/docs/skills.zh.md`
- 一个 Skill 的最小单元是包含 `SKILL.md` 的目录。  
  `SKILL.md` 需要 YAML front matter（`name`, `description`）才能通过校验。

### 2.2 工具注册
CoPaw Agent 默认工具注册在 `src/copaw/agents/react_agent.py` 的 `_create_toolkit()`：
- 文件读写：`read_file / write_file / edit_file`
- Shell 执行：`execute_shell_command`
- 搜索：`grep_search / glob_search`
- 浏览器：`browser_use`

**结论**：CoPaw 可作为“技能+工具”架构的宿主，代码助手能力需通过**新增工具 + Skill 工作流设计**实现。

---

## 3. 需求拆解
### 3.1 功能需求
1. **代码理解与修改**
   - 文件读写、结构化修改、差异输出
   - 代码搜索、依赖/引用定位（LSP）
2. **执行与验证**
   - 自动运行测试 / 构建 / 格式化 / lint
   - 失败自动回滚
3. **多语言支持**
   - Unity（C#）与 Java 服务器优先
   - 可扩展到 TS/Go/Python 等
4. **自动化任务流**
   - 需求 → 方案 → 修改 → 测试 → 报告
5. **可配置与治理**
   - 每个仓库单独配置
   - 可审计、可追溯、可禁用危险动作

### 3.2 非功能需求
- 本地模型运行（优先）
- 跨平台（Windows/macOS/Linux）
- 性能与稳定性（长任务可靠运行）
- 安全控制（命令白名单、路径限制、审批策略）

---

## 4. 总体架构
```
[CLI] ----> [Agent Daemon / CoPaw Runtime] ----> [Agent Orchestrator]
   |                    |                              |
   |                    |                              +--> [Skills Loader]
   |                    |                              +--> [Tools: file/patch/test/git/LSP]
   |                    |                              +--> [Memory + Logs]
   |
[Web Console] <----实时日志/状态----|
```

关键原则：
- **Skills = 行为规范（workflow）**
- **Tools = 可执行能力（execution）**

---

## 5. 核心模块设计

### 5.1 Skills 层（工作流）
建议至少提供 3 类 Skills（可组合）：
1. **code_assistant**（主工作流）
   - 需求理解 → 方案 → 修改 → 测试 → 总结
   - 若测试失败，自动修复或回滚
2. **unity_build**（Unity 特定）
   - 基于 `-batchmode` 构建/测试
3. **java_server**（Java 服务特定）
   - 统一封装 `gradle test` / `mvn test`

Skill 只定义“行为准则”，不直接执行业务逻辑，执行由工具层完成。

### 5.2 工具层（新增/增强）
建议新增以下工具（`src/copaw/agents/tools/`）：
1. **git_ops**  
   - `git_status` / `git_diff` / `git_apply_patch`
2. **patch_ops**  
   - 生成/应用 diff、失败回滚
3. **test_runner**  
   - 统一封装测试/构建命令（超时/失败分析）
4. **lsp_client**  
   - 多语言 LSP 查询（定义、引用、诊断）
5. **repo_context**  
   - 仓库根目录探测、项目结构摘要

### 5.3 安全与治理
1. **命令白名单/黑名单**（配置驱动）  
2. **路径限制**（仅允许仓库内修改）  
3. **审批策略**（危险操作需确认）  
4. **审计日志**（diff + 执行命令记录）

### 5.4 配置体系（建议）
新增仓库级配置文件：`copaw.project.json`  
字段示例：
```json
{
  "repo_root": ".",
  "languages": ["csharp", "java"],
  "test_commands": ["dotnet test", "gradle test"],
  "build_commands": ["dotnet build"],
  "lsp": {
    "csharp": {"server": "omnisharp", "args": []},
    "java": {"server": "jdtls", "args": []}
  },
  "policy": {
    "auto_apply": true,
    "auto_test": true,
    "require_approval_for": ["delete", "network", "git push"]
  }
}
```

---

## 6. MVP 范围
### 必做（第一个可用版本）
- code_assistant Skill
- 文件读写 + 搜索 + `git diff`
- 测试/构建统一执行（可配置命令）
- 自动总结与变更报告

### 暂缓
- LSP 深度语义（后续）
- 审批与回滚策略（先简单化）

---

## 7. 里程碑
1. **M1（2~3 周）**  
   - MVP 流程跑通  
   - Unity/Java 基本测试支持
2. **M2（4~6 周）**  
   - LSP 语义能力  
   - 审计与回滚  
3. **M3（8~12 周）**  
   - 多团队配置化  
   - 公司级推广模板

---

## 8. 风险与对策
| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 自动改代码引入错误 | 质量风险 | 强制跑测试 + 回滚 |
| 多语言适配成本高 | 维护成本 | LSP 统一接口 |
| 命令执行不安全 | 安全风险 | 白名单 + 审批 |
| 模型输出不稳定 | 可靠性 | 结果校验 + 重试 |

---

## 9. 下一步建议
1. 先落地 MVP（可在你本地仓库跑通）  
2. 再引入 LSP 与更严格的审批策略  
3. 为公司推广准备配置模板与治理方案  

---

**备注**：本文仅作为设计规划，具体实现可按里程碑逐步落地。  
