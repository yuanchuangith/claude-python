# Session Context — 2026-05-27

## 修改文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `examples/http_proxy_server.py` | 修改 | 主要业务逻辑文件，新增/修改多处 |
| `tests/test_http_proxy_tool_calls.py` | 修改 | 测试文件，新增 helper 引用 |
| `scripts/pull_debug_logs.sh` | 修改 | SSH 密钥路径修复 + `--latest` 模式 |

---

## 一、`examples/http_proxy_server.py` 变更详情

### 1.1 新增导入

```python
import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal
from fastapi import Request
```

- `Any` → `ToolResultRequest.result` 支持任意类型
- `Literal` → `ToolResultRequest.status` 限定 `"success" | "failed"`
- `Request` → 从请求头读取 `X-Conversation-Id`
- `asyncio`, `timedelta` → 超时清理逻辑

### 1.2 新增正则

```python
_RE_TYPO_TOOL_CALL = re.compile(r'\[TOL_CALL\]\s*(\w+)\s*\(', re.DOTALL)
_RE_DATA_URL = re.compile(r'^data:image/\w+;base64,')
```

### 1.3 新增辅助函数（测试引用）

| 函数 | 签名 | 用途 |
|------|------|------|
| `_normalize_tool_call_markers` | `(content: str) -> tuple[str, list[str]]` | 修复 `TOL_CALL` 拼写错误，返回规范化内容和匹配到的错拼列表 |
| `_clean_extracted_tool_calls` | `(content: str) -> str` | 从内容中移除 `[TOOL_CALL]` 标记及其之后的文本 |
| `_normalize_image_data` | `(data: str) -> str` | 移除 `data:image/...;base64,` 前缀 |
| `_strip_json_actions_content` | `(content: str) -> tuple[str, int]` | 移除 JSON actions 代码块（支持嵌套大括号），返回清理后内容和移除字符数 |
| `_transform_tool_args` | `(tool_name: str, args: dict) -> dict` | 将 `Question` 工具参数转换为 `ask_user` 格式 |
| `extract_tool_calls_with_diagnostics` | `(content: str) -> tuple[list[dict], dict]` | 提取工具调用并返回诊断信息（typo_markers, parse_failed_spans 等） |

### 1.4 `ToolResultRequest` 模型变更

```python
class ToolResultRequest(BaseModel):
    conversationId: str = ""                              # 会话 ID（camelCase）
    runId: str                                            # 本次运行 ID
    turn: int = 0                                         # 轮次
    toolCallId: str                                       # 工具调用 ID（对应 ToolUseBlock.id）
    toolName: str                                         # 工具名称
    arguments: dict = {}                                  # 原始工具参数
    status: Literal["success", "failed"] = "success"      # 限定值
    result: Any = None                                    # 任意类型（dict/list/str/None）
    error: str = ""                                       # 错误信息
    timestamp: str = ""                                   # 前端时间戳
```

**关键变更**:
- `result: dict` → `result: Any` — 接受任意类型
- `status: str` → `status: Literal["success", "failed"]` — 非法值抛 ValidationError
- 字段名全部 camelCase，与前端对齐

### 1.5 `extract_tool_calls` 函数变更

- **不再去重**：保留重复的相同工具调用（如多个 `ExitAction`）
- `_parse_args` 解析失败返回 `None`（而非 `{}`），`_add` 中跳过
- 入口处修复 `[TOL_CALL]` → `[TOOL_CALL]` 拼写错误

### 1.6 tool_calls 生成逻辑变更（合并策略）

**之前**: 原生 ToolUseBlock 和文本解析的 `[TOOL_CALL]` 二选一

**现在**: 合并两者，以原生为主，用 parsed 补充，按 `(name, arguments)` 去重

```python
native_tool_calls: list[dict] = []  # 从 ToolUseBlock 收集（带 id）

# 合并逻辑（非流式和流式端点均有）
parsed_tool_calls = extract_tool_calls(content)
tool_calls = list(native_tool_calls)
seen_keys = {(tc["name"], json.dumps(tc["arguments"], sort_keys=True)) for tc in native_tool_calls}
for tc in parsed_tool_calls:
    key = (tc["name"], json.dumps(tc["arguments"], sort_keys=True))
    if key not in seen_keys:
        tool_calls.append(tc)
        seen_keys.add(key)
```

原生调用包含 `id` 字段（对应 `ToolUseBlock.id`），解析的不包含。

### 1.7 流式端点新增 `X-Conversation-Id` 读取

```python
@app.post("/api/ai/claude/stream")
async def claude_proxy_stream(req: ClaudeProxyRequest, request: Request):
    conversation_id = request.headers.get("X-Conversation-Id", "")
```

之前流式端点不读取请求头。

### 1.8 日志系统变更 — 基于 ConversationId

**文件命名**: `{conversation_id}.json`，无 ID 时 `unknown_{timestamp}.json`

**追加策略**: 同一会话的所有请求追加到同一个 JSON 数组文件

```python
def save_debug_log(log_data: dict, conversation_id: str):
    # 文件存在 → 读取现有数组 → append → 写回
    # 文件不存在 → 新建 [log_data]
```

**日志内容包含**:
- `conversation_id`
- `request` (mode, prompt_len, model, max_turns, tool_names, images_count 等)
- `turns` (每个 turn 的 blocks: TextBlock, ToolUseBlock, ToolResultBlock)
- `raw_content` (完整 AI 响应文本)
- `tool_calls` (合并后的工具调用列表)
- `clean_content` (移除 TOOL_CALL 标记后的文本)
- `result` (success, cost_usd, duration_ms, total_messages, total_turns)

### 1.9 新增 `/api/ai/claude/tool-result` 端点

```python
@app.post("/api/ai/claude/tool-result", response_model=ToolResultResponse)
async def receive_tool_result(req: ToolResultRequest, request: Request):
```

**功能**: 接收前端返回的工具调用结果

**幂等机制**: 按 `(conversation_id, run_id, tool_call_id)` 三元组去重
- 已存在 → 返回 `duplicate=True`
- 不存在 → 存储并记录日志

**超时清理**: `_tool_result_store` 中的记录 5 分钟后过期

**当前阶段**: 仅接收/记录/排队，不主动喂给模型

### 1.10 新增 `ToolResultResponse` 模型

```python
class ToolResultResponse(BaseModel):
    success: bool
    message: str
    duplicate: bool = False
```

### 1.11 新增 `model_to_dict` 兼容函数

```python
def model_to_dict(model) -> dict:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()
```

Pydantic v1/v2 兼容。

### 1.12 新增调试查询端点

```python
@app.get("/api/ai/claude/tool-results/{conversation_id}/{run_id}")
async def get_tool_results(conversation_id: str, run_id: str):
```

获取指定会话和运行的所有工具结果（用于调试）。

### 1.13 `_REPAIR_PROMPT_TEMPLATE` 语言变更

从中文改为英文。测试验证包含 `"Repair only the JSON formatting"` 等关键短语。

---

## 二、`tests/test_http_proxy_tool_calls.py` 变更

### 新增 import

```python
from examples.http_proxy_server import (
    _REPAIR_PROMPT_TEMPLATE,
    _clean_extracted_tool_calls,
    _normalize_image_data,
    _normalize_tool_call_markers,
    _strip_json_actions_content,
    _transform_tool_args,
    extract_tool_calls,
    extract_tool_calls_with_diagnostics,
)
```

### 测试用例（12 个，全部通过）

| 测试 | 验证内容 |
|------|----------|
| `test_extracts_typo_tool_call_marker` | `[TOL_CALL]` 拼写错误能被正确提取 |
| `test_repairs_unquoted_keys_without_corrupting_url_strings` | 未加引号的 key 修复不会破坏 URL 中的冒号 |
| `test_extract_keeps_repeated_mutating_tool_calls` | 重复的相同工具调用不去重 |
| `test_extract_reports_failed_tool_call_parse` | 解析失败的工具调用在 diagnostics 中报告 |
| `test_clean_removes_normalized_tool_call` | `_clean_extracted_tool_calls` 移除 TOOL_CALL 标记 |
| `test_normalize_reports_typo_markers` | `_normalize_tool_call_markers` 返回错拼列表 |
| `test_strip_json_actions_code_block` | 移除 ```json 代码块中的 actions |
| `test_strip_json_actions_raw_block` | 移除 `{"message":"ok","actions":...}` 格式 |
| `test_strip_bare_json_actions_raw_block` | 移除裸 `{"actions":...}` 格式 |
| `test_repair_prompt_is_format_only` | 修复提示词是纯格式修复，不修改内容 |
| `test_question_alias_args_transform_to_ask_user_shape` | Question 工具参数转换为 ask_user 格式 |
| `test_normalize_image_data_strips_data_url_prefix` | 移除 base64 data URL 前缀 |

---

## 三、`scripts/pull_debug_logs.sh` 变更

1. **SSH 密钥路径**: `/f/Desktop/code_new.pem` → `/c/Users/yuanchuan/Desktop/code_new.pem`
2. **新增 `--latest` 模式**: 清空本地日志目录，只拉取远端最新一个文件

---

## 四、API 端点总览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ai/claude` | 非流式代理，返回完整响应 |
| POST | `/api/ai/claude/stream` | SSE 流式代理，逐字返回 |
| POST | `/api/ai/claude/tool-result` | 接收前端工具调用结果（幂等） |
| GET | `/api/ai/claude/tool-results/{conversation_id}/{run_id}` | 查询工具结果（调试） |
| GET | `/config` | 模型配置和可用模型列表 |
| GET | `/health` | 健康检查 |

---

## 五、注意事项

1. `_tool_result_store` 是内存存储，服务重启后清空
2. `tool_call` 的 `id` 字段只有原生 `ToolUseBlock` 有，文本解析的没有
3. `[TOL_CALL]` 拼写错误在提取时自动修复为 `[TOOL_CALL]`
4. 流式和非流式端点共享相同的 tool_calls 合并逻辑
5. 日志文件是 JSON 数组格式，每个请求追加一条记录
