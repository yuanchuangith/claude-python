#!/usr/bin/env python3
"""
HTTP Proxy Server for Claude Agent SDK

前端 ActionDesign AI 助手通过此服务调用 Claude Code。
前端发送 POST 请求，本服务调用 claude-agent-sdk 处理后返回结果。

依赖安装：
    pip install fastapi uvicorn claude-agent-sdk

启动方式：
    uvicorn examples.http_proxy_server:app --host 0.0.0.0 --port 8888

或使用 Python 直接运行：
    python examples/http_proxy_server.py
"""

import os
import re
import json
import time
import logging
from typing import Optional

import anyio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    query,
)

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(name)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# 前端注册的工具白名单 — 只转换这些工具为 [TOOL_CALL]，忽略 Claude Code 内置工具（Bash、Read 等）
DESIGN_TOOLS = {'create_node', 'insert_node', 'create_action', 'list_elements', 'preview_code'}
# 支持图片输入的模型
IMAGE_MODELS = {'mimo-v2.5', 'gpt-5.5'}
# 可用模型列表（名称: 是否支持图片）
AVAILABLE_MODELS = {
    'mimo-v2.5-pro': {'supports_images': False},
    'mimo-v2.5': {'supports_images': True},
    'gpt-5.5': {'supports_images': True},
}
# 模型 → 凭证组映射（凭证组名对应 .env 中的 MODEL_<GROUP>_URL / MODEL_<GROUP>_KEY）
MODEL_CREDENTIAL_MAP = {
    'mimo-v2.5-pro': 'MIMO',
    'mimo-v2.5': 'MIMO',
    'gpt-5.5': 'GPT',
}


def _load_model_credentials() -> dict[str, dict[str, str]]:
    """从环境变量加载模型凭证配置，格式：MODEL_<GROUP>_URL / MODEL_<GROUP>_KEY"""
    creds: dict[str, dict[str, str]] = {}
    for model_name, group in MODEL_CREDENTIAL_MAP.items():
        url = os.environ.get(f"MODEL_{group}_URL", "")
        key = os.environ.get(f"MODEL_{group}_KEY", "")
        if url and key:
            creds[model_name] = {"url": url, "key": key}
        else:
            logger.warning(f"[配置] 模型 {model_name} 的凭证不完整: MODEL_{group}_URL={'有' if url else '缺'}, MODEL_{group}_KEY={'有' if key else '缺'}")
    return creds


MODEL_CREDENTIALS = _load_model_credentials()
logger.info(f"[配置] 已加载模型凭证: {list(MODEL_CREDENTIALS.keys())}")


def _build_options(req_model: str, **kwargs) -> ClaudeAgentOptions:
    """构建 ClaudeAgentOptions，如果指定了模型则自动加载对应凭证"""
    env = {}
    if req_model and req_model in MODEL_CREDENTIALS:
        creds = MODEL_CREDENTIALS[req_model]
        env = {
            "ANTHROPIC_BASE_URL": creds["url"],
            "ANTHROPIC_AUTH_TOKEN": creds["key"],
        }
        logger.info(f"[凭证] 使用模型 {req_model} 的凭证: {creds['url']}")
    elif req_model:
        logger.warning(f"[凭证] 模型 {req_model} 无凭证配置，使用默认")
    return ClaudeAgentOptions(env=env, **kwargs)
# uvicorn access log 太吵，只保留 warning+
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# 预编译正则表达式
_RE_TOOL_CALL = re.compile(r'\[TOOL_CALL\]\s*(\w+)\s*\(', re.DOTALL)
_RE_CODE_BLOCK = re.compile(r'```(?:jsonc?|JSON)?\s*\n?(.*?)\n?```', re.DOTALL)
_RE_FIX_KEY = re.compile(r'(\w+)\s*:')
_RE_FIX_TRAILING_COMMA = re.compile(r',\s*([\]}])')
_RE_FIX_SINGLE_QUOTE = re.compile(r"'([^']*)'\s*:")
_RE_CLEAN_TOOL_CALL = re.compile(r'\[TOOL_CALL\]\s*\w+\s*\([^)]*(?:\([^)]*\)[^)]*)*\)')

app = FastAPI(title="Claude Agent Proxy", version="1.0.0")

# 允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 请求/响应模型
# ============================================================

class ImageInput(BaseModel):
    """图片输入 — base64 或 URL 二选一"""
    media_type: str = "image/png"  # image/png, image/jpeg, image/gif, image/webp
    data: str = ""  # base64 编码的图片数据
    url: str = ""   # 图片 URL


class ClaudeProxyRequest(BaseModel):
    """前端发送的请求格式"""
    prompt: str
    model: str = ""  # 模型名称，为空则用环境变量默认值
    allowed_tools: list[str] = []
    max_turns: int = 99999
    max_tokens: int = 8192
    project_path: str = "src/core/common/ActionDesign"
    auto_repair: bool = True  # 解析失败时是否自动用 AI 修复格式
    tool_names: list[str] = []  # 前端传入的工具白名单，为空则用 DESIGN_TOOLS
    images: list[ImageInput] = []  # 可选图片列表（base64 或 URL）


class ClaudeProxyResponse(BaseModel):
    """返回给前端的响应格式"""
    content: str
    tool_calls: list[dict] = []
    success: bool
    error: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: int = 0


# ============================================================
# JSON 修复
# ============================================================

def _repair_json(text: str) -> str | None:
    """尝试修复 AI 输出的常见 JSON 格式问题"""
    text = text.strip()
    if not text:
        return None

    # 移除注释
    text = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)

    # 单引号 → 双引号（仅 key 部分）
    text = _RE_FIX_SINGLE_QUOTE.sub(r'"\1":', text)

    # 移除尾逗号
    text = _RE_FIX_TRAILING_COMMA.sub(r'\1', text)

    # 尝试直接解析
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # 缺少闭合括号：逐个补全
    for closing in ['}', ']', ']}', '}}', ']]']:
        try:
            json.loads(text + closing)
            return text + closing
        except json.JSONDecodeError:
            continue

    return None


def _extract_balanced_args(content: str, start: int) -> tuple[str, int] | None:
    """从 start 位置提取括号平衡的参数字符串，返回 (args_str, end_pos)"""
    if start >= len(content) or content[start] != '(':
        return None

    depth = 0
    i = start
    while i < len(content):
        ch = content[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return content[start + 1:i], i + 1
        elif ch == '"':
            # 跳过字符串内的括号
            i += 1
            while i < len(content) and content[i] != '"':
                if content[i] == '\\':
                    i += 1  # 跳过转义字符
                i += 1
        elif ch == "'":
            i += 1
            while i < len(content) and content[i] != "'":
                if content[i] == '\\':
                    i += 1
                i += 1
        i += 1

    # 未闭合：返回剩余内容
    return content[start + 1:], len(content)


# ============================================================
# 工具调用提取
# ============================================================

def _extract_actions_from_json(data: dict) -> list[dict]:
    """从包含 actions 的 JSON 对象中提取工具调用"""
    results = []
    if isinstance(data, dict) and "actions" in data:
        for action_key, nodes in data["actions"].items():
            if isinstance(nodes, list):
                for node in nodes:
                    tc = _convert_json_node_to_tool_call(node, action_key)
                    if tc:
                        results.append(tc)
    return results


def extract_tool_calls(content: str) -> list[dict]:
    """从 AI 响应文本中提取工具调用，支持多种格式"""
    tool_calls = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(tc: dict):
        args = tc["arguments"]
        key = (tc["name"], args.get("elementKey") or args.get("element", ""), args.get("title", ""), args.get("actionKey", "") or args.get("targetAction", ""))
        if key not in seen:
            seen.add(key)
            tool_calls.append(tc)

    def _parse_args(args_str: str) -> dict:
        """解析工具参数，带多级修复"""
        # 1. 直接解析
        try:
            return json.loads(args_str)
        except json.JSONDecodeError:
            pass
        # 2. 修复 key 未加引号
        try:
            fixed = _RE_FIX_KEY.sub(r'"\1":', args_str)
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
        # 3. 通用 JSON 修复
        repaired = _repair_json(args_str)
        if repaired:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        logger.debug(f"TOOL_CALL 参数解析失败: {args_str[:200]}")
        return {}

    # 格式 1: [TOOL_CALL] tool_name({...}) — 使用平衡括号匹配
    for match in _RE_TOOL_CALL.finditer(content):
        name = match.group(1)
        # 正则已消耗 '('，end 指向 '(' 之后，回退 1 位让 _extract_balanced_args 从 '(' 开始
        paren_start = match.end() - 1
        result = _extract_balanced_args(content, paren_start)
        if result is None:
            continue
        args_str, _ = result
        args = _parse_args(args_str.strip())
        _add({"name": name, "arguments": args})

    # 格式 2: JSON 代码块中的 actions 数组（带修复）
    for match in _RE_CODE_BLOCK.finditer(content):
        raw = match.group(1).strip()
        data = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            repaired = _repair_json(raw)
            if repaired:
                try:
                    data = json.loads(repaired)
                except json.JSONDecodeError:
                    logger.debug(f"JSON 代码块修复后仍失败: {raw[:200]}")
        if data:
            for tc in _extract_actions_from_json(data):
                _add(tc)

    # 格式 3: 顶层 JSON 对象（带修复）
    try:
        data = json.loads(content.strip())
        for tc in _extract_actions_from_json(data):
            _add(tc)
    except json.JSONDecodeError:
        repaired = _repair_json(content.strip())
        if repaired:
            try:
                data = json.loads(repaired)
                for tc in _extract_actions_from_json(data):
                    _add(tc)
            except json.JSONDecodeError:
                pass

    logger.debug(f"[提取] 格式1(正则)={sum(1 for m in _RE_TOOL_CALL.finditer(content))} matches, "
                 f"最终提取={len(tool_calls)} 个 tool_calls")
    return tool_calls


def _convert_json_node_to_tool_call(node: dict, action_key: str) -> dict | None:
    """将 JSON 格式的节点转换为 tool_call 格式"""
    if not isinstance(node, dict):
        return None

    element_key = node.get("elementKey") or node.get("element")
    if not element_key:
        return None

    if node.get("insertAfterKey") or node.get("anchorKey"):
        return {
            "name": "insert_node",
            "arguments": {
                "element": element_key,
                "params": node.get("paramsValue", {}),
                "actionKey": action_key,
                "insertAfterKey": node.get("insertAfterKey") or node.get("anchorKey"),
                "position": node.get("position", "after"),
                "title": node.get("title", ""),
            }
        }
    else:
        return {
            "name": "create_node",
            "arguments": {
                "element": element_key,
                "params": node.get("paramsValue", {}),
                "actionKey": action_key,
                "title": node.get("title", ""),
                "key": node.get("key"),
            }
        }


# ============================================================
# AI 格式修复
# ============================================================

# 检测内容是否看起来像在尝试输出结构化 actions
_RE_LOOKS_LIKE_ACTIONS = re.compile(
    r'("actions"\s*:|"elementKey"\s*:|"insertAfterKey"\s*:|"paramsValue"\s*:|'
    r'"BeforeSubmit"\s*:|"NullCondition"\s*:|"OpenMessageDialog"\s*:|"ExitAction"\s*:)',
    re.IGNORECASE,
)

# 修复提示词模板
_REPAIR_PROMPT_TEMPLATE = """你的上一条回复中包含了 JSON 数据，但格式有误，前端无法解析。

以下是你的原始回复内容：
---
{content}
---

请将上述内容中的 JSON 数据严格按照以下格式重新输出，不要添加任何额外解释：

```json
{{
  "actions": {{
    "main": [
      {{
        "key": "...",
        "title": "...",
        "elementKey": "...",
        "insertAfterKey": "...",
        "paramsValue": {{ ... }}
      }}
    ],
    "子动作名": [
      {{
        "key": "...",
        "title": "...",
        "elementKey": "...",
        "insertAfterKey": "...",
        "paramsValue": {{ ... }}
      }}
    ]
  }}
}}
```

要求：
1. 只输出 JSON 代码块，不要输出其他文字
2. 确保所有 key 和字符串值都用双引号
3. 确保 JSON 格式完全正确（无尾逗号、括号闭合）
4. 保留原始内容中的所有节点和参数"""


def _looks_like_structured_actions(content: str) -> bool:
    """检测内容是否看起来像在尝试输出结构化 actions"""
    return bool(_RE_LOOKS_LIKE_ACTIONS.search(content))


async def _ai_repair_format(content: str, options: ClaudeAgentOptions) -> str | None:
    """用 AI 修复格式错误的 JSON，返回修复后的内容"""
    try:
        repair_prompt = _REPAIR_PROMPT_TEMPLATE.format(content=content[:4000])
        repair_options = ClaudeAgentOptions(
            max_turns=5,
            allowed_tools=[],
            cwd=options.cwd,
            env=options.env,
            permission_mode=options.permission_mode,
        )

        logger.info(f"[AI修复] 开始修复，原内容长度={len(content)}")
        logger.debug(f"[AI修复] repair_prompt 前500字: {repair_prompt[:500]}")

        result_parts = []
        repair_msg_count = 0
        async for message in query(prompt=repair_prompt, options=repair_options):
            repair_msg_count += 1
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_parts.append(block.text)
                        logger.debug(f"[AI修复] 收到 TextBlock len={len(block.text)}")
            elif isinstance(message, ResultMessage):
                logger.info(f"[AI修复] ResultMessage: subtype={message.subtype}, is_error={message.is_error}, errors={message.errors}")

        repaired = "\n".join(result_parts)
        logger.info(f"[AI修复] 完成: 消息数={repair_msg_count}, 修复后长度={len(repaired)}")
        if repaired and repaired != content:
            logger.info(f"[AI修复] 成功: 原长度={len(content)}, 修复后长度={len(repaired)}")
            return repaired
        else:
            logger.warning("[AI修复] 修复后内容无变化或为空")
    except Exception as e:
        logger.warning(f"[AI修复] 失败: {type(e).__name__}: {e}", exc_info=True)

    return None


# ============================================================
# 多模态 Prompt 构建
# ============================================================

async def build_multimodal_prompt(req: ClaudeProxyRequest):
    """构建多模态 prompt — 支持文本 + 图片"""
    if not req.images:
        # 无图片：使用字符串模式（原有逻辑）
        yield req.prompt
        return

    # 有图片：构造 content list
    content: list[dict] = [{"type": "text", "text": req.prompt}]
    for img in req.images:
        if img.data:
            # base64 图片
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": img.data,
                },
            })
        elif img.url:
            # URL 图片
            content.append({
                "type": "image",
                "source": {
                    "type": "url",
                    "url": img.url,
                },
            })

    logger.info(f"[多模态] 构建 prompt: text_len={len(req.prompt)}, images={len(req.images)}")
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


# ============================================================
# API 端点
# ============================================================

@app.post("/api/ai/claude", response_model=ClaudeProxyResponse)
async def claude_proxy(req: ClaudeProxyRequest):
    """
    代理 Claude Code 调用

    前端 AIAssistant 组件会将用户消息和 system prompt 组合后
    作为 prompt 发送到此端点。服务端调用 claude-agent-sdk 的
    query() 函数处理，收集完整响应后返回。
    """
    start_time = time.time()
    logger.info(f"[非流式] 收到请求: prompt_len={len(req.prompt)}, model={req.model or 'default'}, images={len(req.images)}, max_turns={req.max_turns}, max_tokens={req.max_tokens}, auto_repair={req.auto_repair}, tool_names={req.tool_names}")
    allowed = set(req.tool_names) if req.tool_names else DESIGN_TOOLS
    logger.debug(f"[非流式] prompt 前500字: {req.prompt[:500]}")

    try:
        options = _build_options(
            req_model=req.model,
            max_turns=req.max_turns,
            allowed_tools=req.allowed_tools if req.allowed_tools else [],
            **({"model": req.model} if req.model else {}),
        )

        content_parts: list[str] = []
        total_cost = 0.0
        msg_count = 0
        turn_count = 0

        prompt_input = build_multimodal_prompt(req) if req.images else req.prompt
        async for message in query(prompt=prompt_input, options=options):
            msg_count += 1

            if isinstance(message, AssistantMessage):
                turn_count += 1
                block_types = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        block_types.append("TextBlock")
                        content_parts.append(block.text)
                        logger.debug(f"[非流式] turn={turn_count} TextBlock len={len(block.text)}: {block.text[:200]}")
                    elif isinstance(block, ToolUseBlock):
                        if block.name in allowed:
                            block_types.append(f"ToolUseBlock({block.name})")
                            args_str = json.dumps(block.input, ensure_ascii=False)
                            content_parts.append(f'[TOOL_CALL] {block.name}({args_str})')
                            logger.info(f"[非流式] turn={turn_count} 设计工具: {block.name}, id={block.id}")
                            logger.debug(f"[非流式] turn={turn_count} input: {args_str[:500]}")
                        else:
                            logger.debug(f"[非流式] turn={turn_count} 忽略内置工具: {block.name}")
                logger.info(f"[非流式] turn={turn_count} AssistantMessage blocks={block_types}")

            elif isinstance(message, ResultMessage):
                logger.info(f"[非流式] ResultMessage: subtype={message.subtype}, cost=${message.total_cost_usd:.4f}, "
                           f"duration_ms={message.duration_ms}, is_error={message.is_error}, "
                           f"errors={message.errors}, api_error_status={message.api_error_status}")
                logger.debug(f"[非流式] ResultMessage result_text (前500字): {(message.result or '')[:500]}")
                if message.total_cost_usd:
                    total_cost = message.total_cost_usd

            else:
                logger.debug(f"[非流式] 其他消息类型: {type(message).__name__}")

        content = "\n".join(content_parts)
        duration_ms = int((time.time() - start_time) * 1000)

        logger.info(f"[非流式] 请求完成: {duration_ms}ms, cost=${total_cost:.4f}, "
                    f"总消息数={msg_count}, 总轮次={turn_count}, content_len={len(content)}")

        # 提取工具调用（从文本中解析 [TOOL_CALL] 等格式）
        tool_calls = extract_tool_calls(content)
        logger.info(f"[非流式] 提取到 {len(tool_calls)} 个 tool_calls")
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                logger.debug(f"[非流式] tool_call[{i}]: name={tc['name']}, args_keys={list(tc['arguments'].keys())}")

        # 如果没提取到，但内容看起来像结构化 actions，尝试 AI 修复
        if not tool_calls and req.auto_repair and _looks_like_structured_actions(content):
            logger.info("[非流式] 内容包含 actions 结构但解析失败，尝试 AI 格式修复...")
            repaired = await _ai_repair_format(content, options)
            if repaired:
                tool_calls = extract_tool_calls(repaired)
                if tool_calls:
                    content = repaired
                    logger.info(f"[非流式] AI 修复后成功提取 {len(tool_calls)} 个工具调用")
                else:
                    logger.warning("[非流式] AI 修复后仍无法提取工具调用")

        # 如果提取到了工具调用，从 content 中移除 [TOOL_CALL] 标记
        if tool_calls:
            clean_content = _RE_CLEAN_TOOL_CALL.sub('', content).strip()
        else:
            clean_content = content

        return ClaudeProxyResponse(
            content=clean_content,
            tool_calls=tool_calls,
            success=True,
            error=None,
            cost_usd=total_cost,
            duration_ms=duration_ms,
        )

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"[非流式] 请求失败: {type(e).__name__}: {e}", exc_info=True)

        return ClaudeProxyResponse(
            content="",
            tool_calls=[],
            success=False,
            error=str(e),
            cost_usd=0.0,
            duration_ms=duration_ms,
        )


@app.post("/api/ai/claude/stream")
async def claude_proxy_stream(req: ClaudeProxyRequest):
    """
    SSE 流式代理 — 逐字返回 AI 响应（真正的 token-by-token）

    事件格式：
    data: {"type":"text_delta","content":"你"}
    data: {"type":"text_delta","content":"好"}
    data: {"type":"message_complete","content":"完整内容","tool_calls":[],"success":true,"cost_usd":0.05,"duration_ms":2300}
    """
    start_time = time.time()
    logger.info(f"[SSE] 收到请求: prompt_len={len(req.prompt)}, model={req.model or 'default'}, images={len(req.images)}, max_turns={req.max_turns}, max_tokens={req.max_tokens}, auto_repair={req.auto_repair}, tool_names={req.tool_names}")
    logger.debug(f"[SSE] prompt 前500字: {req.prompt[:500]}")
    allowed = set(req.tool_names) if req.tool_names else DESIGN_TOOLS

    async def event_generator():
        try:
            options = _build_options(
                req_model=req.model,
                max_turns=req.max_turns,
                allowed_tools=req.allowed_tools if req.allowed_tools else [],
                include_partial_messages=True,
                **({"model": req.model} if req.model else {}),
            )

            full_text = ""
            total_cost = 0.0
            msg_count = 0
            turn_count = 0
            stream_event_count = 0

            prompt_input = build_multimodal_prompt(req) if req.images else req.prompt
            async for message in query(prompt=prompt_input, options=options):
                msg_count += 1

                if isinstance(message, StreamEvent):
                    stream_event_count += 1
                    event = message.event
                    event_type = event.get("type", "")

                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        delta_type = delta.get("type", "")
                        if delta_type == "text_delta":
                            text_chunk = delta.get("text", "")
                            if text_chunk:
                                full_text += text_chunk
                                yield f"data: {json.dumps({'type': 'text_delta', 'content': text_chunk}, ensure_ascii=False)}\n\n"
                        elif delta_type == "input_json_delta":
                            # 工具调用的 input JSON delta
                            partial_json = delta.get("partial_json", "")
                            logger.debug(f"[SSE] input_json_delta: {partial_json[:200]}")

                    elif event_type == "content_block_start":
                        content_block = event.get("content_block", {})
                        cb_type = content_block.get("type", "")
                        if cb_type == "tool_use":
                            logger.info(f"[SSE] content_block_start tool_use: id={content_block.get('id')}, name={content_block.get('name')}")
                        elif cb_type == "text":
                            logger.debug(f"[SSE] content_block_start text")

                    elif event_type == "content_block_stop":
                        logger.debug(f"[SSE] content_block_stop index={event.get('index')}")

                    elif event_type == "message_start":
                        logger.debug(f"[SSE] message_start: {event.get('message', {}).get('model', 'unknown')}")

                    elif event_type == "message_delta":
                        stop_reason = event.get("delta", {}).get("stop_reason", "")
                        logger.info(f"[SSE] message_delta stop_reason={stop_reason}")

                    else:
                        logger.debug(f"[SSE] 未知 StreamEvent type={event_type}")

                elif isinstance(message, AssistantMessage):
                    turn_count += 1
                    block_types = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            block_types.append("TextBlock")
                            full_text += block.text
                            yield f"data: {json.dumps({'type': 'text_delta', 'content': block.text}, ensure_ascii=False)}\n\n"
                            logger.debug(f"[SSE] turn={turn_count} TextBlock len={len(block.text)}")
                        elif isinstance(block, ToolUseBlock):
                            if block.name in allowed:
                                block_types.append(f"ToolUseBlock({block.name})")
                                args_str = json.dumps(block.input, ensure_ascii=False)
                                full_text += f'[TOOL_CALL] {block.name}({args_str})'
                                # 不 yield text_delta — tool call 标记不应显示给用户
                                logger.info(f"[SSE] turn={turn_count} 设计工具: {block.name}, id={block.id}")
                                logger.debug(f"[SSE] turn={turn_count} input: {args_str[:500]}")
                            else:
                                logger.debug(f"[SSE] turn={turn_count} 忽略内置工具: {block.name}")
                    logger.info(f"[SSE] turn={turn_count} AssistantMessage blocks={block_types}")

                elif isinstance(message, ResultMessage):
                    logger.info(f"[SSE] ResultMessage: subtype={message.subtype}, cost=${message.total_cost_usd:.4f}, "
                               f"duration_ms={message.duration_ms}, is_error={message.is_error}, "
                               f"errors={message.errors}, api_error_status={message.api_error_status}")
                    logger.debug(f"[SSE] ResultMessage result_text (前500字): {(message.result or '')[:500]}")
                    if message.total_cost_usd:
                        total_cost = message.total_cost_usd

                else:
                    logger.debug(f"[SSE] 其他消息类型: {type(message).__name__}")

            duration_ms = int((time.time() - start_time) * 1000)
            logger.info(f"[SSE] 流结束: {duration_ms}ms, 总消息数={msg_count}, 轮次={turn_count}, "
                       f"stream_events={stream_event_count}, full_text_len={len(full_text)}")

            # 提取工具调用
            tool_calls = extract_tool_calls(full_text)
            logger.info(f"[SSE] 提取到 {len(tool_calls)} 个 tool_calls")

            # AI 格式修复（如果需要）
            if not tool_calls and req.auto_repair and _looks_like_structured_actions(full_text):
                logger.info("[SSE] 内容包含 actions 结构但解析失败，尝试 AI 格式修复...")
                repaired = await _ai_repair_format(full_text, options)
                if repaired:
                    tool_calls = extract_tool_calls(repaired)
                    if tool_calls:
                        full_text = repaired
                        logger.info(f"[SSE] AI 修复后成功提取 {len(tool_calls)} 个工具调用")
                    else:
                        logger.warning("[SSE] AI 修复后仍无法提取工具调用")

            # 清理 content 中的 TOOL_CALL 标记
            if tool_calls:
                clean_content = _RE_CLEAN_TOOL_CALL.sub('', full_text).strip()
            else:
                clean_content = full_text

            # 发送最终结果
            yield f"data: {json.dumps({'type': 'message_complete', 'content': clean_content, 'tool_calls': tool_calls, 'success': True, 'cost_usd': total_cost, 'duration_ms': duration_ms}, ensure_ascii=False)}\n\n"

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[SSE] 请求失败: {type(e).__name__}: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'message_complete', 'content': '', 'tool_calls': [], 'success': False, 'error': str(e), 'cost_usd': 0.0, 'duration_ms': duration_ms}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/config")
async def get_config():
    """返回当前模型配置和可用模型列表"""
    model = os.environ.get("ANTHROPIC_MODEL", "")
    return {
        "model": model,
        "supports_images": model in IMAGE_MODELS,
        "available_models": AVAILABLE_MODELS,
    }


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
