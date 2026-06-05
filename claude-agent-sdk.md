# Claude Agent SDK 编排 Agent 说明

## Agent 简介

这个 Agent 是给前端 ActionDesign 编排工具使用的 Claude 代理服务。前端把用户需求、页面上下文、可用编排元素和历史对话拼成 prompt，后端调用 `claude-agent-sdk`，再把模型输出转换成前端可执行的工具调用。

当前后端项目是 `claude-agent-sdk-python`。核心入口在 `examples/http_proxy_server.py`，提供非流式接口、SSE 流式接口、工具调用解析、调试日志和工具结果接收。

## 主要能力

- 接收前端 prompt，并调用 Claude SDK 的 `query()`。
- 支持普通文本输出和 `[TOOL_CALL] tool_name({...})` 工具协议。
- 支持从 JSON actions 结构中恢复工具调用。
- 支持流式 SSE 输出，同时隐藏内部 `[TOOL_CALL]` 协议，避免展示给用户。
- 记录调试日志，包括 prompt 头部、prompt 尾部、用户消息列表、第一条用户消息和最新用户消息。
- 接收前端工具执行结果并缓存，但不主动把 tool result 回灌给 Claude SDK。

## 后端实现

### 请求模型

`ClaudeProxyRequest` 描述前端发给后端的请求，包含 prompt、模型、工具白名单、图片、最大轮数等字段。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:429`

### 非流式接口

`/api/ai/claude` 使用 `query()` 收集完整 Claude 响应。它会遍历 `AssistantMessage`，把 `TextBlock` 拼成内容，把允许的 `ToolUseBlock` 转成 `[TOOL_CALL]` 文本和 `tool_calls` 结构。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:861`
- `claude-agent-sdk-python/examples/http_proxy_server.py:891`
- `claude-agent-sdk-python/examples/http_proxy_server.py:954`

### SSE 流式接口

`/api/ai/claude/stream` 使用 `include_partial_messages=True`。`StreamEvent.content_block_delta/text_delta` 只用于实时推 UI 和检测 `[TOOL_CALL]`，最终 `message_complete.content` 优先使用最终 `AssistantMessage`。如果没有最终 `AssistantMessage`，才 fallback 到 partial text。

这样可以避免 partial delta 和最终 AssistantMessage 被重复拼进最终内容。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:1042`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1067`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1092`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1135`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1193`

### 调试日志

`_build_prompt_log_fields()` 统一生成非流式和 SSE 的 prompt 日志字段：

- `prompt_preview`: prompt 前 200 字。
- `prompt_tail_preview`: prompt 后 2000 字。
- `user_messages`: 从 prompt 中提取的所有 `User:` 段落。
- `initial_user_message`: 第一条用户消息。
- `latest_user_message`: 最新用户消息。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:112`
- `claude-agent-sdk-python/examples/http_proxy_server.py:995`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1248`

## 工具协议

### 工具白名单

默认只允许前端注册的设计工具进入 `tool_calls`，避免把 Claude Code 内置工具暴露给前端。

当前白名单：

```python
DESIGN_TOOLS = {'create_node', 'insert_node', 'create_action', 'list_elements', 'preview_code'}
```

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:54`

### 文本工具调用

模型可以输出：

```text
[TOOL_CALL] create_node({"elementKey":"ExitAction","targetAction":"main"})
```

后端通过 `extract_tool_calls()` 解析，生成：

```json
{
  "name": "create_node",
  "arguments": {
    "elementKey": "ExitAction",
    "targetAction": "main"
  }
}
```

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:592`
- `claude-agent-sdk-python/examples/http_proxy_server.py:604`

### JSON actions 恢复

如果模型输出 JSON actions，后端会用 `_extract_actions_from_json()` 和 `_convert_json_node_to_tool_call()` 转成前端工具调用。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:579`
- `claude-agent-sdk-python/examples/http_proxy_server.py:680`

### 工具结果接收

`/api/ai/claude/tool-result` 只负责接收、记录、去重和缓存前端工具执行结果。当前设计不把工具结果主动喂回 Claude SDK。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:1311`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1323`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1357`

## 工具调用和工具结果日志记录

### 日志文件位置

后端调试日志写入当前运行目录下的 `debug_logs/`。如果请求带了 `X-Conversation-Id`，文件名是 `{conversation_id}.json`；否则使用 `unknown_{timestamp}.json`。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:51`
- `claude-agent-sdk-python/examples/http_proxy_server.py:131`
- `claude-agent-sdk-python/examples/http_proxy_server.py:140`

### 工具调用日志

非流式和 SSE 都会把工具调用写入同一份会话日志。主要字段包括：

- `turns`: 每轮 AssistantMessage 的块级记录，包括 `TextBlock`、`ToolUseBlock` 和 `ToolResultBlock` 摘要。
- `raw_content`: 后端用于解析工具调用的完整文本内容。
- `tool_calls`: 已归一化、准备返回给前端执行的工具调用列表。
- `clean_content`: 移除内部 `[TOOL_CALL]` 后展示给前端的文本。
- `result`: 调用耗时、费用、消息数、轮次、是否中断等结果信息。

非流式日志写入点：

- `claude-agent-sdk-python/examples/http_proxy_server.py:909`
- `claude-agent-sdk-python/examples/http_proxy_server.py:926`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1003`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1005`

SSE 日志写入点：

- `claude-agent-sdk-python/examples/http_proxy_server.py:1153`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1171`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1256`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1258`

### 工具结果日志

前端执行工具后调用 `/api/ai/claude/tool-result`。后端会记录一条 `type: "tool_result"` 的日志，字段包括：

- `run_id`
- `tool_call_id`
- `tool_name`
- `arguments`
- `status`
- `result`
- `error`
- `is_error`

同时，工具结果会进入 `_tool_result_store` 内存缓存，key 是 `(conversation_id, run_id, tool_call_id)`。这个缓存用于去重和调试查询，不是长期持久化数据库。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:486`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1311`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1335`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1342`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1355`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1361`

## 编排内容生成流程

1. 前端收集用户需求、页面结构、可用元素、已有动作和对话历史。
2. 前端把这些信息拼成 Agent prompt，发给后端 `/api/ai/claude` 或 `/api/ai/claude/stream`。
3. 后端调用 `claude-agent-sdk` 的 `query()`。
4. Claude 输出自然语言说明、原生 `ToolUseBlock`，或文本 `[TOOL_CALL]`。
5. 后端把允许的工具调用归一化为 `tool_calls`。
6. 前端读取 `tool_calls` 并执行具体编排工具，例如创建动作、创建节点、插入节点、预览代码。
7. 前端把工具执行结果发到 `/api/ai/claude/tool-result`，后端记录结果供调试和排查。

## 本次后端修复点

### SSE 最终内容去重

旧逻辑会把 partial delta 和最终 AssistantMessage 都追加到 `full_text`，导致最终 `message_complete.content` 重复。现在拆成：

- `partial_text`: 只保存流式 delta。
- `assistant_text_parts`: 只保存最终 AssistantMessage 和原生 ToolUseBlock。
- `_select_sse_final_text()`: 有最终 AssistantMessage 时使用最终内容，否则 fallback 到 partial text。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:124`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1067`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1193`

### Prompt 日志增强

非流式和 SSE 现在共用 `_build_prompt_log_fields()`，避免两处日志结构漂移。

文件引用：

- `claude-agent-sdk-python/examples/http_proxy_server.py:112`
- `claude-agent-sdk-python/examples/http_proxy_server.py:995`
- `claude-agent-sdk-python/examples/http_proxy_server.py:1248`

## 测试

后端测试集中在工具调用解析、SSE 内容选择和工具协议隐藏。

文件引用：

- `claude-agent-sdk-python/tests/test_http_proxy_tool_calls.py:1`
- `claude-agent-sdk-python/tests/test_http_proxy_tool_calls.py:130`
- `claude-agent-sdk-python/tests/test_http_proxy_tool_calls.py:137`

验证命令：

```powershell
python -m py_compile examples/http_proxy_server.py
$env:PYTHONPATH='src'; python -m pytest tests/test_http_proxy_tool_calls.py -q
```

## 前端项目文件引用

当前 `claude-agent-sdk-python` checkout 中没有前端 Agent 文件。按现有计划，前端应在另一个项目中包含这些文件：

- `src/core/common/ActionDesign/Agent/AgentLoop.ts`
- `src/core/common/ActionDesign/Agent/__tests__/AgentLoopRepairPrompt.test.cjs`
- `src/core/common/ActionDesign/Agent/ClaudeAgent.ts`
- `src/core/common/ActionDesign/Agent/ActionAssembler.ts`
- `src/core/common/ActionDesign/Agent/tools/`

前端计划还未在当前 checkout 中执行，因为这些文件不存在。

## 2026-06-04 ActionDesign Agent Gateway 改造上下文

本次改造把后端从旧的 `examples/http_proxy_server.py` 单文件代理，扩展为新的 ActionDesign Agent Gateway。旧文件保持 dirty 状态，不回滚、不作为新入口；新实现集中在 `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/`，启动入口是 `claude-agent-sdk-python/examples/actiondesign_agent_gateway.py`。

### 新后端入口和依赖

新增可选依赖：

```toml
[project.optional-dependencies]
actiondesign = [
    "fastapi>=0.110.0",
    "uvicorn>=0.27.0",
    "httpx>=0.27.0",
]
```

文件引用：

- `claude-agent-sdk-python/pyproject.toml:35`
- `claude-agent-sdk-python/examples/actiondesign_agent_gateway.py:1`

### 新 gateway 包结构

核心代码放在独立包中：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/app.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/models.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/settings.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/tool_protocol.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/mimo_provider.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/claude_code_provider.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/backend_tools.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/actiondesign_backend_executor.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/embedding_client.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/knowledge_vector_index.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/redaction.py`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/session_log.py`

### 对外接口

新 gateway 提供 provider-scoped 路由，前端可以不改请求结构：

- `GET /api/actiondesign-agent/health`
- `GET /api/actiondesign-agent/models`
- `POST /api/actiondesign-agent/chat`
- `POST /api/actiondesign-agent/chat/stream`
- `POST /api/actiondesign-agent/mimo/chat`
- `POST /api/actiondesign-agent/mimo/chat/stream`
- `POST /api/actiondesign-agent/claude-code/chat`
- `POST /api/actiondesign-agent/claude-code/chat/stream`
- `POST /api/actiondesign-agent/tool-result`
- `GET /api/actiondesign-agent/tool-results/{conversation_id}/{run_id}`

响应格式保持一致：

```json
{
  "provider": "mimo",
  "model": "mimo-v2.5",
  "content": "展示给用户的文本",
  "tool_calls": [],
  "success": true,
  "error": null,
  "duration_ms": 123,
  "usage": {}
}
```

SSE 继续兼容旧前端格式：

```text
data: {"type":"text_delta","content":"..."}

data: {"type":"message_complete","content":"...","tool_calls":[],"success":true}
```

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/app.py:28`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/models.py:76`

### Provider 行为

`mimo` provider 直接对接 Anthropic-compatible Messages API，默认地址是：

```text
https://api.xiaomimimo.com/anthropic/v1/messages
```

认证默认使用 `api-key` header，也支持 bearer。`mimo-v2.5` 支持图片；`mimo-v2.5-pro` 遇到图片时返回 `MODEL_DOES_NOT_SUPPORT_IMAGES`，并给出 fallback model。

`claude-code` provider 使用 SDK 的 `query()` 和 `ClaudeAgentOptions`。它可以在后端使用 Claude Code 内部读类工具，但这些工具不会返回给前端执行。

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/mimo_provider.py:39`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/claude_code_provider.py:26`

### Claude Code 内部工具隔离

新增后端环境变量：

```powershell
CLAUDE_CODE_INTERNAL_TOOLS=Read,Grep,Glob,LS
CLAUDE_CODE_AUTO_ALLOW_INTERNAL_TOOLS=true
```

默认只开放读类内部工具：

- `Read`
- `Grep`
- `Glob`
- `LS`

不开放 `Bash/Edit/Write`。即使 Claude Code 输出 `[TOOL_CALL] Read({...})` 或 `[TOOL_CALL] Bash({...})`，后端也会过滤掉，不会给前端执行。只有 ActionDesign 白名单工具会进入 `tool_calls`。

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/settings.py:136`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/claude_code_provider.py:250`

### ActionDesign 前端工具白名单

后端只允许 ActionDesign 前端工具返回给前端，例如：

- `create_node`
- `insert_node`
- `delete_node`
- `create_action`
- `preview_code`
- `propose_plan`
- `ask_user`
- `list_elements`
- `get_element_detail`

`toolNames` 只能缩小白名单，不能把 `Read`、`Bash`、`mcp.*`、`skill.*` 或 `knowledge.*` 扩展成前端可执行工具。

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/models.py:15`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/mimo_provider.py:359`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/claude_code_provider.py:250`

### 工具协议强化

`tool_protocol.py` 复刻并强化旧 proxy 行为：

- 支持 `[TOOL_CALL]`
- 兼容误写 `[TOL_CALL]`
- 支持未加引号 key 的 JSON repair
- 避免破坏 URL 中的冒号
- 支持 JSON actions 提取
- 支持 `Question -> ask_user`
- 支持图片 data URL 前缀剥离
- 支持跨 chunk 隐藏工具协议
- 工具调用清理只移除完整工具表达式，不截断后续普通说明文本

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/tool_protocol.py:1`
- `claude-agent-sdk-python/tests/test_actiondesign_gateway_tool_protocol.py:1`

### MiMo backend tool loop

MiMo 不再只是“一次请求直接返回模型输出”。现在 MiMo 可以通过后端内部文本协议调用后端工具：

```text
[BACKEND_TOOL_CALL] knowledge.search({"query":"NullCondition required validation"})
[BACKEND_TOOL_CALL] knowledge.read({"path":"components/actions.md","heading":"NullCondition"})
[BACKEND_TOOL_CALL] mcp.search({"query":"ActionDesign validation"})
[BACKEND_TOOL_CALL] skill.load({"name":"quality-nonconformance"})
```

后端执行这些工具后，把结果以内部协议回灌给 MiMo：

```text
[BACKEND_TOOL_RESULT] knowledge.search
status: success
result:
...
```

循环继续，直到 MiMo 不再输出 backend tool call。最终只把普通文本和 ActionDesign 前端工具返回给前端：

```text
[TOOL_CALL] create_node({"elementKey":"NullCondition"})
```

backend tool 协议不会进入前端 `content`、`tool_calls` 或 SSE delta。

默认限制：

- `MIMO_MAX_BACKEND_TOOL_TURNS=6`
- `MIMO_MAX_BACKEND_TOOL_CALLS_PER_TURN=4`

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/mimo_provider.py:109`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/backend_tools.py:89`
- `claude-agent-sdk-python/tests/test_actiondesign_gateway_mimo_backend_loop.py:44`

### 后端工具 allowlist

当前后端工具白名单：

- `mcp.list_resources`
- `mcp.read_resource`
- `mcp.search`
- `mcp.call_tool`
- `skill.search`
- `skill.load`
- `knowledge.search`
- `knowledge.read`

`mcp.call_tool` 默认不会直接放行任意 MCP tool，必须通过 `ACTIONDESIGN_AGENT_MCP_READ_ONLY_TOOLS` 显式配置嵌套工具名，否则返回 `BACKEND_TOOL_NOT_ALLOWED`。

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/backend_tools.py:11`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/backend_tools.py:109`

### Knowledge 向量检索

新增本地 Markdown 知识库向量索引。配置：

```powershell
ACTIONDESIGN_KNOWLEDGE_ROOT=G:\hoyi\gxp2.components\knowledge
ACTIONDESIGN_KNOWLEDGE_INDEX_DIR=debug_logs/actiondesign-agent/knowledge-index

ACTIONDESIGN_EMBEDDING_PROVIDER=openai-compatible
ACTIONDESIGN_EMBEDDING_BASE_URL=http://127.0.0.1:xxxx/v1
ACTIONDESIGN_EMBEDDING_API_KEY=xxx
ACTIONDESIGN_EMBEDDING_MODEL=text-embedding-xxx

ACTIONDESIGN_KNOWLEDGE_MAX_RESULTS=4
ACTIONDESIGN_KNOWLEDGE_MAX_CHARS_PER_ITEM=4000
ACTIONDESIGN_KNOWLEDGE_MAX_CONTEXT_CHARS=12000
```

实现规则：

- 扫描 `knowledge/**/*.md`
- Markdown 按标题切块
- 每个 chunk 记录 `id/path/category/title/heading/content/mtime/size/embedding`
- 索引落盘到 `ACTIONDESIGN_KNOWLEDGE_INDEX_DIR`
- 根据文件 `mtime/size` 判断是否复用已有 embedding
- 查询时生成 query embedding，先做 cosine similarity topK
- embedding 服务不可用或失败时，自动退回关键词检索
- 数据量小，第一版使用本地 `manifest.json` + `chunks.jsonl`，不引入 Milvus/pgvector

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/knowledge_vector_index.py:36`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/embedding_client.py:17`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/actiondesign_backend_executor.py:11`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/settings.py:164`

### knowledge.search / knowledge.read

`knowledge.search(args)`：

- 参数：`query`
- 使用 query 向量检索
- 返回 top snippets
- 每条包含 `path/category/title/heading/score/snippet`

`knowledge.read(args)`：

- 参数：`path`，可选 `heading`
- 读取指定 Markdown 文件或指定 heading chunk
- 限制最大字符数
- 禁止读取 knowledge 根目录外文件
- 路径穿越返回 `KNOWLEDGE_PATH_FORBIDDEN`

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/actiondesign_backend_executor.py:23`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/actiondesign_backend_executor.py:57`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/knowledge_vector_index.py:82`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/knowledge_vector_index.py:120`

### MiMo knowledge prompt

MiMo 的 backend-only prompt 已改为：

- 如果需要组件参数、事件、用法、示例，先调用 `knowledge.search`
- search 结果不够时再调用 `knowledge.read`
- 最终只输出 ActionDesign 前端工具，例如 `create_node`、`preview_code`、`propose_plan`

Claude Code provider 保持不扩大改动，仍依靠它自己的 `Read/Grep/Glob/LS` 读项目文件。

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/mimo_provider.py:28`

### 前端工具结果回传

前端执行 ActionDesign 工具后，继续使用后端 tool-result 接口回传：

```http
POST /api/actiondesign-agent/tool-result
```

后端按 `(conversation_id, run_id, tool_call_id)` 去重缓存，TTL 为 5 分钟。查询接口：

```http
GET /api/actiondesign-agent/tool-results/{conversation_id}/{run_id}
```

这个机制用于调试和前后端对齐，不会把 Claude Code 内部工具或 MiMo backend tools 交给前端执行。

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/session_log.py:1`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/app.py:123`

### 安全边界

- provider API key、embedding API key 只从后端环境变量读取，不进入前端，也不写 localStorage。
- 日志脱敏会处理 secret、Authorization、api-key、图片 base64 等敏感内容。
- `conversationId` 做安全校验，非法 id 返回 400。
- `projectPath` 只在路径存在时用于 Claude Code cwd，不存在不失败。
- 后端不直接修改 ActionDesign 画布状态。
- 第一版不执行 shell，不写项目文件，不自动运行 skill 中的命令。

文件引用：

- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/redaction.py:1`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/app.py:139`
- `claude-agent-sdk-python/src/claude_agent_sdk/actiondesign_gateway/claude_code_provider.py:142`

### 测试覆盖

新增和扩展测试：

- `claude-agent-sdk-python/tests/test_actiondesign_gateway_tool_protocol.py`
- `claude-agent-sdk-python/tests/test_actiondesign_gateway_app.py`
- `claude-agent-sdk-python/tests/test_actiondesign_gateway_mimo_backend_loop.py`
- `claude-agent-sdk-python/tests/test_actiondesign_gateway_knowledge_vector_index.py`

覆盖点：

- `[TOL_CALL]` 容错
- URL 冒号不被 JSON repair 破坏
- 重复 `create_node` 保留
- JSON actions 提取
- `Question -> ask_user`
- 跨 chunk 隐藏工具协议
- `/models` 无 key 时显示 `mimo.unavailable`
- `/tool-result` 幂等
- 非法 `conversationId` 返回 400
- `mimo-v2.5-pro` 图片拒绝
- Claude Code 内部工具默认是 `Read/Grep/Glob/LS`
- Claude Code/文本协议中的 `Read/Bash` 不返回前端
- MiMo backend tool loop 执行并回灌结果
- backend tool marker 不出现在 SSE
- `knowledge.search` 执行后最终只返回 ActionDesign tool_calls
- knowledge 索引 Markdown fixture
- 语义 query 返回正确 chunk
- 文件未变不重复生成 embedding
- embedding 失败退回关键词检索
- `knowledge.read` 禁止路径穿越

### 验证结果

已执行：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -m pytest tests/test_http_proxy_tool_calls.py tests/test_actiondesign_gateway_tool_protocol.py tests/test_actiondesign_gateway_app.py tests/test_actiondesign_gateway_mimo_backend_loop.py tests/test_actiondesign_gateway_knowledge_vector_index.py -q
```

结果：

```text
48 passed
```

已执行：

```powershell
python -m compileall src\claude_agent_sdk\actiondesign_gateway examples\actiondesign_agent_gateway.py
git diff --check
```

结果：

- compileall 通过
- `git diff --check` 无空白错误，仅提示 `examples/http_proxy_server.py` 和 `pyproject.toml` 的 CRLF 换行警告

本地 smoke：

```text
http://127.0.0.1:8889/api/actiondesign-agent/models
```

当前 smoke 进程：

```text
PID 23824
```
