# CoPaw 日志参考

## 日志文件一览

| 日志 | 路径 | 写入时机 |
|------|------|----------|
| 应用日志 | `~/logs/YYYY-MM-DD_HH-MM-SS.log` | `copaw app` 启动时（`--log-dir ~/logs` 默认开启） |
| LLM 会话日志 | `~/.copaw/llm_sessions/<session_id>.log` | 每次 LLM 调用（`LoggingChatModelProxy`） |
| 模型调试日志 | `~/.copaw/debug_model.log` | 每次 query_handler 请求（RotatingFileHandler 5MB×2） |
| 错误转储 | `%TEMP%/copaw_query_error_*.json` | query_handler 捕获到未处理异常时 |
| 会话状态 | `~/.copaw/sessions/<user_id>_<channel>--<session_id>.json` | 每次对话结束后保存 |

---

## 1. 应用日志 `~/logs/`

格式：`时间戳 | LEVEL | 文件:行 | 函数 | 消息`  
代码：`src/copaw/utils/logging.py` → `setup_logger()`，`src/copaw/cli/app_cmd.py`

```powershell
# 找最新日志并查看末尾
$f = Get-ChildItem ~/logs/*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content $f.FullName -Tail 100

# 实时跟踪
Get-Content $f.FullName -Wait -Tail 50

# 只看错误
Get-Content $f.FullName | Select-String "ERROR|CRITICAL"
```

---

## 2. LLM 会话日志 `~/.copaw/llm_sessions/`

每个 `session_id` 一个文件，记录送给模型的完整消息列表 + 响应内容。  
代码：`src/copaw/utils/llm_logger.py`，由 `LoggingChatModelProxy` 调用（`src/copaw/agents/model_factory.py`）

格式（每次调用之间用 `====` 分隔）：
```
================================================================================
▶ REQUEST  model=gpt-5.2  tools=8
================================================================================
[system]
...
[user]
你好

================================================================================
◀ RESPONSE  model=gpt-5.2  in=1234 out=56  t=2.3s
================================================================================
你好！...
```

```powershell
# 列出所有会话（最新在前）
Get-ChildItem ~/.copaw/llm_sessions/*.log | Sort-Object LastWriteTime -Descending

# 读某个会话（session_id 从 URL 或应用日志里获取）
Get-Content ~/.copaw/llm_sessions/1772366091136.log

# 搜索所有会话里的关键词
Select-String "auth_unavailable|InternalServerError" ~/.copaw/llm_sessions/*.log
```

> **Windows 已知问题**：QQ 渠道的 `session_id` 格式为 `qq:XXXXXXXX`，冒号在 Windows
> 路径中非法，文件创建失败，留下 0 字节的 `~/.copaw/llm_sessions/qq` 文件，不影响功能。
> 追查 QQ 会话的 LLM 记录时改用 `debug_model.log` 或 `error dump`。

---

## 3. 模型调试日志 `~/.copaw/debug_model.log`

RotatingFileHandler（5 MB × 2 备份）。代码：`src/copaw/app/runner/runner.py`

关键标记：
- `=== QUERY START session=xxx ===` — 请求开始
- `CP1:stream_final session=xxx` — 流式响应最后一个 chunk（**成功才出现**）
- `CP2:pre_save session=xxx` — 保存 session 前的 memory 快照
- `LLM call: model=... base_url=... api_key_prefix=... api_key_len=...` — 实际调用参数（排查 key/url）
- `block[N] type=text fffd=N preview=...` — 消息字节级编码信息（排查乱码）

```powershell
# 查看最后 100 行
Get-Content ~/.copaw/debug_model.log -Tail 100

# 追踪某个 session 的完整轨迹
Select-String "qq:E6B0C533" ~/.copaw/debug_model.log

# 查所有 LLM 调用参数（确认 api_key 和 base_url）
Select-String "LLM call:" ~/.copaw/debug_model.log
```

若某个 session 有 `QUERY START` 但没有 `CP1`，说明该请求期间所有 LLM 调用均失败。

---

## 4. 错误转储 `%TEMP%/copaw_query_error_*.json`

`query_handler` 抛出未处理异常时生成。代码：`src/copaw/app/runner/query_error_dump.py`

字段：
- `trace` — 完整 Python traceback
- `exception_type` / `exception_message`
- `request` — 原始请求（channel、session_id、用户消息、channel_meta）
- `agent_state` — 异常时刻的 memory.content + toolkit.active_groups 快照

```powershell
# 列出所有 dump（最新在前）
Get-ChildItem $env:TEMP/copaw_query_error_*.json | Sort-Object LastWriteTime -Descending

# 读最新的 traceback
$f = Get-ChildItem $env:TEMP/copaw_query_error_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1
(Get-Content $f.FullName | ConvertFrom-Json).trace

# 看 exception_message
(Get-Content $f.FullName | ConvertFrom-Json).exception_message
```

---

## 5. 会话状态 `~/.copaw/sessions/`

文件名：`<user_id>_<channel>--<session_id>.json`（或 `<channel>_<session_id>.json`）  
不是日志，但排查时经常需要：

- `agent.memory.content` — 所有历史消息（含失败的用户消息）
- `agent.toolkit.active_groups` — 已激活工具组（`[]` 表示只有 basic 内置工具）
- `agent._sys_prompt` — 上次保存的系统提示词

```powershell
# 列出所有 session 文件
Get-ChildItem ~/.copaw/sessions/*.json | Sort-Object LastWriteTime -Descending | Select-Object Name, Length, LastWriteTime
```

---

## 排查速查

| 症状 | 先查 | 重点 |
|------|------|------|
| LLM 调用失败（500/auth_unavailable） | `error dump` → `debug_model.log` | `LLM call:` 行确认 key/url；若参数正确则是 API 临时故障，重试 |
| 某 session 无 AI 回复 | `debug_model.log` | 搜 session_id，有 `QUERY START` 无 `CP1` → LLM 全失败 |
| 消息乱码 | `debug_model.log` | `fffd=N` 字段，N>0 表示有 `\ufffd` 替换字符 |
| 请求从未到达 agent | 应用日志 | 搜 `Handle agent query` |
| 想看完整的 prompt | LLM 会话日志 | `▶ REQUEST` 段 |
