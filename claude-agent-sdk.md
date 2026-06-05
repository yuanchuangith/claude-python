# Claude Agent SDK ActionDesign Agent 上下文

## 文档用途

这个文件是当前仓库根目录的长期上下文记录，给后续切换智能体时快速理解项目方向、历史开发内容、接口约束和当前状态使用。

维护规则：

- 保留项目介绍、流程、约束、安全边界、历史改造背景和验证记录。
- 后续更新只对发生变化的对应章节做增删改，不要整篇压缩成摘要。
- 已删除的历史入口可以描述为历史背景，但不要再作为当前可调用入口记录。
- 后续上下文只写当前仓库根目录的 `claude-agent-sdk.md`，不要写旧父目录文件。

## Agent 简介

这个 Agent 是给前端 ActionDesign 编排工具使用的后端代理服务。前端把用户需求、页面上下文、可用编排元素和历史对话拼成 prompt，后端调用 provider，再把模型输出转换成前端可执行的 ActionDesign 工具调用。

当前后端项目是 `claude-agent-sdk-python`。现有实现已经从历史单文件 Claude 代理入口收敛到新的 ActionDesign Agent Gateway：

- `examples/actiondesign_agent_gateway.py`
- `src/claude_agent_sdk/actiondesign_gateway/`

## 当前对外入口

当前聊天入口只保留 provider 专用路由：

- `POST /api/actiondesign-agent/mimo/chat`
- `POST /api/actiondesign-agent/mimo/chat/stream`
- `POST /api/actiondesign-agent/claude-code/chat`
- `POST /api/actiondesign-agent/claude-code/chat/stream`

辅助接口继续保留：

- `GET /api/actiondesign-agent/health`
- `GET /api/actiondesign-agent/models`
- `POST /api/actiondesign-agent/knowledge/upload`
- `POST /api/actiondesign-agent/knowledge/search`
- `POST /api/actiondesign-agent/knowledge/read`
- `POST /api/actiondesign-agent/tool-result`
- `GET /api/actiondesign-agent/tool-results/{conversation_id}/{run_id}`

已删除的历史入口：

- 历史 Claude 代理非流式入口。
- 历史 Claude 代理流式入口。
- 历史 Claude 代理 tool-result 回传入口。
- 新 gateway 的通用自动聊天入口。

前端请求体里的 `provider` 字段仍保留用于兼容，但 provider 专用路由以 URL 为准。

## 主要能力

- 接收前端 prompt，并按 URL 路由调用 MiMo 或 Claude Code provider。
- 支持普通文本输出和 `[TOOL_CALL] tool_name({...})` 前端工具协议。
- 支持从 JSON actions 结构中恢复工具调用。
- 支持 SSE 流式输出，同时隐藏内部工具协议，避免展示给用户。
- 只把 ActionDesign 白名单工具返回给前端执行。
- 支持 backend-only tool loop，让模型先查询知识库或只读后端工具，再生成最终 ActionDesign 操作。
- 支持知识库上传、检索、读取。
- 接收前端工具执行结果并缓存，用于调试和前后端对齐。
- 记录调试日志，并对 API key、Authorization、图片 base64 等敏感内容脱敏。

## Gateway 包结构

核心代码放在独立包中：

- `src/claude_agent_sdk/actiondesign_gateway/app.py`
- `src/claude_agent_sdk/actiondesign_gateway/models.py`
- `src/claude_agent_sdk/actiondesign_gateway/settings.py`
- `src/claude_agent_sdk/actiondesign_gateway/tool_protocol.py`
- `src/claude_agent_sdk/actiondesign_gateway/mimo_provider.py`
- `src/claude_agent_sdk/actiondesign_gateway/mimo_http.py`
- `src/claude_agent_sdk/actiondesign_gateway/mimo_stream.py`
- `src/claude_agent_sdk/actiondesign_gateway/mimo_protocol.py`
- `src/claude_agent_sdk/actiondesign_gateway/claude_code_provider.py`
- `src/claude_agent_sdk/actiondesign_gateway/backend_tools.py`
- `src/claude_agent_sdk/actiondesign_gateway/actiondesign_backend_executor.py`
- `src/claude_agent_sdk/actiondesign_gateway/embedding_client.py`
- `src/claude_agent_sdk/actiondesign_gateway/knowledge_vector_index.py`
- `src/claude_agent_sdk/actiondesign_gateway/qdrant_knowledge_store.py`
- `src/claude_agent_sdk/actiondesign_gateway/redaction.py`
- `src/claude_agent_sdk/actiondesign_gateway/session_log.py`

启动入口：

- `examples/actiondesign_agent_gateway.py`

## 请求和响应模型

`AgentChatRequest` 描述前端发给后端的请求，包含 provider、model、conversationId、prompt、stream、toolNames、images、maxTokens、thinking、projectPath 等字段。

响应格式保持一致：

```json
{
  "provider": "mimo",
  "model": "mimo-v2.5",
  "content": "展示给用户的文本",
  "tool_calls": [],
  "success": true,
  "error": null,
  "code": null,
  "duration_ms": 123,
  "usage": {}
}
```

SSE 兼容前端格式：

```text
data: {"type":"text_delta","content":"..."}

data: {"type":"message_complete","content":"...","tool_calls":[],"success":true}
```

前端判断一次 provider 调用完成时，以 `message_complete` 为准。

## Provider 行为

### MiMo

`mimo` provider 直接对接 Anthropic-compatible Messages API。

默认配置：

- 默认模型：`mimo-v2.5`
- 默认认证：`api-key` header
- 可选认证：bearer
- 默认超时：由 settings 中 MiMo timeout 配置控制

图片支持：

- 图片模型白名单来自 `models.py` 的 `MIMO_IMAGE_MODELS`。
- 当前 `mimo-v2.5` 支持图片。
- 当前 `mimo-v2.5-pro` 不支持图片。
- 不支持图片的模型遇到 `images` 会返回 `MODEL_DOES_NOT_SUPPORT_IMAGES`，并给出 fallback image model。

MiMo 支持 backend tool loop。模型可以先输出 backend-only 工具调用，后端执行后把结果回灌给 MiMo，直到 MiMo 输出最终回答或 ActionDesign 前端工具调用。

默认限制：

- `MIMO_MAX_BACKEND_TOOL_TURNS=6`
- `MIMO_MAX_BACKEND_TOOL_CALLS_PER_TURN=4`

### Claude Code

`claude-code` provider 使用 SDK 的 `query()` 和 `ClaudeAgentOptions`。它可以在后端使用 Claude Code 内部读类工具，但这些工具不会返回给前端执行。

默认 Claude Code 内部工具：

- `Read`
- `Grep`
- `Glob`
- `LS`

不开放 `Bash/Edit/Write` 给前端。最终返回给前端执行的只能是 ActionDesign 白名单工具。

## ActionDesign 前端工具白名单

后端只允许 ActionDesign 前端工具返回给前端，例如：

- `list_elements`
- `get_element_detail`
- `get_component_methods`
- `get_page_actions`
- `propose_plan`
- `ask_user`
- `enter_plan_mode`
- `exit_plan_mode`
- `create_action`
- `create_node`
- `insert_node`
- `delete_node`
- `preview_code`

`toolNames` 只能缩小白名单，不能把 `Read`、`Bash`、`mcp.*`、`skill.*` 或 `knowledge.*` 扩展成前端可执行工具。

## 前端工具协议

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

`tool_protocol.py` 支持和约束：

- 支持 `[TOOL_CALL]`
- 兼容误写 `[TOL_CALL]`
- 支持未加引号 key 的 JSON repair
- 避免破坏 URL 中的冒号
- 支持 JSON actions 提取
- 支持 `Question -> ask_user`
- 支持图片 data URL 前缀剥离
- 支持跨 chunk 隐藏工具协议
- 工具调用清理只移除完整工具表达式，不截断后续普通说明文本

## JSON actions 恢复

如果模型输出 JSON actions，后端会用 `_extract_actions_from_json()` 和 `_convert_json_node_to_tool_call()` 转成前端工具调用。

常见转换：

- 有 `insertAfterKey` 或 `anchorKey` 时转为 `insert_node`。
- 有 `elementKey` 或 `element` 时转为 `create_node`。
- `actionKey`、`title`、`paramsValue` 会映射进 arguments。

## Backend-only 工具协议

MiMo 和 Claude Code 都支持 backend-only 工具协议：

```text
[BACKEND_TOOL_CALL] knowledge.search({"query":"NullCondition required validation"})
[BACKEND_TOOL_CALL] knowledge.read({"path":"components/actions.md","heading":"NullCondition"})
[BACKEND_TOOL_CALL] mcp.search({"query":"ActionDesign validation"})
[BACKEND_TOOL_CALL] skill.load({"name":"quality-nonconformance"})
```

后端执行这些工具后，把结果以内部协议回灌给模型：

```text
[BACKEND_TOOL_RESULT] knowledge.search
status: success
result:
...
```

最终只把普通文本和 ActionDesign 前端工具返回给前端：

```text
[TOOL_CALL] create_node({"elementKey":"NullCondition"})
```

backend-only 协议不会进入前端 `content`、SSE delta 或前端 `tool_calls`。

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

如果 backend executor 抛异常，后端会转成 `BackendToolResult(status="failed", code="BACKEND_TOOL_FAILED")` 并回灌给模型，不中断整个 provider loop。

## 知识库能力

知识库支持两种读取来源：

1. Qdrant store：由 `ACTIONDESIGN_QDRANT_URL` 等配置启用。
2. 本地 Markdown index：由 `ACTIONDESIGN_KNOWLEDGE_ROOT` 启用，索引目录默认是 `debug_logs/actiondesign-agent/knowledge-index`。

`knowledge.search/read` backend tool 和公开的 `/knowledge/search`、`/knowledge/read` 共享同一套执行路径：

- 优先使用 Qdrant store。
- Qdrant 未配置时 fallback 到本地 Markdown index。
- 两者都不可用时返回 `KNOWLEDGE_ROOT_NOT_CONFIGURED`。

`/knowledge/upload` 只支持 Qdrant store，不写本地 Markdown 文件。

### 本地 Markdown 向量索引

配置示例：

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
- 数据量小，第一版使用本地 `manifest.json` + `chunks.jsonl`

### knowledge.search

参数：

- `query`
- `limit`
- `maxCharsPerItem`
- `maxContextChars`

返回 top snippets，每条包含：

- `path`
- `category`
- `title`
- `heading`
- `score`
- `snippet`

### knowledge.read

参数：

- `path`
- `heading`
- `maxChars`

行为：

- 读取指定 Markdown 文件或指定 heading chunk。
- 限制最大字符数。
- 禁止读取 knowledge 根目录外文件。
- 路径穿越返回 `KNOWLEDGE_PATH_FORBIDDEN`，HTTP 状态码为 400。
- 文件不存在返回 `KNOWLEDGE_NOT_FOUND`，HTTP 状态码为 404。

## MiMo knowledge prompt

MiMo 的 backend-only prompt 约束：

- 如果需要组件参数、事件、用法、示例，先调用 `knowledge.search`。
- search 结果不够时再调用 `knowledge.read`。
- 可以调用只读 backend MCP 或 Skill 工具。
- backend-only 工具只供后端执行，不展示给前端。
- 最终只输出 ActionDesign 前端工具，例如 `create_node`、`preview_code`、`propose_plan`。

Claude Code provider 保持不扩大改动，仍依靠它自己的 `Read/Grep/Glob/LS` 读项目文件。

## 前端工具结果回传

前端执行 ActionDesign 工具后，继续使用后端 tool-result 接口回传：

```http
POST /api/actiondesign-agent/tool-result
```

后端按 `(conversation_id, run_id, tool_call_id)` 去重缓存，TTL 为 5 分钟。

查询接口：

```http
GET /api/actiondesign-agent/tool-results/{conversation_id}/{run_id}
```

这个机制用于调试和前后端对齐，不会把 Claude Code 内部工具或 MiMo backend tools 交给前端执行。

## 日志和调试

后端调试日志写入当前运行目录下的 `debug_logs/`。如果请求带了 `X-Conversation-Id`，使用该 conversation id 做会话日志关联。

主要记录内容：

- prompt 预览和尾部预览。
- provider、model、duration、success、error。
- 原始模型文本、clean content、tool_calls。
- backend tool 调用和回灌结果。
- 前端 tool-result 回传。

日志脱敏会处理：

- secret
- Authorization
- api-key
- 图片 base64
- 其它明显敏感字段

## 编排内容生成流程

1. 前端收集用户需求、页面结构、可用元素、已有动作和对话历史。
2. 前端把这些信息拼成 Agent prompt，发给 provider 专用 gateway 路由。
3. 后端按 URL 选择 MiMo 或 Claude Code provider。
4. Provider 可先执行 backend-only knowledge/MCP/Skill 查询。
5. 模型输出自然语言说明、JSON actions，或文本 `[TOOL_CALL]`。
6. 后端把允许的 ActionDesign 工具调用归一化为 `tool_calls`。
7. 前端读取 `tool_calls` 并执行具体编排工具，例如创建动作、创建节点、插入节点、预览代码。
8. 前端把工具执行结果发到 tool-result 接口，后端记录结果供调试和排查。

## 安全边界

- provider API key、embedding API key 只从后端环境变量读取，不进入前端，也不写 localStorage。
- 日志脱敏会处理 secret、Authorization、api-key、图片 base64 等敏感内容。
- `conversationId` 做安全校验，非法 id 返回 400。
- `projectPath` 只在路径存在时用于 Claude Code cwd，不存在不失败。
- 后端不直接修改 ActionDesign 画布状态。
- 第一版不执行 shell，不写项目文件，不自动运行 skill 中的命令。
- 前端可执行工具必须来自 ActionDesign 白名单。
- backend-only 工具结果不暴露给前端。

## 前端项目文件引用

当前 `claude-agent-sdk-python` checkout 中没有前端 Agent 文件。按现有计划，前端应在另一个项目中包含这些文件：

- `src/core/common/ActionDesign/Agent/AgentLoop.ts`
- `src/core/common/ActionDesign/Agent/__tests__/AgentLoopRepairPrompt.test.cjs`
- `src/core/common/ActionDesign/Agent/ClaudeAgent.ts`
- `src/core/common/ActionDesign/Agent/ActionAssembler.ts`
- `src/core/common/ActionDesign/Agent/tools/`

前端计划还未在当前 checkout 中执行，因为这些文件不存在。

## 历史修复背景

### 旧 Claude 代理阶段

历史阶段的单文件代理提供过：

- 非流式 Claude 调用。
- SSE 流式 Claude 调用。
- prompt 日志字段。
- 工具调用解析。
- 工具结果接收和去重缓存。

这些内容保留为历史背景，用于理解后续 gateway 的设计来源。当前实现不再保留旧入口兼容。

历史修复点：

- SSE 最终内容去重：避免 partial delta 和最终 AssistantMessage 被重复拼进最终内容。
- Prompt 日志增强：非流式和 SSE 共用 prompt 日志字段构造逻辑。
- 工具协议增强：支持文本工具调用、JSON actions、工具协议隐藏。

### 2026-06-04 ActionDesign Agent Gateway 改造

本次改造把后端从历史单文件代理扩展为新的 ActionDesign Agent Gateway。新实现集中在 `src/claude_agent_sdk/actiondesign_gateway/`，启动入口是 `examples/actiondesign_agent_gateway.py`。

新增能力：

- provider-scoped 路由。
- MiMo provider。
- Claude Code provider。
- backend-only tool loop。
- 本地 Markdown knowledge index。
- tool-result 去重缓存。
- provider API key 和日志脱敏安全边界。

### 2026-06-05 入口收敛和稳定性更新

本次更新：

- 当前聊天入口只保留 MiMo 和 Claude Code provider 专用 chat/stream 路由。
- 通用自动聊天路由不再注册。
- 已删除的历史 Claude 代理入口不再保留兼容。
- knowledge public API 继续保留。
- tool-result API 继续保留。
- `AgentChatRequest.provider` 字段继续保留，避免前端请求体兼容性破坏。
- `/models` 保留 `defaultProvider/defaultModel` 兼容字段，并在 provider 信息里返回 `chatPath`、`streamPath`、`supportsGenericChat: false`。

稳定性修复点：

- `knowledge/read` 非法路径返回结构化 400，code 为 `KNOWLEDGE_PATH_FORBIDDEN`。
- backend tool executor 异常降级为 `BACKEND_TOOL_FAILED`，回灌给模型，不中断 provider loop。
- 公开 `/knowledge/search` 和 `/knowledge/read` 与 backend `knowledge.search/read` 共用执行器。
- public knowledge API 优先 Qdrant，未配置 Qdrant 时 fallback 到本地 Markdown index。
- `/knowledge/upload` 仍只支持 Qdrant，不写本地 Markdown 文件。

### 2026-06-05 MiMo provider streaming and robustness fix

MiMo provider 已完成以下修复：

- MiMo stream 入口改为真实调用上游 Anthropic-compatible Messages API SSE，请求体包含 `stream: true`。
- 前端判断 MiMo 调用完成时，只以 `message_complete` 为准；`text_delta` 只用于增量展示。
- 单个 MiMo turn 以 `message_stop` 或上游流正常结束为结束信号。
- 整个 provider 调用只有在当前 turn 没有可执行 backend tool call 时才完成。
- `stop_reason == "max_tokens"` 会标记未完整完成：非流式返回 `success: false`，SSE `message_complete` 返回 `success: false` 和 `MIMO_RESPONSE_INCOMPLETE`。
- malformed backend tool marker 不再泄漏为普通最终文本，会作为内部 `BACKEND_TOOL_ARGUMENTS_INVALID` 结果回灌给 MiMo。
- `[BACKEND_TOOL_CALL]` 和 `[BACKEND_TOOL_RESULT]` 不进入最终 `content`、SSE delta 或前端 `tool_calls`。
- 多轮 MiMo usage 会累加 numeric 字段，例如 `input_tokens/output_tokens`，并保留 `usage.turns` 便于排查。
- 上游错误结构化为 `MIMO_UPSTREAM_ERROR`、`MIMO_UPSTREAM_TIMEOUT`、`MIMO_UPSTREAM_NETWORK_ERROR`、`MIMO_RESPONSE_INVALID`、`MIMO_STREAM_ERROR`。
- 图片模型支持判断统一使用 `models.py` 的 `MIMO_IMAGE_MODELS`，fallback 图片模型也从该白名单派生。

### 2026-06-05 MiMo provider usability optimization

本次在已有 MiMo SSE 和 robustness 修复基础上继续优化：

- `AgentChatResponse` 新增 `code: str | None`，非流式 `/mimo/chat` 在 `max_tokens` 或上游失败等场景会保留 `MIMO_RESPONSE_INCOMPLETE`、`MIMO_UPSTREAM_*`、`MIMO_RESPONSE_INVALID` 等结构化错误码。
- `stream_mimo()` 采用按 MiMo turn 缓冲策略：当前 turn 确认没有 backend tool call 后，才把清理后的可见文本作为 `text_delta` 发给前端；如果当前 turn 含有效或 malformed backend call，则整轮文本不展示，只回灌 backend result 后进入下一轮。
- 前端仍只用 `message_complete` 判断一次 MiMo 调用完成；`text_delta` 只是最终可见 turn 的展示增量，不再代表后台检索 turn。
- malformed 前端工具 marker（`[TOOL_CALL]` 或 `[TOL_CALL]` 但无法解析成允许工具调用）会从最终 `content` 和 SSE payload 中清理，`tool_calls` 为空，调用仍保持 `success: true`。
- usage 输出保持兼容：单轮响应只返回原 numeric usage 字段；多轮 backend tool loop 才增加 `usage.turns`，同时累计 numeric 字段。
- MiMo SSE parser 已标准化为空行聚合 event，支持多行 `data:`、`event:` fallback、`[DONE]` 正常结束；invalid JSON 或非对象事件返回 `MIMO_RESPONSE_INVALID`。
- MiMo provider 维护边界已拆分：`mimo_provider.py` 保留 provider loop、请求体构造和响应组装；`mimo_http.py` 负责 HTTP/SSE 请求和上游错误映射；`mimo_stream.py` 负责 SSE event 解析、usage/stop/text delta 提取；`mimo_protocol.py` 负责 frontend/backend marker 诊断和安全清理。

## 测试覆盖

Gateway 相关测试：

- `tests/test_actiondesign_gateway_tool_protocol.py`
- `tests/test_actiondesign_gateway_app.py`
- `tests/test_actiondesign_gateway_mimo_backend_loop.py`
- `tests/test_actiondesign_gateway_knowledge_vector_index.py`
- `tests/test_actiondesign_gateway_qdrant_knowledge_store.py`

覆盖点：

- `[TOL_CALL]` 容错。
- URL 冒号不被 JSON repair 破坏。
- 重复 `create_node` 保留。
- JSON actions 提取。
- `Question -> ask_user`。
- 跨 chunk 隐藏工具协议。
- `/models` 无 key 时显示 `mimo.unavailable`。
- `/models` 返回 provider 专用 `chatPath/streamPath`。
- `/tool-result` 幂等。
- 非法 `conversationId` 返回 400。
- 图片模型拒绝和 fallback。
- Claude Code 内部工具默认是 `Read/Grep/Glob/LS`。
- Claude Code/文本协议中的 `Read/Bash` 不返回前端。
- MiMo backend tool loop 执行并回灌结果。
- backend tool marker 不出现在 SSE 和最终结果中。
- malformed backend tool marker 不作为最终文本泄漏。
- MiMo stream 使用上游 SSE 并逐 chunk 产生 `text_delta`。
- MiMo 多轮 usage 聚合。
- MiMo 单轮 usage 不输出 `usage.turns`，多轮才输出 `usage.turns`。
- MiMo stream 对 backend tool turn 严格缓冲，不提前展示该 turn 的文本。
- malformed 前端 `[TOOL_CALL]`/`[TOL_CALL]` 会清理，不进入最终 content 或 SSE payload。
- MiMo SSE parser 支持多行 `data:`、`event:` fallback、`[DONE]` 和 invalid JSON 结构化错误。
- MiMo timeout、network error、invalid JSON、SSE error event 结构化。
- `knowledge.search` 执行后最终只返回 ActionDesign tool_calls。
- knowledge 索引 Markdown fixture。
- 语义 query 返回正确 chunk。
- 文件未变不重复生成 embedding。
- embedding 失败退回关键词检索。
- `knowledge.read` 禁止路径穿越。
- Qdrant knowledge store 行为。

## 最新验证结果

已执行：

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/test_actiondesign_gateway_tool_protocol.py tests/test_actiondesign_gateway_app.py tests/test_actiondesign_gateway_mimo_backend_loop.py tests/test_actiondesign_gateway_knowledge_vector_index.py tests/test_actiondesign_gateway_qdrant_knowledge_store.py -q
```

结果：

```text
60 passed
```

已执行：

```powershell
python -m compileall src\claude_agent_sdk\actiondesign_gateway examples\actiondesign_agent_gateway.py
```

结果：通过。

已执行：

旧入口残留搜索已执行，结果无命中。为避免上下文文件自身被后续搜索命中，这里不内嵌旧入口 literal pattern。

已执行：

```powershell
git diff --check
```

结果：

- 旧入口残留搜索无命中。
- `git diff --check` 无空白错误；Windows checkout 可能提示 CRLF normalization warning。
