# ActionDesign Thin Gateway

该服务是 ActionDesign 前端 LangGraph Agent 的轻量 Python 网关。它不是 Agent 运行时。

## 职责

- 受控转发动态渠道，并按 Profile 选择 OpenAI Chat Completions 或 Claude/Anthropic Messages 协议。
- 将统一 native tools/image/tool choice 契约转换为对应 Provider 协议，并把 function/tool use 返回规范化为统一响应。
- 输出版本化 SSE envelope。
- 记录 request、response、usage、耗时、错误和前端 tool result。
- 管理 API Profile 与 Key，API 永不回显 secret。
- 脱敏转发前端 LangGraph trace 到 LangSmith。
- 通过环境只读或管理员 managed 的通用 stdio/streamable HTTP MCP proxy 注入目标 Secret、透传 Schema/result，并记录元数据日志。
- 使用 Pydantic v2 校验请求、配置、SSE、响应、Profile、tool result 和 trace；使用 Draft 2020-12 JSON Schema 校验 Provider 返回的动态工具参数。

## 明确不负责

- 不运行 ActionDesign 工具循环。
- 不解析文本标记或 XML 工具协议。
- 不执行 Review、Topic classifier、计划闸门或完成闸门。
- 不提供知识库、embedding、Markdown index 或 Qdrant。
- 不接受任意 URL 转发；受管 Profile host 必须在 allowlist 中，OpenAI `/v1/chat/completions` 或 Claude `/v1/messages` path 由网关按协议规范化。
- 普通 MCP runtime 请求不接受浏览器传入命令、脚本路径、环境变量或任意目标 authority；只有管理员 Bearer 管理 API 可以提交 strict Server 配置，且不能扩大部署级 allowlist。不复制 GXP 查询业务逻辑，不执行数据库或设计写入。

## 启动

```bash
pip install -e ".[actiondesign]"
uvicorn claude_agent_sdk.actiondesign_gateway.app:app --host 0.0.0.0 --port 8888
```

健康检查：

```text
GET /api/actiondesign-agent/health
```

OpenAPI document version: `2.2.0` (strict runtime identity and full request-attempt detail contract).

Runtime identity contract: `conversationId` is a canonical lowercase UUID v4 and `runId` is `run_<uuid-v4>`. Chat/stream/tool-result require exact Body/Header identity agreement; the service seal path also requires UUID v4. Admin conversation lookup/seal retains legacy safe-ID compatibility for historical logs.

## API

### 模型与转发

- `GET /api/actiondesign-agent/models`
- `POST /api/actiondesign-agent/chat`
- `POST /api/actiondesign-agent/chat/stream`
- `POST /api/actiondesign-agent/mimo/chat`
- `POST /api/actiondesign-agent/mimo/chat/stream`
- `POST /api/actiondesign-agent/claude-code/chat`
- `POST /api/actiondesign-agent/claude-code/chat/stream`

`mimo`/`claude-code` 固定路由保留用于兼容旧前端；动态渠道使用通用 `/chat` 路由。请求中的 `tools` 使用统一 native schema，网关会转换为 OpenAI `type=function` 或 Claude `input_schema`：

```json
{
  "provider": "mimo",
  "conversationId": "11111111-1111-4111-8111-111111111111",
  "runId": "run_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "systemPrompt": "完整且独立的 ActionDesign 系统规则",
  "prompt": "...",
  "stream": true,
  "protocolVersion": "1.0",
  "graphVersion": "1.6.0",
  "promptManifestVersion": "2.1.0",
  "promptManifestHash": "sha256:...",
  "toolCatalogVersion": "1.4.0",
  "toolCatalogHash": "sha256:...",
  "skillCatalogVersion": "1.0.0",
  "skillCatalogHash": "sha256:...",
  "loadedSkillVersions": { "gxp-lowcode-debug": "1.0.0" },
  "mcpServerIds": ["shared-readonly"],
  "tools": [
    {
      "name": "inspect_action",
      "description": "Inspect an action",
      "input_schema": {
        "type": "object",
        "properties": {
          "actionKey": { "type": "string" }
        }
      }
    }
  ]
}
```

`systemPrompt` 是必填非空字段，并映射到 Provider 的独立 `system` 字段；不会拼接到普通 `prompt`。缺失、仅空白、超长或结构不合法的请求在发起 Provider 调用前返回 422。

网关只做通用传输层结构校验，不维护 ActionDesign 画布业务工具白名单。业务工具名称、计划范围、画布目标、写入冲突和完成闸门继续由前端 LangGraph 校验。MCP Server 与 transport 由管理员固定配置，请求不能覆盖 URL、command、Secret 或认证绑定；网关只校验调用方指定的工具确实由该 Server 的实时 tools/list 暴露。

非流式响应：

```json
{
  "provider": "mimo",
  "model": "mimo-v2.5",
  "content": "",
  "tool_calls": [
    {
      "id": "call_1",
      "name": "inspect_action",
      "arguments": { "actionKey": "main" }
    }
  ],
  "success": true,
  "usage": {}
}
```

### SSE

每个 frame 都是统一 envelope：

```text
data: {"protocolVersion":"1.0","sequence":1,"conversationId":"11111111-1111-4111-8111-111111111111","runId":"run_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","traceId":"trace_1","timestamp":"...","type":"model.delta","payload":{"content":"..."}}

data: {"protocolVersion":"1.0","sequence":2,"conversationId":"11111111-1111-4111-8111-111111111111","runId":"run_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","traceId":"trace_1","timestamp":"...","type":"model.completed","payload":{"content":"...","tool_calls":[],"success":true}}
```

允许的 type：

- `model.delta`
- `model.completed`
- `gateway.error`

### Tool result 审计

- `POST /api/actiondesign-agent/tool-result`

该接口只记录前端工具执行结果，并按 `conversationId + runId + toolCallId` 幂等；不会触发下一轮模型调用。

### 通用 MCP transport / Secret proxy

- `GET /api/actiondesign-agent/mcp/{serverId}/tools`
- `POST /api/actiondesign-agent/mcp/{serverId}/call`

规则：

- MCP Server 来自环境只读配置或管理员 managed store；普通调用方只能提交 serverId、该 Server 实时暴露的 toolName 和 arguments。
- 仅支持后端 `stdio` 与 `streamable_http`。HTTP 强制 HTTPS、精确 authority/path allowlist、禁止 redirect；stdio 强制现存绝对 executable allowlist。
- stdio 子进程只继承最小系统环境并注入当前 Server 显式绑定的 Secret；HTTP 只向当前 Server 的固定 Header 注入 Secret。Provider、LangSmith、Admin 和其他 MCP Secret 不会交叉继承。
- tools/list 的 Draft 2020-12 input/output Schema 与 call result 使用有界 JSON 原样透传。网关不按业务含义选择工具、不解析 SQL、不解释结果，也不包含固定 Skill、工具目录或业务结果 Schema。
- 调用日志只记录参数/result 的 hash、长度、键名、耗时和 `isError`，不保存参数或结果正文。
- 启用时 `ACTIONDESIGN_AGENT_ALLOW_ORIGINS` 必须是显式 Origin，禁止 `*`。生产环境仍应放在已认证反向代理之后。
- 前端 Skill/Graph 决定调用顺序；网关不实现诊断工作流、不读取 Skill prompt、不判断业务结论。

### API Profile / Key

- `GET /admin/`：本项目内置的独立管理页面，不依赖其他前端工程
- `POST /api/actiondesign-agent/admin/login`
- `GET /api/actiondesign-agent/admin/access-key`
- `POST /api/actiondesign-agent/admin/access-key/rotate`
- `GET /api/actiondesign-agent/admin/profiles`
- `GET /api/actiondesign-agent/admin/profiles/{profileId}`
- `PUT /api/actiondesign-agent/admin/profiles/{profileId}`
- `DELETE /api/actiondesign-agent/admin/profiles/{profileId}`
- `GET|PUT|DELETE /api/actiondesign-agent/admin/providers/{providerId}`
- `GET|POST /api/actiondesign-agent/admin/profiles/{profileId}/models`
- `GET|PUT|DELETE /api/actiondesign-agent/admin/profiles/{profileId}/models/{modelId}`
- `PUT /api/actiondesign-agent/admin/profiles/{profileId}/default-model`
- `GET|PUT|DELETE /api/actiondesign-agent/admin/profiles/{profileId}/secret`
- `POST /api/actiondesign-agent/admin/profiles/{profileId}/secret/rotate`
- `POST /api/actiondesign-agent/admin/profiles/{profileId}/connection-test`
- `GET /api/actiondesign-agent/admin/profiles/{profileId}/health`
- `GET /api/actiondesign-agent/admin/channels/health`
- `GET /api/actiondesign-agent/admin/mcp-servers`
- `GET|PUT|DELETE /api/actiondesign-agent/admin/mcp-servers/{serverId}`
- `PUT /api/actiondesign-agent/admin/mcp-servers/{serverId}/enabled`
- `POST /api/actiondesign-agent/admin/mcp-servers/{serverId}/connection-test`
- `GET|PUT|DELETE /api/actiondesign-agent/admin/secrets/{secretId}`
- `POST /api/actiondesign-agent/admin/secrets/{secretId}/rotate`
- `GET /api/actiondesign-agent/admin/request-logs`
- `GET /api/actiondesign-agent/admin/request-logs/{requestId}`
- `GET /api/actiondesign-agent/admin/audit-logs`
- `GET /api/actiondesign-agent/admin/conversations`
- `GET /api/actiondesign-agent/admin/conversations/{conversationId}`
- `POST /api/actiondesign-agent/admin/conversations/{conversationId}/seal`

管理接口要求：

```text
POST /api/actiondesign-agent/admin/login {"password":"..."}
Authorization: Bearer <short-lived-session-token>
```

规则：

- 管理密码不硬编码在源码中；本项目部署时通过隔离 `.env` 设置 `ACTIONDESIGN_ADMIN_PASSWORD=<set-in-deployment>`，未配置时登录端点返回 `ADMIN_LOGIN_DISABLED`。登录连续失败 5 次后，同一来源会被限制 30 分钟。密码只在登录请求中发送，不写日志。`ACTIONDESIGN_ADMIN_ALLOW_REMOTE` 默认 `false`，因此公网 admin 路由不可访问；生产前端必须由 loopback 上的 HTTPS 同源代理转发，或在完成网络边界保护后显式开启远程管理。
- `/admin/` 直接调用同源管理 API，覆盖渠道、模型、路由优先级、Browser API Key、系统审计、完整 Conversation 和 metadata 请求日志。管理密码、session、Provider Key 与新生成的 Browser API Key 都只保存在当前页面内存；页面刷新后需要重新登录。
- 浏览器可直接把短期管理 session 放入 `X-ActionDesign-Api-Key` 调用模型；长期浏览器/CLI Key 通过 access-key rotate 创建或轮换，明文只在该次响应出现，后端仅持久化盐化摘要和末尾掩码。
- 内部服务继续独立使用 `X-ActionDesign-Service-Token`，不得把 Provider Key 或 Service Token 下发给浏览器。
- Profile API 响应只有 `keyConfigured` 与末四位掩码 `secretMasked`，绝不返回 Provider Key。通用 Secret 查询同样只返回状态、类型、时间和掩码。
- 更新时 `apiKey` 为空会保留已有 Key。
- 每个 Profile 包含动态 `provider` 渠道 ID、`displayName`、`protocol=openai|claude`、`priority`、`models[].priority`、`defaultModel`、Base URL、认证方式、超时和启用状态。priority 为 1..10000，数值越大越优先。
- `/models.routingOrder` 返回真实全局顺序。无显式选择时先使用最高优先级渠道的 `defaultModel`；显式 profile/model 始终作为首选。可重试故障先切同 Profile 其余模型（按 priority），再切其他 Profile 的默认模型及模型链。
- 401/403 会跳过当前 Profile 的其余模型再尝试其他凭据；408/429/5xx、timeout/network、context/model unavailable 可降级；参数、能力校验及普通 4xx 不盲重试。流式只允许首个可见 delta 前切换，已输出后返回 `gateway.error` 而不换路由。
- Claude Thinking 透传为 Anthropic `thinking`；OpenAI Chat Completions 按预算映射为 `reasoning_effort`。
- `default-mimo` 和 `default-claude-code` 只是普通环境渠道，不允许删除，也不能改 provider/protocol/Base URL/auth/key；但允许页面修改 priority、defaultModel 和 models 路由 metadata，并通过 environment overlay 持久化。
- 环境变量和受管 profile 的 URL 都会在注册前校验 HTTPS/loopback 与 `ACTIONDESIGN_PROFILE_ALLOWED_HOSTS`，并拒绝 userinfo、query 和 fragment；非法环境 Profile 会使网关启动失败，而不是带着未校验目标运行。
- profile 文件使用 strict schema v4，只保存非敏感 metadata、优先级、环境 overlay 和随机 `secret_ref`；旧 schema v2/v3 会严格校验后一次性迁移到 v4，旧明文 Profile 文件仍拒绝加载。
- 默认使用系统安全 Keyring backend。无桌面会话的 systemd 环境可以显式选择 `file` secret backend；该文件与父目录分别按 `0600`/`0700` 保护，且仍与 Profile metadata 分离，不会回显到 API、审计或请求日志。
- 修改已有 Profile 的 provider、`protocol`、`baseUrl` 或 `authMode` 时必须同时提交新 Key；Secret 与 metadata 更新、删除失败时执行回滚。
- profile 文件默认 `config/actiondesign-api-profiles.json`，写入后尝试设置 `0600`；生产环境应把目录挂载到受保护存储。
- Linux/systemd 部署可配置 SecretService/KWallet/libsecret Keyring，或显式使用权限隔离的 file backend；禁止把 secret 写回 Profile metadata JSON。

### LangSmith

- `POST /api/actiondesign-agent/telemetry/langsmith/runs/batch`

前端发送脱敏 trace batch，后端使用服务端 LangSmith Key 转发。`graph` 和 `interrupt` 映射为 LangSmith `chain` run；LangSmith 失败只计入 rejected，不影响 Agent 业务请求。

## 环境变量

### 网关

```text
ACTIONDESIGN_AGENT_DEFAULT_PROVIDER=mimo
ACTIONDESIGN_AGENT_ALLOW_ORIGINS=https://your-frontend.example
ACTIONDESIGN_SERVICE_AUTH_MODE=required
ACTIONDESIGN_SERVICE_AUTH_TOKEN=<random-secret-at-least-32-characters>
ACTIONDESIGN_AGENT_LOG_ROOT=debug_logs/actiondesign-agent
ACTIONDESIGN_FULL_CONVERSATION_LOG=true
ACTIONDESIGN_FULL_CONVERSATION_LOG_ROOT=logs/actiondesign-agent
ACTIONDESIGN_FULL_CONVERSATION_LOG_MAX_BYTES=104857600
ACTIONDESIGN_REQUEST_LOG_PATH=logs/actiondesign-agent/request-logs.jsonl
ACTIONDESIGN_REQUEST_LOG_MAX_BYTES=10485760
ACTIONDESIGN_REQUEST_LOG_BACKUP_COUNT=5
```

请求日志只保留严格白名单内的 usage/error、hash、长度、耗时和路由元数据；读取历史行时再次清洗。默认单文件 10 MiB、保留 5 个备份，列表与详情使用有界分块反向读取，不会把整个日志文件载入内存。

生产环境必须使用 HTTPS 同源反向代理保护薄网关。代理需要删除客户端伪造的 Service Token Header；浏览器使用短期 session 或独立 Browser API Key，不读取 Provider Key/Service Token。`chat`、流式路由、兼容 Provider 路由、`tool-result`、通用 MCP 和 LangSmith batch 接受有效 `X-ActionDesign-Api-Key`，内部调用也可使用 `X-ActionDesign-Service-Token`；`health/models` 保持公开只读。短期 session 只应放内存或 sessionStorage，不应放 localStorage。

### MiMo

```text
GXP_MIMO_API_KEY=...
GXP_MIMO_MESSAGES_URL=https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages
GXP_MIMO_DEFAULT_MODEL=mimo-v2.5
GXP_MIMO_AUTH_MODE=api-key
GXP_MIMO_TIMEOUT_SECONDS=120
```

### Claude-compatible provider

```text
CLAUDE_CODE_API_KEY=...
CLAUDE_CODE_MESSAGES_URL=https://api.anthropic.com/v1/messages
CLAUDE_CODE_DEFAULT_MODEL=claude-sonnet-4-5
CLAUDE_CODE_MODELS=claude-sonnet-4-5
CLAUDE_CODE_AUTH_MODE=api-key
CLAUDE_CODE_TIMEOUT_SECONDS=120
```

兼容现有 Claude Code/Anthropic 环境变量：

```text
ANTHROPIC_AUTH_TOKEN=...
ANTHROPIC_BASE_URL=https://api.anthropic.com
ANTHROPIC_MODEL=claude-sonnet-4-5
```

仅存在 `ANTHROPIC_AUTH_TOKEN` 时使用 Bearer；`ANTHROPIC_API_KEY` 使用 API-key。显式 `CLAUDE_CODE_*` 配置始终优先。`GET /models` 的 `ready` 只表示 Profile 与 Key 已配置，不代表上游已验证凭据有效；真实验收仍必须调用 chat/stream。流式上游 HTTP 错误只读取错误响应体并统一映射为 `gateway.error`，不会把未缓冲响应异常暴露给前端。

### Profile 管理

```text
ACTIONDESIGN_PROFILE_STORE_PATH=config/actiondesign-api-profiles.json
ACTIONDESIGN_PROFILE_SECRET_STORE_BACKEND=keyring
ACTIONDESIGN_PROFILE_SECRET_STORE_PATH=config/actiondesign-api-profile-secrets.json
ACTIONDESIGN_MANAGED_SECRET_STORE_PATH=config/actiondesign-managed-secrets.json
ACTIONDESIGN_PROFILE_ADMIN_TOKEN=...
ACTIONDESIGN_ADMIN_PASSWORD=ailxhmm12
ACTIONDESIGN_ADMIN_SESSION_TTL_SECONDS=3600
ACTIONDESIGN_ADMIN_ALLOW_REMOTE=false
ACTIONDESIGN_BROWSER_API_KEY_STORE_PATH=config/actiondesign-browser-api-key.json
ACTIONDESIGN_PROFILE_ALLOWED_HOSTS=token-plan-cn.xiaomimimo.com,api.anthropic.com
```

### LangSmith

```text
ACTIONDESIGN_LANGSMITH_ENABLED=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=actiondesign-production
LANGSMITH_WORKSPACE_ID=...
ACTIONDESIGN_LANGSMITH_TIMEOUT_SECONDS=15
```

### 通用 MCP proxy

```text
ACTIONDESIGN_MCP_PROXY_ENABLED=true
ACTIONDESIGN_MCP_TIMEOUT_SECONDS=30
ACTIONDESIGN_MCP_ALLOWED_HOSTS=mcp.example.com
ACTIONDESIGN_MCP_ALLOWED_HTTP_PATHS=/mcp
ACTIONDESIGN_MCP_ALLOWED_EXECUTABLE_PATHS=C:\absolute\path\to\mcp-server.exe
ACTIONDESIGN_MCP_SERVER_STORE_PATH=config/actiondesign-managed-mcp-servers.json
ACTIONDESIGN_MCP_SERVERS_JSON=[{"id":"shared","transport":"streamable_http","url":"https://mcp.example.com/mcp","secretId":"mcp-main","secretBinding":{"target":"header","name":"Authorization","prefix":"Bearer "}}]
```

`ACTIONDESIGN_MCP_SERVERS_JSON` 只提供只读环境来源；管理页面新增的 Server 写入 `ACTIONDESIGN_MCP_SERVER_STORE_PATH`，保存、启停或删除后立即热加载到当前 proxy。启用 MCP proxy 时允许初始零 Server，但 `ACTIONDESIGN_SERVICE_AUTH_MODE` 必须为 `required`，且 Origin 必须显式配置，禁止 `*`。普通调用方不能覆盖 transport、URL、command、Header、环境变量或 Secret；Secret 明文只通过管理员 Secret API 写入安全后端，Server 响应只返回 masked/configured binding 状态。

## 日志与脱敏

- 系统修改审计日志独立写入 `debug_logs/actiondesign-agent/system/admin-audit.jsonl`，记录登录、Profile/Key/priority、Browser API Key 和 seal 操作的 actor/IP、requestId、结果与脱敏详情。
- metadata 请求日志独立写入 `request-logs.jsonl` 并轮转；每个实际 fallback attempt 都记录 attempt、实际 Profile/协议/模型，不保存正文。
- chat、stream、tool-result、MCP 和 LangSmith transport 的每个真实 HTTP 请求都由后端生成独立 canonical UUID v4 `requestId`；调用方传入的 legacy `requestId` 不会成为服务端请求身份，同一次 Provider fallback 的 attempts 复用后端 requestId。
- 完整会话日志默认开启，按 `conversationId` 同步追加到 `logs/actiondesign-agent/yyyyMMdd_HHmmss_{conversationId}.jsonl`，使用 per-conversation lock，目录/文件权限尝试设为 `0700`/`0600`。
- 完整会话日志保存每轮 systemPrompt、prompt、请求参数、每次 fallback、最终响应/工具调用/错误；图片只保存类型、来源、长度和 SHA-256，不保存原始 base64。
- conversation 默认 `open`，仅显式 seal 后为 `completed|aborted`；断线/timeout 只把 turn 标成 cancelled/error，不伪称会话完成。超过大小上限会写 `incompleteReason`。
- Authorization、API Key、cookie、长 base64 和图片 data 会脱敏。
- 所有会话日志在最终持久化写入层再次统一脱敏，调用方不能绕过该边界；SSE 错误文本也会在返回和记录前脱敏。
- Profile secret 不进入普通 API 响应或请求日志。
- 管理员请求日志 API 只返回 requestId、渠道/Profile/协议/模型、状态、耗时、Prompt/System 长度与 SHA-256、图片/工具计数、usage 和错误码，不返回完整输入、图片或业务正文。
- MCP 原始 Secret 和未脱敏正文不进入会话日志；小结果内联保存脱敏副本，大结果写入有大小/总量上限、SHA-256 校验和 `0600` 权限的 content-addressed artifact。普通 tool-result 与 MCP tool-result 均可由 transcript 恢复；artifact 缺失或损坏会明确返回 `incompleteReason`。

## 验证

```bash
python -m compileall -q src/claude_agent_sdk/actiondesign_gateway
PYTHONPATH=src python scripts/verify_actiondesign_gateway_local.py
python -m pytest \
  tests/test_actiondesign_gateway_app.py \
  tests/test_actiondesign_gateway_provider_http.py \
  tests/test_actiondesign_gateway_profile_store.py \
  tests/test_actiondesign_gateway_langsmith.py \
  tests/test_actiondesign_gateway_admin_boundaries.py \
  tests/test_actiondesign_gateway_mcp_proxy.py \
  tests/test_actiondesign_gateway_secret_store.py -q
```

`verify_actiondesign_gateway_local.py` 使用临时回环 Anthropic-compatible HTTP Provider 验证真实网关 `/models`、managed Profile HTTP CRUD、Key 只写不回显、使用该 Profile 的非流式 native tool call、SSE `model.completed`、删除回滚边界和日志不泄漏 Key；Secret Store 仅为进程内存，不访问外部网络、不使用真实凭据、不启动 MCP 或写入持久配置。真实 Provider 凭据、系统 Keyring 服务账户和 LangSmith workspace 仍需部署环境另行验收。

已删除且不应恢复的路由：`/review`、`/topic/classify`、`/knowledge/*`、`/tool-results/*`。MCP 只通过通用 `{serverId}` transport 路由暴露；任何历史 server ID 若仍需使用，也只能作为普通预配置 server，不再拥有专用路由、Schema 或工具白名单。
