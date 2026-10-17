# ActionDesign Thin Gateway

- 文档角色：Python轻量网关的接口、配置、认证、Secret、MCP和部署事实源。
- 本地知识库新增独立后端服务：[LocalDocRAG](LocalDocRAG/README.md)。FastAPI、Word/OCR、SQLite、Ollama/Qdrant、检索问答和后台任务均在本仓 `LocalDocRAG/backend`；React管理页面位于本仓 `LocalDocRAG/frontend`。其默认端口7860，既有网关8888保持原职责；真实模型/GPU验收状态见[记录](LocalDocRAG/docs/ACCEPTANCE.md)。
- 最后核对：2026-10-17；应用/OpenAPI版本与部署健康必须分别核实，不从旧部署记录推断当前运行状态。
- 2026-10-17：新增下面的共享业务证据只读代理；本机实际 MiMo 路由与验证范围由最新知识报告记录。
- 整个项目首读：[前端Agent主文档](../component/doc/agent-context.md)。业务目标、Graph、工具/Skill/提示词及当前验收状态都从该入口读取；后端不运行ActionDesign业务Agent。
- 当前前后端MCP实现与验证：[gateway-contract-review.md](../component/doc/shared/reports/gateway-contract-review.md)。[交付状态](../component/doc/shared/delivery-status.md)区分开发验证、隔离集成和用户业务验收。
- 本文API、环境变量和部署说明按主题维护；历史部署日期不作为当前验收。

## 两个前端 Agent 共用本地业务证据

两个浏览器 Agent 仍运行各自 Graph/Registry/Prompt，网关不运行业务 Agent 或另建知识聊天循环。实际模型通过 `/api/actiondesign-agent/models` 发现，当前本机路由为 `claude-code / default-claude-code / claude / mimo-v2.6-pro`；Provider 名称不是模型名称，不复制一套 MiMo 密钥。

配置 `ACTIONDESIGN_LOCALDOC_BASE_URL=http://127.0.0.1:7860` 与 `ACTIONDESIGN_LOCALDOC_KNOWLEDGE_BASE_ID=<UUID>` 后，以下接口复用既有 runtime 服务/客户端/本机默认认证及 Origin 策略。未配置两项时 config 返回 `enabled:false`，保留既有 IndexedDB 行为；配置必须同时提供，URL 仅允许 loopback HTTP origin，禁止任意远端、路径或重定向。

| 方法 | 路径（共同前缀 `/api/actiondesign-agent`） | 约定 |
| --- | --- | --- |
| GET | `/knowledge/config` | 未配置时仅`{enabled:false}`；启用时包含knowledgeBaseId及`processing:{retrieval:'local',model:'configured_gateway'}`，不返回底层URL/密钥 |
| POST | `/knowledge/search` | `query,offset?,limit?,max_snippet_chars?,result_version?,knowledge_base_id?`；可选KB必须匹配配置，按LocalDocRAG分页返回摘要/ID/版本；表行保留fullTableId/readHint供直接读取整表 |
| GET | `/knowledge/documents/{chunk_uuid}` | 固定配置KB读取活动原文；可选 `knowledge_base_id` 必须匹配，返回 `KnowledgeDocument` 或 `null` |

代理固定转发 `/api/agent/search`、`/api/agent/knowledge/{UUID}`，设置本地 Origin/请求头，不传浏览器认证/Cookie/Fetch Metadata。上游版本冲突409继续传递，服务不可用或异常响应返回有界502，响应与错误均不缓存。来源的相对预览链接转换为固定本机管理页绝对地址，点击不会误落到网关或平台根路径。此代理没有上传、删除、激活、保存或发布入口。Provider Key 仍由原网关托管。

`evidence_only` 可用于业务理解、编排建议和完整流程问答，不成为已审核操作包，也不授予画布写入、保存发布、页面输入、提交或签名权限。必要证据可进入既有外部 MiMo Provider，不能宣称全离线；原件、解析、Embedding、版本和索引保留在本机。来源与分页见 [LocalDocRAG API](LocalDocRAG/docs/API.md)，质量与边界见[验收报告](../component/doc/business-operation/knowledge-shared-implementation-2026-10-17.md)。

## 原生推理状态协议

`AgentChatMessage` 的assistant和`AgentChatResponse`新增可选`reasoning`。严格schemaVersion 1绑定实际provider/Profile/配置模型/协议，最大512KiB；原生内容不做trim。支持`claude`的thinking/signature与redacted_thinking、`openai`兼容服务的reasoning_content、显式`openai-responses`的reasoning item/summary/encrypted_content。

前后端Provider设置均可选择OpenAI Responses。Base URL由网关规范化到`/v1/responses`，模型发现仍用`/v1/models`，连接测试使用Responses input。业务请求设置`store:false`、`include:["reasoning.encrypted_content"]`；启用思考时发送reasoning effort/summary。旧Profile不自动迁移；外部兼容API仍保留既有`/v1/chat/completions`和`/v1/messages`入口，本轮未新增通用`/v1/responses`服务入口。

Python不拥有业务历史或checkpoint：浏览器保存对应消息和完整调用/结果批次并在后续请求传messages，网关只按本次实际路由校验状态并转换成Provider原生字段。fallback到不同provider/Profile/模型/协议时丢弃不兼容reasoning，普通历史保留。返回模型revision不改变配置model别名的状态绑定。

缺少流终止标记、失败或截断的推理不回传。完整会话日志对严格验证的签名/密文原样保存，避免通用Base64脱敏损坏状态；原生正文命中敏感值/Secret时整状态移除（null），不篡改签名。前端trace只发送元数据。模型未返回状态时不伪造，也不承诺获取可读完整CoT。

本地新增20项通过，扩展178通过/5已有失败/2既有跳过；已有失败在隔离的修改前协议副本复现。需重启更新后的gateway并加载新前端，真实Provider接受行为与正式页面验收未执行。完整前端续接/存储/压缩边界和证据见[实施报告](../component/doc/shared/reports/agent-reasoning-continuity-report.md)。

## MCP 单一配置源与浏览器直连

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

本小节是2026-08-10历史现场记录，不是当前服务健康结论。本次2026-10-07只验证隔离网关与真实GXP stdio的41工具发现/connection_status，不据此修改正式远端配置或确认旧8890服务可达。

- Gateway 已注册 managed Server `gxp-lowcode-readonly`，地址为 `http://43.135.137.212:8890/mcp`，`headers={}`、`configVersion=1`、`enabled=true`。
- Gateway 重启后配置仍存在；服务器本机已完成 MCP `initialize` 与 `tools/list`，当前暴露 11 个只读工具。
- 这仍不是浏览器验收：公网 TCP 8890 当前超时，需先开放腾讯云安全组；数据库连接为 `configured=true, reachable=false`，还需数据库白名单/ACL 放行 `43.135.137.212`。两项完成后再从 CPM 页面验证 CORS、Session Header 和真实工具调用。

## MCP 一键容器部署

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

- 当前接口事实以 [actiondesign_gateway](src/claude_agent_sdk/actiondesign_gateway) 为准；历史部署与当前本地改动分别取证，不能只凭OpenAPI版本确认已上线。
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
- actiondesign_gateway 不提供知识库、embedding、Markdown index 或 Qdrant；这些能力由本仓独立的[LocalDocRAG服务](LocalDocRAG/README.md)负责。
- 不接受任意 URL 转发；受管 Profile host 必须在 allowlist 中，OpenAI `/v1/chat/completions`、Responses `/v1/responses` 或 Claude `/v1/messages` path 由网关按协议规范化。
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

本机开发可显式设置ACTIONDESIGN_LOCAL_DEFAULT_CLIENT_API_KEY与ACTIONDESIGN_LOCAL_DEFAULT_CLIENT_ORIGIN，供未携带凭据的运行时请求在网关内代用专用Client Key。仅允许loopback连接、精确且已在allow_origins中的Origin；默认关闭，Key逐请求验证并可撤销，不回传浏览器、不用于管理员接口或/v1，也不扩大其他部署的认证范围。前端登录/更换凭据后重新加载models；401应提示身份验证。

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

- `GET /api/actiondesign-agent/mcp/{serverId}/connection-config`
- `GET /api/actiondesign-agent/mcp/{serverId}/tools`
- `POST /api/actiondesign-agent/mcp/{serverId}/call`

规则：

- connection-config复用现有_require_service_auth支持的服务Token、Client Key、管理会话与浏览器Key，精确检查Origin并返回Cache-Control: no-store。仅HTTP返回最终URL/Header、协议与超时；stdio不返回command/cwd/args/env或Secret。该接口与Client Key专用mcp-config路由分开，前端运行时不以管理会话调用后者。
- HTTP最终凭据只保留在浏览器执行内存，不进入安全管理摘要、Graph/Step Snapshot、Prompt、trace、checkpoint或持久化存储。新HTTP步骤读新配置，原步骤保留原URL/Header/会话；HTTP失败不回退网关，不重放已发出的调用。
- stdio tools/list和call接受X-MCP-Config-Version，新版前端始终提交版本。过期执行前返回409/MCP_CONFIG_VERSION_CONFLICT；非法值返回422/MCP_CONFIG_VERSION_INVALID。执行中请求持有原proxy，历史无版本客户端保持兼容；新过期请求不能读取新Secret或重用历史Secret。
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
- `/admin/` 直接调用同源管理 API，覆盖渠道、模型、路由优先级、Browser API Key、系统审计、完整 Conversation 和请求日志。页面内存保存 Bearer；登录另设 HttpOnly、SameSite=Strict、仅作用于 `/api/actiondesign-agent/admin/session` 的会话 Cookie（HTTPS 时 Secure），刷新页面通过同源 `POST /admin/session` 恢复登录，使用期间定时续期。此接口要求精确同源 Origin 和 `X-ActionDesign-Admin-Session: 1`，不因 CORS allowlist 接受跨域 Cookie 认证；普通管理员 API 仍只接受 Bearer。页面不向 localStorage/sessionStorage 保存凭据。
- 默认空闲过期时间为 `ACTIONDESIGN_ADMIN_SESSION_TTL_SECONDS=3600`；本机启动配置为 `86400`（24 小时），页面打开期间最多每 10 分钟续期，回到页面时也续期。`DELETE /admin/session` 撤销服务器会话并删除 Cookie；会话仍由网关进程内存保存，服务重启或真实过期后需重新登录。仅真正的管理认证失效会清空页面，其他业务请求的 401 和网络波动不再直接登出。
- 浏览器可直接把短期管理 session 放入 `X-ActionDesign-Api-Key` 调用模型；长期浏览器/CLI Key 通过 access-key rotate 创建或轮换，明文只在该次响应出现，后端仅持久化盐化摘要和末尾掩码。
- 内部服务继续独立使用 `X-ActionDesign-Service-Token`，不得把 Provider Key 或 Service Token 下发给浏览器。
- Profile API 响应只有 `keyConfigured` 与末四位掩码 `secretMasked`，绝不返回 Provider Key。通用 Secret 查询同样只返回状态、类型、时间和掩码。
- 更新时 `apiKey` 为空会保留已有 Key。
- 每个 Profile 包含动态 `provider` 渠道 ID、`displayName`、`protocol=openai|claude|openai-responses`、`priority`、`models[].priority`、`defaultModel`、Base URL、认证方式、超时和启用状态。priority 为 1..10000，数值越大越优先。
- `/models.routingOrder` 返回真实全局顺序。无显式选择时先使用最高优先级渠道的 `defaultModel`；显式 profile/model 始终作为首选。可重试故障先切同 Profile 其余模型（按 priority），再切其他 Profile 的默认模型及模型链。
- 401/403 会跳过当前 Profile 的其余模型再尝试其他凭据；408/429/5xx、timeout/network、context/model unavailable 可降级；参数、能力校验及普通 4xx 不盲重试。流式只允许首个可见 delta 前切换，已输出后返回 `gateway.error` 而不换路由。
- Claude Thinking 透传为 Anthropic `thinking`：`enabled`保留预算，显式或默认`disabled`发送`{"type":"disabled"}`，不再省略后交由上游默认决定。main/review及流式/非流式共用此转换；同路由历史signed/redacted thinking原样保留。此修复不改变请求schema默认值或其他协议；上游是否接受并实际关闭思考仍须真实Provider复验，不能仅由`supportsThinking:false`或本地测试断言。OpenAI Chat Completions继续按启用预算映射为 `reasoning_effort`。
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

图节点或工具trace事件不一定关联HTTP模型请求，因此事件级`requestId`可省略；提供时仍必须为canonical UUID v4。批次HTTP请求的`requestId`及Header/Body身份校验仍必填，不能把批次请求身份复制成事件所对应的模型请求身份。此项修正对齐前端TraceEnvelope 1.0的既有可选字段，API版本维持3.1.0；已有网关进程需重启加载新模型定义。

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

### 系统日志页签（本机明文调试，2026-10-16）

`/admin/` 新增独立“系统日志”页签，管理员登录后可以查看、筛选和清空请求与返回内容。`ACTIONDESIGN_SYSTEM_LOG_ENABLED=true` 开启采集，默认关闭；当前本机已开启。存储由 `ACTIONDESIGN_SYSTEM_LOG_PATH` 指定，默认 `logs/local/system-logs.sqlite3`，重启保留，清空删除全部系统调试记录并回收数据库空间。

- 记录网关收到的 HTTP 请求（包括认证失败、校验失败、静态资源和预检）：方法、完整 URL、重复查询参数、原始 Header、完整 Body、状态码、耗时和返回 Header/Body。
- 记录网关实际发给模型服务的每次 HTTP 调用，包括协议转换后的完整参数、每次 fallback、模型发现和连接测试，并用 `parentId` 关联客户端请求。支持 OpenAI Chat Completions、Responses 和 Claude；网络错误也保留记录。
- 响应继续实时转发；SSE 正文在请求结束后完整保存，断开或失败标记不完整；压缩内容解码后可读，并保留线上字节，二进制使用 Base64。正文不按原有 metadata 日志的长度或深度裁剪。
- 支持全文（含请求和返回正文）、方向、方法、HTTP 状态、requestId/conversationId/runId、渠道、模型、时间范围和仅错误筛选；列表分页，默认每 5 秒刷新，详情支持刷新、完整 JSON 复制和关联模型调用。
- 这是用户明确要求的独立明文调试存储：Header 中的 Authorization/Cookie、API Key、密码、图片和其他参数原样保留，仅管理员接口可读取，响应 `Cache-Control: no-store`。以下脱敏规则仍适用于原有请求摘要、会话、审计和 Trace，不适用于此显式开启的系统调试日志。
- 日志查询、详情和清空接口自身不采集，避免日志正文递归膨胀。清空不改变原有会话/审计日志；尚在执行的请求结束后会重新记录。开启前的完整原文无法补录；浏览器直连 MCP、浏览器内部事件及 stdio 进程内部日志不经过此 HTTP 采集器。

管理 API：`GET /api/actiondesign-agent/admin/system-logs`、`GET /api/actiondesign-agent/admin/system-logs/{id}`、`DELETE /api/actiondesign-agent/admin/system-logs`。筛选参数为 `q/direction/method/status/requestId/conversationId/runId/provider/model/parentId/startedAt/endedAt/errorsOnly`，分页 `limit/offset`，时间必须带时区。

- 系统修改审计日志独立写入 `debug_logs/actiondesign-agent/system/admin-audit.jsonl`，记录登录、Profile/Key/priority、Browser API Key 和 seal 操作的 actor/IP、requestId、结果与脱敏详情。
- metadata 请求日志独立写入 `request-logs.jsonl` 并轮转；每个实际 fallback attempt 都记录 attempt、实际 Profile/协议/模型，不保存正文。
- Agent运行时的chat、stream、tool-result、MCP和LangSmith真实HTTP调用由前端生成独立canonical UUID v4 requestId，网关在Header/Body边界校验并回显，不替换调用方身份；correlationId保持同一逻辑调用，重试使用新的requestId及retryOf/parentRequestId。兼容入口的内部请求映射遵守对应协议；同一次Provider fallback的attempts复用该HTTP requestId。
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

已删除且不应恢复的旧业务循环/知识管理路由：`/review`、`/topic/classify`、旧 `/knowledge/*`、`/tool-results/*`。2026-10-17 新增的 `/knowledge/config`、`/knowledge/search`、`/knowledge/documents/{UUID}` 只代理固定本机业务原文，属于本文开头明确的只读例外，不恢复旧知识管理或后端Agent循环。MCP只通过通用 `{serverId}` transport路由暴露；历史server ID若仍需使用，也只能作为普通预配置server，不再拥有专用路由、Schema或工具白名单。

## 当前开发验证与上线边界

MCP建设阶段相关后端51项通过；前端当阶段91个测试文件通过，后续编排/提示词审查扩展到93/93，生产组件构建成功。TypeScript仍有38项既有诊断，与基线一致，不是全部类型通过。本次文档归整没有重新执行后端全量测试。

实际浏览器在隔离当前网关验证HTTP认证、会话、调用、版本切换和旧/新步骤隔离；真实GXP stdio发现41工具并完成connection_status。传输验证不代表业务诊断、正式部署或用户阶段4验收。正式MCP/Provider配置未修改；旧运行进程需要重启才能提供新connection-config接口。

前端SDK为1.32.1；工具/Skill/提示词与业务验收记录在[Agent主文档](../component/doc/agent-context.md)及其索引中。HTTP最终Header、stdio_env绑定、配置写入/启停/删除的expectedConfigVersion和后端单一配置源须与前端同版本使用。

## 持续上下文调用与预算（协议1.1）

### 增量等待协议与验证（2026-10-12）

- Provider HTTP 429解析Retry-After秒数或HTTP-date，保留脱敏retryAfterMs及等待Header；非法、负数、非有限值不返回等待。权限错误不附可重试等待。
- /chat和/chat/stream协议1.1透传retryAfterMs，协议1.0保持原错误字段形状；前端执行可取消等待、有界重试和期限，后端不创建业务重试/工具状态机。
- 分支、输入队列、安全artifact、空闲压缩、经验去重、预算校准和未知写核对仍由前端实现，后端没有增加业务存储或第二套Agent。
- 定向reasoning_continuity/request_accounting/retry_after共53通过（Retry-After7项含HTTP/SSE×新旧协议）；扩大313通过/7既有失败/2跳过。7项在隔离当前src、仅替换本轮app.py/provider_http.py为HEAD后全部复现，Responses保留。源文件20项既有lint与基线一致、新测试lint/编译通过，没有新增真实Provider/正式页面验收或服务重启。详细边界见[增量实现报告](../component/agent-codex-incremental-implementation-report.md)。

### 完整请求预算与用量

- 网关仍只转换协议/路由/预算/用量，不拥有业务Graph、摘要策略、经验库或任务状态。前端复用/chat、/chat/stream，taskPurpose为main/review/compaction/memory/intent；辅助请求仅本次结构化结果工具，沿用当前模型和既有fallback。
- /models协商supportedProtocolVersions=1.0/1.1。旧响应保持原形；新版保留原usage，新增normalizedUsage（输入/输出/缓存读取/缓存创建/推理/总量，未知null）和requestBudget（估算输入、可用输入、输出预留、窗口、实际Provider/Profile/model/protocol、isEstimate=true）。缓存/推理为分类或子集，不能重复累计到total。
- [request_budget.py](src/claude_agent_sdk/actiondesign_gateway/request_budget.py)复用实际HTTP发送body，覆盖system/messages/native tools Schema/images及输出预留；每个候选路由发送前独立检查。模型名称取实际body.model，兼容req.model为空的既有默认模型路径。PNG/GIF/JPEG尺寸与协议估算图片，未知尺寸保守估计；opaque密文不按字符计真实推理token，业务字段名data/signature/encrypted_content不能因此漏算。
- 超预算HTTP413，detail.code=CONTEXT_BUDGET_EXCEEDED，1.1附实际路由预算；旧协议不增加budget响应字段。主/Review/辅助实际路由均返回归一化用量。前端决定压缩及有界重试，默认累计请求硬上限null；后端没有另设业务累计上限。
- 特殊图片/推理估计只在实际协议的原生消息与内容块边界生效，不递归传播到业务工具参数、Schema或示例；业务对象的type=reasoning/redacted_thinking/image不能绕过预算。
- 新预算26项通过（Provider HTTP/Schema/图片/缓存/缺usage/实际fallback/旧新响应及SSE，同名业务type隔离，以及最终响应脱敏/来源ID/真实HTTP和SSE接线）；最新扩大230通过/6既有失败/2跳过，6项隔离旧源码复现。2026-10-11已使用claude协议mimo-v2.6-pro真实调用；发现并修复camelCase token用量误脱敏和Graph来源ID中的user:0被误当连接用户名。凭据仍脱敏，来源ID仅保护严格run_UUID:user:数字，已配置Secret精确替换仍执行。首轮用8889隔离store/log加载新代码复验；用户随后重启8888，HTTP新旧协议/SSE/预算拒绝/前端经验5/5定向实测通过，未部署正式业务页面。前端完整报告：[agent-continuity-recovery-report.md](../component/doc/shared/reports/agent-continuity-recovery-report.md)。
