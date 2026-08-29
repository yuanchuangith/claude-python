# ActionDesign Thin Gateway

## MCP 单一配置源与浏览器直连（2026-08-09，优先于下文旧 MCP 描述）

MCP 配置只有一个 `McpServerStore` 和一个 `ManagedSecretStore`。环境变量提供只读 Server 层；`/admin/`、管理员 API、兼容期 `/mcp-config/*` 与公开 `/mcp-browser/*` 都操作同一个 managed 层，不存在 Browser 专用 Store，也不按“管理员创建/前端创建”拆成两套数据。合并时环境 ID 优先且只读，managed 配置不能覆盖环境 Server；启动发现历史 ID 冲突会 fail closed。

最终数据流：

```text
环境配置（只读） ───────────────┐
后端管理员 /admin 与管理 API ───┼─> McpServerStore + ManagedSecretStore
前端设置页 /mcp-browser/* ──────┘                 │
                                                  ▼
                                    合并并展开浏览器完整配置
                                                  │
                                                  ▼
                              浏览器 initialize / tools/list / tools/call
```

### 无 Client API Key 的浏览器配置接口

以下接口由 `ACTIONDESIGN_PUBLIC_BROWSER_MCP_CONFIG=true` 显式开启，默认关闭。开启时 `ACTIONDESIGN_AGENT_ALLOW_ORIGINS` 必须至少包含一个精确 Origin，不能留空或使用 `*`。接口要求请求携带匹配的 `Origin`，并统一返回 `Cache-Control: no-store`；CORS/Origin 限制只是部署边界，不是用户身份认证。

- `GET /api/actiondesign-agent/mcp-browser/servers`
- `GET /api/actiondesign-agent/mcp-browser/servers/{serverId}`
- `PUT /api/actiondesign-agent/mcp-browser/servers/{serverId}`
- `PUT /api/actiondesign-agent/mcp-browser/servers/{serverId}/enabled`
- `DELETE /api/actiondesign-agent/mcp-browser/servers/{serverId}`
- `POST /api/actiondesign-agent/mcp-browser/servers/{serverId}/connection-test`

公开写接口只接受 `transport=streamable_http`。新增时 `expectedConfigVersion=0`；修改、启停和删除必须提交当前正整数版本，过期页面返回 `409 MCP_CONFIG_VERSION_CONFLICT`。Server 配置变化或绑定 Secret 轮换会令所有受影响 Server 的有效版本各增加一次；环境 Server 的版本也会持久化，但环境配置本身不变。

公开响应严格返回 `source/editable/enabled/transport/executionMode/configVersion/configStatus/errorCode`，以及完整的 `streamableHttp.url`、最终 `headers`、`protocolVersion=2025-06-18` 和超时，不返回内部 `staticHeaders`、`secretBindings`、Secret ID 或部署管理字段。写入端接受前端最终 `headers`；兼容期管理员和旧 `/mcp-config/*` 调用仍可提交 `staticHeaders + secretBindings`，但两种格式不能混用。最终 Header 中的 Authorization、API Key 等敏感值会自动进入 `ManagedSecretStore`，非敏感 Header 留在 Server metadata。Secret 缺失或撤销时返回 `configStatus=invalid`，不返回半完整 Header。HTTP Header 名称按大小写去重，调用方不能配置 `Content-Type`、`Accept`、`MCP-Protocol-Version` 或 `Mcp-Session-Id`。

这是已确认的新安全边界：HTTP MCP Secret 会出现在 Gateway 配置响应、浏览器内存、目标 MCP 请求和开发者工具 Network 面板中。配置响应禁止缓存；Secret 仍不得写入日志、审计正文、Trace、Prompt、checkpoint、localStorage、IndexedDB 或其他持久化前端状态。审计只保存入口、Server/Secret ID、有效版本和不含 Secret 值的配置 hash。

### HTTP 与 stdio 执行分流

- `streamable_http` 固定为 `executionMode=browser_direct`。后端不执行其 `initialize`、`tools/list`、`tools/call`，也没有 Gateway fallback。`/mcp/{serverId}/tools|call` 对 HTTP 返回 `MCP_BROWSER_DIRECT_REQUIRED`。
- HTTP 后端“连接测试”只检查 strict schema、HTTP/HTTPS URL、Header、Secret binding 和版本完整性。真实连接测试由 `/admin/` 或前端设置页的浏览器直接请求目标 URL。浏览器直连元数据允许 HTTP/HTTPS；只有 Gateway transport/proxy 的公网目标继续强制 HTTPS。
- `stdio` 是明确例外，只能由环境或管理员配置，继续使用 `executionMode=gateway_proxy`、受认证 Gateway、executable allowlist、最小环境继承和 Secret env 注入。公开投影只显示 stdio 摘要，不返回 command、cwd、args、env 或 Secret。
- `/admin/` 的 HTTP MCP 编辑器明确显示“浏览器直连”和密钥回传警告；stdio 真实连接测试仍由后端执行。

浏览器在页面加载、Agent run 开始、每个新 Step Snapshot 前以及本页保存/启停/删除后重新读取配置。同一 `serverId + configVersion` 复用会话；新版本创建新 Transport，进行中的请求和已经绑定当前 Step 的旧会话不热替换。删除、停用或 Secret 轮换只影响下一 Step Snapshot。

真实验收必须在目标 MCP 已允许 CPM Origin、POST、自定义认证 Header、`MCP-Protocol-Version`、`Mcp-Session-Id` 并暴露 `Mcp-Session-Id` 的浏览器中完成。聚焦单元测试和后端配置完整性检查不等于真实 CORS、凭据或 MCP 会话验收。

### `gxp-lowcode-readonly` 当前部署状态（2026-08-10）

- Gateway 已注册 managed Server `gxp-lowcode-readonly`，地址为 `http://43.135.137.212:8890/mcp`，`headers={}`、`configVersion=1`、`enabled=true`。
- Gateway 重启后配置仍存在；服务器本机已完成 MCP `initialize` 与 `tools/list`，当前暴露 11 个只读工具。
- 这仍不是浏览器验收：公网 TCP 8890 当前超时，需先开放腾讯云安全组；数据库连接为 `configured=true, reachable=false`，还需数据库白名单/ACL 放行 `43.135.137.212`。两项完成后再从 CPM 页面验证 CORS、Session Header 和真实工具调用。

## MCP 一键容器部署（2026-08-10）

`actiondesign-mcp-deployer.service` 是唯一可访问 Docker、专用构建目录和 MCP Nginx include 的进程。8888 Gateway 只通过 root 管理、`0660 root:actiondesign-gateway` 的 Unix Socket 调用 Deployer；Gateway 用户不加入 `docker` 组，也不能访问 Docker Socket。功能由 `ACTIONDESIGN_MCP_DEPLOYMENTS_ENABLED=true` 显式开启，默认关闭。

- OCI 来源必须是 `registry/path@sha256:<64 hex>`，Registry host 必须在 `ACTIONDESIGN_MCP_DEPLOY_ALLOWED_REGISTRIES`。
- Git 来源必须是 allowlist host 上的 HTTPS/SSH URL 和完整小写 40 位 Commit；构建文件固定为仓库根目录 `Dockerfile`、根上下文和 `mcp-deploy.yaml`，不接受压缩包、自动语言识别、动态 Dockerfile path 或 shell 启动字段。
- HTTP 稳定地址固定为 `https://43-135-137-212.sslip.io/mcp-runtime/{serverId}/mcp`。候选使用五分钟有效的随机 HTTPS path，由 `/admin/` 所在浏览器直接执行 `initialize`、initialized notification 与 `tools/list`，成功后才切换稳定路由和共享 `McpServerStore`。
- 部署生成的 stdio Server 只执行 allowlist 中的固定 `actiondesign-mcp-stdio-bridge`。Bridge 通过另一个受限 Unix Socket 请求 Deployer 创建临时容器；Gateway 不直接运行 Docker。
- `McpDeploymentStore` 只保存 source、Revision、Job、容器和回滚状态；MCP 的有效配置仍只有 `McpServerStore + ManagedSecretStore`。代码 Revision 切换、回滚、启停只令有效 `configVersion` 增加一次。
- 容器无持久卷、允许出网、使用 bridge network、非 root image、只读根文件系统、默认 seccomp、`no-new-privileges`、capabilities 全删除，禁止 privileged、host network/PID/IPC、设备和主机目录挂载。默认资源为 `0.5 CPU / 256 MiB / 128 PIDs`，上限为 `1 CPU / 512 MiB`。

Git 仓库的 manifest 固定示例：

```yaml
apiVersion: actiondesign.mcp/v1
transport: streamable_http
http:
  containerPort: 8000
  mcpPath: /mcp
  healthCheck: {type: http, path: /health}
environment:
  - {name: UPSTREAM_TOKEN, secretId: mcp-upstream, valuePrefix: ""}
resources: {cpu: 0.5, memoryMiB: 256, pids: 128}
startupTimeoutSeconds: 60
```

Secret 暴露范围是强制边界：`browser_header` 才能进入 HTTP 浏览器配置；`deployment_only` 只用于 Git/Registry 和 HTTP 容器环境；`stdio_env` 只用于 stdio 容器环境。明文不进入 source、manifest、Job、DeploymentStore、审计或命令参数。Deployer 每次部署和 stdio 启动前重新读取原子 Secret metadata，以识别管理员后来新增或轮换的 Secret。

管理员 Bearer 接口位于 `/api/actiondesign-agent/admin/mcp-deployments*` 与 `/admin/mcp-deployment-jobs*`，覆盖 202 异步部署、Job 轮询、候选配置、浏览器验证、启停、回滚、脱敏有界日志和补偿式删除。部署和 MCP 配置分别携带 `expectedDeploymentVersion` 与 `expectedConfigVersion`；并发任务返回 `MCP_DEPLOYMENT_BUSY`。Deployer 重启会 fail closed 地回滚所有未完成 Job。

该服务是 ActionDesign 前端 LangGraph Agent 的轻量 Python 网关。它不是 Agent 运行时。

## 文档基线

- 本文按 `2026-08-02` 当前工作树整理；接口事实以 `src/claude_agent_sdk/actiondesign_gateway/` 为准。
- Python 包版本：`0.2.88`。
- FastAPI 应用与 `/openapi.json` 中的 `info.version`：`3.1.0`。
- 文档只描述 Python thin gateway；前端 LangGraph 的规划、Review、工具选择、完成闸门和业务状态机不在本仓实现。

## 职责

- 受控转发动态渠道，并按 Profile 选择 OpenAI Chat Completions 或 Claude/Anthropic Messages 协议。
- 将统一 native tools/image/tool choice 契约转换为对应 Provider 协议，并把 function/tool use 返回规范化为统一响应。
- 输出版本化 SSE envelope。
- 记录 request、response、usage、耗时、错误和前端 tool result。
- 管理 API Profile 与 Key，API 永不回显 secret。
- 脱敏转发前端 LangGraph trace 到 LangSmith。
- 合并环境只读层与 managed 层并投影完整的 browser-direct HTTP MCP 配置；仅对 stdio 提供受认证 Gateway proxy、Secret env 注入和有界 Schema/result 透传。
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

FastAPI API contract version：`3.1.0`。

运行时身份契约：`conversationId` 必须是 canonical lowercase UUID v4，`runId` 必须是 `run_<uuid-v4>`。原生 chat/stream/tool-result 要求 Body 与 Header 中的身份完全一致；服务侧 seal 路由同样要求 UUID v4。管理员会话查询与 seal 为历史日志保留 legacy safe-ID 兼容。OpenAI/Anthropic 兼容接口会在网关内部生成符合该契约的身份。

## 认证与权限边界

| 接口范围 | 凭据 | 说明 |
| --- | --- | --- |
| `/api/actiondesign-agent/health` | 无 | 仅返回运行状态和功能开关，不代表上游 Provider/MCP 已通过真实调用 |
| `/api/actiondesign-agent/models` | Client API Key 或管理员 Bearer | 当前不再是匿名接口 |
| 原生 chat/stream/tool-result/seal、MCP runtime、LangSmith batch | `X-ActionDesign-Service-Token`、`X-ActionDesign-Api-Key`，或 Client API Key | 生产环境必须使用 `ACTIONDESIGN_SERVICE_AUTH_MODE=required`；`disabled` 只用于显式本地开发 |
| `/v1/*`、`/api/actiondesign-agent/mcp-config/*` | Client API Key | 不接受 Provider Key 充当网关凭据 |
| `/api/actiondesign-agent/mcp-browser/*` | 无 Client API Key | 仅在 `ACTIONDESIGN_PUBLIC_BROWSER_MCP_CONFIG=true` 时启用；要求精确 Origin/CORS，响应 `Cache-Control: no-store` |
| `/api/actiondesign-agent/admin/*` 与 `/admin/client-api-keys` | 管理员 Bearer session/token | Client API Key 不能管理 Profile、日志或其他 Client API Key |

Client API Key 可放在 `Authorization: Bearer ...`、`x-api-key: ...` 或 `X-ActionDesign-Api-Key: ...`。若同时提交多个 Header，其值必须完全一致，否则返回 401。Provider Key 与 Service Token 不得下发浏览器。HTTP MCP Secret 是明确例外，会通过 `/mcp-browser/*` 完整配置响应返回浏览器，但不得写入日志、审计正文、Trace、Prompt、checkpoint 或持久化前端存储；stdio Secret 仍不得公开。

## API

### 模型与转发

- `GET /api/actiondesign-agent/models`
- `POST /api/actiondesign-agent/chat`
- `POST /api/actiondesign-agent/chat/stream`
- `POST /api/actiondesign-agent/mimo/chat`
- `POST /api/actiondesign-agent/mimo/chat/stream`
- `POST /api/actiondesign-agent/claude-code/chat`
- `POST /api/actiondesign-agent/claude-code/chat/stream`
- `POST /api/actiondesign-agent/conversations/{conversationId}/seal`

`mimo`/`claude-code` 固定路由保留用于兼容旧前端；动态渠道使用通用 `/chat` 路由。`/models` 返回 `routingOrder`、全局默认 Provider/Model 和每个模型的能力元数据，但 `ready` 只表示 Profile 与 Key 已配置，不等于上游凭据有效。请求中的 `tools` 使用统一 native schema，网关会转换为 OpenAI `type=function` 或 Claude `input_schema`：

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

网关只做通用传输层结构校验，不维护 ActionDesign 画布业务工具白名单。业务工具名称、计划范围、画布目标、写入冲突和完成闸门继续由前端 LangGraph 校验。HTTP MCP 由浏览器使用完整配置直接执行并校验实时 tools/list；stdio 仍由受认证 Gateway 校验并代理，调用方不能覆盖 command、Secret 或 env 绑定。

### OpenAI / Anthropic 兼容接口

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`

规则：

- 三个接口只接受 Client API Key。`/v1/messages` 还要求 `anthropic-version: 2023-06-01`。
- `/v1/chat/completions` 与 `/v1/messages` 复用同一 Profile 路由、能力校验、failover、日志和脱敏边界，不创建第二套 Provider/Agent 实现。
- `model` 可省略；省略时使用后端当前全局默认路由，显式提交时优先尝试所选模型。非流式响应通过 `X-Actual-Profile`、`X-Actual-Model` 和 `X-Route-Attempts` 标识实际路由。
- 支持对应协议的非流式/流式 wire format、tools/tool result、tool choice、最近一条 user 消息中的图片，以及 OpenAI `reasoning_effort` / Anthropic `thinking` 的受支持子集。未知字段按 strict schema 拒绝，不承诺覆盖两家协议的全部参数。
- 兼容请求中的 Client API Key 只用于网关认证；不会被当作 Provider Key、MCP Header 或 Secret 向上游透传。

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

### MCP 浏览器直连与 stdio proxy

- `GET /api/actiondesign-agent/mcp/{serverId}/tools`
- `POST /api/actiondesign-agent/mcp/{serverId}/call`

规则：

- MCP Server 来自同一个环境只读层与 managed store；`/admin/`、管理员 API、兼容路由和 `/mcp-browser/*` 复用同一配置服务。
- `streamable_http` 固定为 `browser_direct`，浏览器配置 URL 可使用 HTTP 或 HTTPS；后端不执行 initialize、tools/list 或 tools/call，`/mcp/{serverId}/tools|call` 返回 `MCP_BROWSER_DIRECT_REQUIRED`。Gateway transport/proxy 的公网目标仍强制 HTTPS。
- `/mcp-browser/*` 只投影完整 URL、最终 Header、协议版本和超时，不返回内部 Header/Secret binding 结构。缺失或撤销的 Secret 令配置 fail closed，不返回半完整 Header；响应禁止缓存。
- `stdio` 强制现存绝对 executable allowlist，只继承最小系统环境并注入当前 Server 显式绑定的 Secret；它是后端真实连接测试及 tools/list/tools/call 的唯一 transport。
- stdio tools/list 的 Draft 2020-12 input/output Schema 与 call result 使用有界 JSON 原样透传。网关不按业务含义选择工具、不解析 SQL、不解释结果，也不包含固定 Skill、工具目录或业务结果 Schema。
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

生产环境必须使用 HTTPS 同源反向代理保护薄网关。代理需要删除客户端伪造的 Service Token Header；浏览器使用短期 session 或独立 Browser API Key，不读取 Provider Key/Service Token。`chat`、流式路由、兼容 Provider 路由、`tool-result`、stdio MCP 和 LangSmith batch 接受有效 `X-ActionDesign-Api-Key`，内部调用也可使用 `X-ActionDesign-Service-Token`；`health` 保持公开，`models` 要求有效 Client API Key 或管理员 Bearer。启用 Feature Flag 后，`mcp-browser/*` 按明确的公开配置边界工作。短期 session 只应放内存或 sessionStorage，不应放 localStorage。

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

### MCP 单一配置源与 stdio proxy

```text
ACTIONDESIGN_MCP_PROXY_ENABLED=true
ACTIONDESIGN_PUBLIC_BROWSER_MCP_CONFIG=true
ACTIONDESIGN_MCP_TIMEOUT_SECONDS=30
ACTIONDESIGN_MCP_ALLOWED_EXECUTABLE_PATHS=C:\absolute\path\to\mcp-server.exe
ACTIONDESIGN_MCP_SERVER_STORE_PATH=config/actiondesign-managed-mcp-servers.json
ACTIONDESIGN_MCP_SERVERS_JSON=[{"id":"shared","transport":"streamable_http","url":"https://mcp.example.com/mcp","secretId":"mcp-main","secretBinding":{"target":"header","name":"Authorization","prefix":"Bearer "}}]
```

`ACTIONDESIGN_MCP_SERVERS_JSON` 只提供只读环境来源；管理页面与 `/mcp-browser/*` 写入同一个 `ACTIONDESIGN_MCP_SERVER_STORE_PATH`。HTTP 保存、启停和 Secret 轮换只更新配置及有效版本，不构建或热替换后端 proxy；新的浏览器会话在下一次配置读取后使用新版本。stdio 修改才重建受认证 proxy。启用公开浏览器配置时 Origin 必须显式配置且禁止 `*`。前端最终 `headers` 写入时，敏感 Header 自动进入统一 Secret Store；公开投影只返回合并后的最终 Header，不暴露内部 binding 结构。Provider/Admin/Service Secret 与 stdio command、env、Secret 仍只返回 masked/configured 状态。

一键部署在 Gateway 与 Deployer 共用的 `.env` 中至少配置：

```dotenv
ACTIONDESIGN_MCP_DEPLOYMENTS_ENABLED=false
ACTIONDESIGN_MCP_DEPLOYER_SOCKET=/run/actiondesign-mcp-deployer/api.sock
ACTIONDESIGN_MCP_DEPLOYER_TOKEN=<至少32字符的随机值>
ACTIONDESIGN_MCP_STDIO_BRIDGE_PATH=/usr/local/libexec/actiondesign-mcp-stdio-bridge
ACTIONDESIGN_MCP_STDIO_BROKER_SOCKET=/run/actiondesign-mcp-deployer/stdio.sock
ACTIONDESIGN_MCP_PUBLIC_BASE_URL=https://43-135-137-212.sslip.io
ACTIONDESIGN_MCP_DEPLOYMENT_STORE_PATH=/var/lib/actiondesign-mcp/deployments.json
ACTIONDESIGN_MCP_DEPLOY_WORK_ROOT=/var/lib/actiondesign-mcp/work
ACTIONDESIGN_MCP_DEPLOY_LOG_ROOT=/var/log/actiondesign-mcp
ACTIONDESIGN_MCP_NGINX_SNIPPET_ROOT=/etc/nginx/actiondesign-mcp-runtime
ACTIONDESIGN_MCP_DEPLOY_ALLOWED_GIT_HOSTS=github.com,gitlab.com
ACTIONDESIGN_MCP_DEPLOY_ALLOWED_REGISTRIES=docker.io,ghcr.io
ACTIONDESIGN_MCP_DEPLOY_MAX_HTTP_RUNTIMES=2
ACTIONDESIGN_MCP_DEPLOY_BUILD_TIMEOUT_SECONDS=900
ACTIONDESIGN_MCP_BROWSER_VALIDATION_TIMEOUT_SECONDS=300
ACTIONDESIGN_MCP_STDIO_MAX_SESSION_SECONDS=600
```

`ACTIONDESIGN_MCP_ALLOWED_EXECUTABLE_PATHS` 还必须精确包含固定 Bridge 路径。先以 `ACTIONDESIGN_MCP_DEPLOYMENTS_ENABLED=false` 安装服务、Socket、Bridge 和 Nginx include，完成 Deployer health、HTTP/stdio canary 后再改为 `true` 并重启 8888 Gateway。

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
