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
import uuid
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional

import anyio
from fastapi import FastAPI, HTTPException, Request
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
    ToolResultBlock,
    UserMessage,
    query,
)

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(name)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# 日志目录配置
LOG_DIR = Path("./debug_logs")

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


def _generate_log_filename(conversation_id: str) -> str:
    """生成日志文件名：{conversation_id}.json 或 unknown_{timestamp}.json"""
    if conversation_id:
        return f"{conversation_id}.json"
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d-%H-%M-%S")
    return f"unknown_{timestamp}.json"


def save_debug_log(log_data: dict, conversation_id: str):
    """保存调试日志到文件，同一会话追加到同一文件"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    filename = _generate_log_filename(conversation_id)
    filepath = LOG_DIR / filename

    # 读取现有数据或初始化
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            if isinstance(existing_data, list):
                existing_data.append(log_data)
            else:
                existing_data = [existing_data, log_data]
        except (json.JSONDecodeError, Exception):
            existing_data = [log_data]
    else:
        existing_data = [log_data]

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, ensure_ascii=False, indent=2)
    logger.info(f"[日志] 保存至 {filepath}")


# uvicorn access log 太吵，只保留 warning+
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# 预编译正则表达式
_RE_TOOL_CALL = re.compile(r'\[TOOL_CALL\]\s*(\w+)\s*\(', re.DOTALL)
_RE_TYPO_TOOL_CALL = re.compile(r'\[TOL_CALL\]\s*(\w+)\s*\(', re.DOTALL)
_RE_TOOL_CALL_MARKER = re.compile(r'\[(?:TOOL_CALL|TOL_CALL)\]')
_RE_MARKER_PREFIX = re.compile(r'\[T(?:O(?:O(?:L(?:_(?:C(?:A(?:L(?:L)?)?)?)?)?)?)?)?')
_RE_CODE_BLOCK = re.compile(r'```(?:jsonc?|JSON)?\s*\n?(.*?)\n?```', re.DOTALL)


def _find_tool_call_end(text: str) -> int:
    """找到 [TOOL_CALL] func(...) 结束位置，用括号深度追踪。返回 -1 表示未闭合。"""
    paren_start = text.find('(')
    if paren_start == -1:
        return -1
    depth = 0
    for i in range(paren_start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _emit_safe_text(pending: str, chunk: str) -> tuple[list[str], list[dict], str]:
    """处理流式文本块，抑制 [TOOL_CALL] 标记泄漏到前端。

    - 普通文本：立即输出。
    - [TOOL_CALL] 前缀跨 chunk：继续缓冲。
    - 完整工具调用：提取但不输出 UI 文本。
    - 调用方在流结束时用 _flush_pending 处理残余缓冲。

    Returns:
        (text_chunks_to_yield, extracted_tool_calls, updated_pending)
    """
    pending += chunk
    chunks: list[str] = []
    tool_calls: list[dict] = []

    while pending:
        idx = pending.find('[')
        if idx == -1:
            chunks.append(pending)
            pending = ""
        elif idx > 0:
            chunks.append(pending[:idx])
            pending = pending[idx:]
        else:
            m = _RE_TOOL_CALL_MARKER.match(pending)
            if m:
                end = _find_tool_call_end(pending)
                if end == -1:
                    break  # 工具调用未闭合，继续缓冲
                full, _ = _normalize_tool_call_markers(pending[:end])
                parsed = extract_tool_calls(full)
                tool_calls.extend(parsed)
                pending = pending[end:]
            elif _RE_MARKER_PREFIX.match(pending):
                break  # 可能是标记前缀，继续缓冲
            else:
                # 不是标记 — 找到下一个 [ 之前全部输出
                next_bracket = pending.find('[', 1)
                if next_bracket == -1:
                    chunks.append(pending)
                    pending = ""
                else:
                    chunks.append(pending[:next_bracket])
                    pending = pending[next_bracket:]

    return chunks, tool_calls, pending


def _flush_pending(pending: str) -> tuple[str | None, list[dict]]:
    """流结束时处理残余缓冲。

    - 包含可解析的 [TOOL_CALL]：提取到 tool_calls，不发 text_delta。
    - 普通文本或无法形成工具调用：返回给前端。

    Returns:
        (text_to_yield_or_none, extracted_tool_calls)
    """
    if not pending:
        return None, []

    normalized, _ = _normalize_tool_call_markers(pending)
    parsed = extract_tool_calls(normalized)
    if parsed:
        return None, parsed  # 工具调用，不推给前端

    # 普通文本 — 推给前端
    return pending, []
_RE_FIX_KEY = re.compile(r'(\w+)\s*:')
_RE_FIX_TRAILING_COMMA = re.compile(r',\s*([\]}])')
_RE_FIX_SINGLE_QUOTE = re.compile(r"'([^']*)'\s*:")
_RE_CLEAN_TOOL_CALL = re.compile(r'\[TOOL_CALL\]\s*\w+\s*\([^)]*(?:\([^)]*\)[^)]*)*\)')
_RE_DATA_URL = re.compile(r'^data:image/\w+;base64,')


# ============================================================
# 工具调用辅助函数
# ============================================================

def _normalize_tool_call_markers(content: str) -> tuple[str, list[str]]:
    """修复常见的 tool_call 标记拼写错误（如 TOL_CALL → TOOL_CALL）"""
    typo_matches = re.findall(r'\[TOL_CALL\]', content)
    normalized = content.replace('[TOL_CALL]', '[TOOL_CALL]')
    return normalized, typo_matches


def _clean_extracted_tool_calls(content: str) -> str:
    """从内容中移除所有 [TOOL_CALL] 标记及其参数（包括之后的文本）"""
    normalized, _ = _normalize_tool_call_markers(content)
    # 找到第一个 TOOL_CALL 标记的位置，截取之前的内容
    match = re.search(r'\[TOOL_CALL\]', normalized)
    if match:
        return normalized[:match.start()].strip()
    return normalized.strip()


def _normalize_image_data(data: str) -> str:
    """移除 base64 图片的 data URL 前缀"""
    return _RE_DATA_URL.sub('', data)


def _strip_json_actions_content(content: str) -> tuple[str, int]:
    """移除内容中的 JSON actions 代码块，返回 (清理后内容, 移除字符数)"""
    original_len = len(content)
    # 移除 ```json 代码块
    cleaned = re.sub(r'```(?:jsonc?|JSON)?\s*\n?\{.*?"actions"\s*:.*?\}\s*\n?```', '', content, flags=re.DOTALL)
    # 移除裸 JSON actions 对象（支持嵌套大括号）
    def _find_and_strip_json(text: str) -> str:
        result = text
        # 找到所有以 { 开头且包含 "actions" 的 JSON 对象
        while True:
            # 找到 {"actions" 或包含 "actions" 的 JSON 对象的起始位置
            match = re.search(r'\{[^"]*"actions"\s*:', result)
            if not match:
                # 也匹配 {"message":"...","actions":...}
                match = re.search(r'\{[^{]*?"actions"\s*:', result)
            if not match:
                break
            start = match.start()
            # 计算匹配的闭合大括号
            depth = 0
            end = start
            for i in range(start, len(result)):
                if result[i] == '{':
                    depth += 1
                elif result[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if depth != 0:
                break
            result = result[:start] + result[end:]
        return result
    cleaned = _find_and_strip_json(cleaned)
    removed = original_len - len(cleaned)
    return cleaned.strip(), removed


def _transform_tool_args(tool_name: str, args: dict) -> dict:
    """转换工具参数格式（如 Question → ask_user）"""
    if tool_name == "Question" and "questions" in args:
        questions = args["questions"]
        if questions and isinstance(questions, list):
            q = questions[0]
            result = {
                "question": q.get("question", ""),
                "context": q.get("header", ""),
            }
            options = q.get("options", [])
            if options:
                transformed_options = []
                for opt in options:
                    if isinstance(opt, dict):
                        label = opt.get("label", "")
                        desc = opt.get("description", "")
                        transformed_options.append(f"{label}（{desc}）" if desc else label)
                    else:
                        transformed_options.append(str(opt))
                result["suggestedOptions"] = transformed_options
            return result
    return args


def extract_tool_calls_with_diagnostics(content: str) -> tuple[list[dict], dict]:
    """提取工具调用并返回诊断信息"""
    # 先修复拼写错误
    normalized, typo_matches = _normalize_tool_call_markers(content)

    # 统计标记数量
    tool_call_marker_count = len(_RE_TOOL_CALL.findall(normalized))

    # 提取工具调用
    calls = extract_tool_calls(normalized)
    parsed_count = len(calls)

    # 找出解析失败的标记位置
    parse_failed_spans = []
    for match in _RE_TOOL_CALL.finditer(normalized):
        name = match.group(1)
        paren_start = match.end() - 1
        result = _extract_balanced_args(normalized, paren_start)
        if result is None:
            parse_failed_spans.append(match.group(0)[:50])
        else:
            args_str, _ = result
            # 尝试解析参数
            try:
                json.loads(args_str.strip())
            except json.JSONDecodeError:
                repaired = _repair_json(args_str.strip())
                if repaired:
                    try:
                        json.loads(repaired)
                    except json.JSONDecodeError:
                        parse_failed_spans.append(match.group(0)[:50])
                else:
                    parse_failed_spans.append(match.group(0)[:50])

    parse_failed_count = len(parse_failed_spans)

    diagnostics = {
        "raw_length": len(content),
        "tool_call_count": parsed_count,
        "typo_markers": typo_matches,
        "has_typo_tool_calls": len(typo_matches) > 0,
        "tool_call_marker_count": tool_call_marker_count,
        "parsed_tool_calls_count": parsed_count,
        "dropped_duplicate_count": 0,
        "parse_failed_count": parse_failed_count,
        "parse_failed_spans": parse_failed_spans,
    }
    return calls, diagnostics


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


class ToolResultRequest(BaseModel):
    """前端返回工具调用结果的请求格式（camelCase）"""
    conversationId: str = ""       # 会话 ID（也可从 header 取）
    runId: str                     # 本次运行 ID
    turn: int = 0                  # 轮次
    toolCallId: str                # 工具调用 ID（对应 ToolUseBlock.id）
    toolName: str                  # 工具名称
    arguments: dict = {}           # 原始工具参数
    status: Literal["success", "failed"] = "success"
    result: Any = None             # 工具执行结果（任意类型）
    error: str = ""                # 错误信息
    timestamp: str = ""            # 前端时间戳


class ToolResultResponse(BaseModel):
    """工具结果接收响应"""
    success: bool
    message: str
    duplicate: bool = False  # 是否为重复接收


# ============================================================
# Pydantic v1/v2 兼容
# ============================================================

def model_to_dict(model) -> dict:
    """Pydantic v1/v2 兼容的模型转字典"""
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


# ============================================================
# 工具结果存储（幂等 + 超时清理）
# ============================================================

# key: (conversation_id, run_id, tool_call_id) → value: ToolResultRequest + timestamp
_tool_result_store: dict[tuple[str, str, str], dict] = {}
TOOL_RESULT_TTL_SECONDS = 300  # 5 分钟超时


def _cleanup_expired_tool_results():
    """清理过期的工具结果"""
    now = datetime.now()
    expired_keys = [
        key for key, val in _tool_result_store.items()
        if (now - val["received_at"]).total_seconds() > TOOL_RESULT_TTL_SECONDS
    ]
    for key in expired_keys:
        del _tool_result_store[key]
    if expired_keys:
        logger.info(f"[ToolResult] 清理过期记录: {len(expired_keys)} 条")


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
    # 先修复拼写错误
    content = content.replace('[TOL_CALL]', '[TOOL_CALL]')

    tool_calls = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(tc: dict):
        args = tc["arguments"]
        if args is None:
            return  # 解析失败，跳过
        # 不去重 — 保留重复的相同工具调用（如多个 ExitAction）
        tool_calls.append(tc)

    def _parse_args(args_str: str) -> dict | None:
        """解析工具参数，带多级修复。返回 None 表示解析失败。"""
        # 1. 直接解析
        try:
            return json.loads(args_str)
        except json.JSONDecodeError:
            pass
        # 2. 修复 key 未加引号（但不能破坏 URL 中的冒号）
        try:
            # 用正则匹配未加引号的 key：行首或逗号/花括号后的 word 字符再跟冒号
            fixed = re.sub(r'(?<=[{,])\s*(\w+)\s*:', r' "\1":', args_str)
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
        return None

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
_REPAIR_PROMPT_TEMPLATE = """Your previous response contains JSON data with formatting errors that cannot be parsed.

Here is your original response:
---
{content}
---

Repair only the JSON formatting. Do not add, remove, reorder, rename, translate, or infer any fields.
Return only the JSON code block, no extra explanation.

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
    "sub_action_name": [
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

Requirements:
1. Output only the JSON code block, no other text
2. Ensure all keys and string values use double quotes
3. Ensure JSON format is completely correct (no trailing commas, closed brackets)
4. Preserve all nodes and parameters from the original content"""


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
async def claude_proxy(req: ClaudeProxyRequest, request: Request):
    """
    代理 Claude Code 调用

    前端 AIAssistant 组件会将用户消息和 system prompt 组合后
    作为 prompt 发送到此端点。服务端调用 claude-agent-sdk 的
    query() 函数处理，收集完整响应后返回。
    """
    start_time = time.time()
    conversation_id = request.headers.get("X-Conversation-Id", "")
    logger.info(f"[非流式] 收到请求: prompt_len={len(req.prompt)}, model={req.model or 'default'}, images={len(req.images)}, max_turns={req.max_turns}, max_tokens={req.max_tokens}, auto_repair={req.auto_repair}, tool_names={req.tool_names}, conversation_id={conversation_id}")
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
        turns_log: list[dict] = []
        native_tool_calls: list[dict] = []  # 原生工具调用（带 id）
        total_cost = 0.0
        msg_count = 0
        turn_count = 0

        prompt_input = build_multimodal_prompt(req) if req.images else req.prompt
        async for message in query(prompt=prompt_input, options=options):
            msg_count += 1

            if isinstance(message, AssistantMessage):
                turn_count += 1
                block_types = []
                turn_blocks = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        block_types.append("TextBlock")
                        content_parts.append(block.text)
                        turn_blocks.append({"type": "TextBlock", "length": len(block.text), "text": block.text})
                        logger.debug(f"[非流式] turn={turn_count} TextBlock len={len(block.text)}: {block.text[:200]}")
                    elif isinstance(block, ToolUseBlock):
                        if block.name in allowed:
                            block_types.append(f"ToolUseBlock({block.name})")
                            args_str = json.dumps(block.input, ensure_ascii=False)
                            content_parts.append(f'[TOOL_CALL] {block.name}({args_str})')
                            turn_blocks.append({"type": "ToolUseBlock", "original_name": block.name, "id": block.id, "args": block.input})
                            native_tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})
                            logger.info(f"[非流式] turn={turn_count} 设计工具: {block.name}, id={block.id}")
                            logger.debug(f"[非流式] turn={turn_count} input: {args_str[:500]}")
                        else:
                            logger.debug(f"[非流式] turn={turn_count} 忽略内置工具: {block.name}")
                turns_log.append({"turn": turn_count, "type": "AssistantMessage", "blocks": turn_blocks})
                logger.info(f"[非流式] turn={turn_count} AssistantMessage blocks={block_types}")

            elif isinstance(message, UserMessage):
                # 工具调用结果
                if isinstance(message.content, list):
                    result_blocks = []
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            content_str = block.content if isinstance(block.content, str) else json.dumps(block.content, ensure_ascii=False)
                            result_blocks.append({
                                "type": "ToolResultBlock",
                                "tool_use_id": block.tool_use_id,
                                "content": content_str[:1000],
                                "is_error": block.is_error,
                            })
                            logger.info(f"[非流式] ToolResult: tool_use_id={block.tool_use_id}, is_error={block.is_error}, content_len={len(content_str)}")
                    if result_blocks:
                        turns_log.append({"type": "ToolResultMessage", "blocks": result_blocks})

            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                logger.info(f"[非流式] ResultMessage: subtype={message.subtype}, cost=${cost:.4f}, "
                           f"duration_ms={message.duration_ms}, is_error={message.is_error}, "
                           f"errors={message.errors}, api_error_status={message.api_error_status}")
                logger.debug(f"[非流式] ResultMessage result_text (前500字): {(message.result or '')[:500]}")
                if message.total_cost_usd is not None:
                    total_cost = message.total_cost_usd

            else:
                logger.debug(f"[非流式] 其他消息类型: {type(message).__name__}")

        content = "\n".join(content_parts)
        duration_ms = int((time.time() - start_time) * 1000)

        logger.info(f"[非流式] 请求完成: {duration_ms}ms, cost=${total_cost:.4f}, "
                    f"总消息数={msg_count}, 总轮次={turn_count}, content_len={len(content)}")

        # 合并原生工具调用（带 id）和从文本解析的工具调用
        parsed_tool_calls = extract_tool_calls(content)
        logger.info(f"[非流式] 原生 tool_calls: {len(native_tool_calls)} 个, 解析 tool_calls: {len(parsed_tool_calls)} 个")

        # 以原生为主，用 parsed 补充（按 name+arguments 去重）
        tool_calls = list(native_tool_calls)
        seen_keys = {(tc["name"], json.dumps(tc["arguments"], sort_keys=True)) for tc in native_tool_calls}
        for tc in parsed_tool_calls:
            key = (tc["name"], json.dumps(tc["arguments"], sort_keys=True))
            if key not in seen_keys:
                tool_calls.append(tc)
                seen_keys.add(key)

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

        # 保存调试日志
        log_data = {
            "conversation_id": conversation_id,
            "timestamp": datetime.now().isoformat(),
            "request": {
                "mode": "non-stream",
                "prompt_len": len(req.prompt),
                "prompt_preview": req.prompt[:200],
                "model": req.model or "default",
                "max_turns": req.max_turns,
                "max_tokens": req.max_tokens,
                "tool_names": req.tool_names,
                "auto_repair": req.auto_repair,
                "images_count": len(req.images),
            },
            "turns": turns_log,
            "raw_content": content,
            "tool_calls": tool_calls,
            "clean_content": clean_content,
            "result": {
                "success": True,
                "cost_usd": total_cost,
                "duration_ms": duration_ms,
                "total_messages": msg_count,
                "total_turns": turn_count,
                "interrupted": False,
            },
        }
        save_debug_log(log_data, conversation_id)

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
async def claude_proxy_stream(req: ClaudeProxyRequest, request: Request):
    """
    SSE 流式代理 — 逐字返回 AI 响应（真正的 token-by-token）

    事件格式：
    data: {"type":"text_delta","content":"你"}
    data: {"type":"text_delta","content":"好"}
    data: {"type":"message_complete","content":"完整内容","tool_calls":[],"success":true,"cost_usd":0.05,"duration_ms":2300}
    """
    start_time = time.time()
    conversation_id = request.headers.get("X-Conversation-Id", "")
    logger.info(f"[SSE] 收到请求: prompt_len={len(req.prompt)}, model={req.model or 'default'}, images={len(req.images)}, max_turns={req.max_turns}, max_tokens={req.max_tokens}, auto_repair={req.auto_repair}, tool_names={req.tool_names}, conversation_id={conversation_id}")
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
            turns_log: list[dict] = []
            native_tool_calls: list[dict] = []  # 原生工具调用（带 id）
            total_cost = 0.0
            msg_count = 0
            turn_count = 0
            stream_event_count = 0
            _pending_text = ""  # 缓冲区：检测跨 token 的 [TOOL_CALL] 标记
            first_token_time: float | None = None

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
                                safe_chunks, extracted, _pending_text = _emit_safe_text(_pending_text, text_chunk)
                                for chunk in safe_chunks:
                                    if first_token_time is None:
                                        first_token_time = time.time()
                                    yield f"data: {json.dumps({'type': 'text_delta', 'content': chunk}, ensure_ascii=False)}\n\n"
                                if extracted:
                                    native_tool_calls.extend(extracted)
                                    logger.debug(f"[SSE] StreamEvent 中提取到 {len(extracted)} 个工具调用")
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
                    turn_blocks = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            block_types.append("TextBlock")
                            full_text += block.text
                            turn_blocks.append({"type": "TextBlock", "length": len(block.text), "text": block.text})
                            # 不要把内部工具协议标记推给前端 UI
                            if not _RE_TOOL_CALL.search(block.text) and not _RE_TYPO_TOOL_CALL.search(block.text):
                                yield f"data: {json.dumps({'type': 'text_delta', 'content': block.text}, ensure_ascii=False)}\n\n"
                            else:
                                logger.debug(f"[SSE] turn={turn_count} TextBlock 包含 TOOL_CALL 标记，跳过 text_delta 推送")
                            logger.debug(f"[SSE] turn={turn_count} TextBlock len={len(block.text)}")
                        elif isinstance(block, ToolUseBlock):
                            if block.name in allowed:
                                block_types.append(f"ToolUseBlock({block.name})")
                                args_str = json.dumps(block.input, ensure_ascii=False)
                                full_text += f'[TOOL_CALL] {block.name}({args_str})'
                                turn_blocks.append({"type": "ToolUseBlock", "original_name": block.name, "id": block.id, "args": block.input})
                                native_tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})
                                # 不 yield text_delta — tool call 标记不应显示给用户
                                logger.info(f"[SSE] turn={turn_count} 设计工具: {block.name}, id={block.id}")
                                logger.debug(f"[SSE] turn={turn_count} input: {args_str[:500]}")
                            else:
                                logger.debug(f"[SSE] turn={turn_count} 忽略内置工具: {block.name}")
                    turns_log.append({"turn": turn_count, "type": "AssistantMessage", "blocks": turn_blocks})
                    logger.info(f"[SSE] turn={turn_count} AssistantMessage blocks={block_types}")

                elif isinstance(message, UserMessage):
                    # 工具调用结果
                    if isinstance(message.content, list):
                        result_blocks = []
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                content_str = block.content if isinstance(block.content, str) else json.dumps(block.content, ensure_ascii=False)
                                result_blocks.append({
                                    "type": "ToolResultBlock",
                                    "tool_use_id": block.tool_use_id,
                                    "content": content_str[:1000],
                                    "is_error": block.is_error,
                                })
                                logger.info(f"[SSE] ToolResult: tool_use_id={block.tool_use_id}, is_error={block.is_error}, content_len={len(content_str)}")
                        if result_blocks:
                            turns_log.append({"type": "ToolResultMessage", "blocks": result_blocks})

                elif isinstance(message, ResultMessage):
                    cost = message.total_cost_usd or 0.0
                    logger.info(f"[SSE] ResultMessage: subtype={message.subtype}, cost=${cost:.4f}, "
                               f"duration_ms={message.duration_ms}, is_error={message.is_error}, "
                               f"errors={message.errors}, api_error_status={message.api_error_status}")
                    logger.debug(f"[SSE] ResultMessage result_text (前500字): {(message.result or '')[:500]}")
                    if message.total_cost_usd is not None:
                        total_cost = message.total_cost_usd

                else:
                    logger.debug(f"[SSE] 其他消息类型: {type(message).__name__}")

            duration_ms = int((time.time() - start_time) * 1000)
            logger.info(f"[SSE] 流结束: {duration_ms}ms, 总消息数={msg_count}, 轮次={turn_count}, "
                       f"stream_events={stream_event_count}, full_text_len={len(full_text)}")

            # 刷新缓冲区 — 可解析的工具调用提取不推 UI，普通文本推给前端
            if _pending_text:
                flush_text, flush_tc = _flush_pending(_pending_text)
                if flush_tc:
                    native_tool_calls.extend(flush_tc)
                    logger.debug(f"[SSE] 流结束缓冲区提取到 {len(flush_tc)} 个工具调用")
                if flush_text:
                    yield f"data: {json.dumps({'type': 'text_delta', 'content': flush_text}, ensure_ascii=False)}\n\n"
                _pending_text = ""

            # 合并原生工具调用（带 id）和从文本解析的工具调用
            parsed_tool_calls = extract_tool_calls(full_text)
            logger.info(f"[SSE] 原生 tool_calls: {len(native_tool_calls)} 个, 解析 tool_calls: {len(parsed_tool_calls)} 个")

            # 以原生为主，用 parsed 补充（按 name+arguments 去重）
            tool_calls = list(native_tool_calls)
            seen_keys = {(tc["name"], json.dumps(tc["arguments"], sort_keys=True)) for tc in native_tool_calls}
            for tc in parsed_tool_calls:
                key = (tc["name"], json.dumps(tc["arguments"], sort_keys=True))
                if key not in seen_keys:
                    tool_calls.append(tc)
                    seen_keys.add(key)

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

            # 保存调试日志
            log_data = {
                "conversation_id": conversation_id,
                "timestamp": datetime.now().isoformat(),
                "request": {
                    "mode": "sse-stream",
                    "prompt_len": len(req.prompt),
                    "prompt_preview": req.prompt[:200],
                    "model": req.model or "default",
                    "max_turns": req.max_turns,
                    "max_tokens": req.max_tokens,
                    "tool_names": req.tool_names,
                    "auto_repair": req.auto_repair,
                    "images_count": len(req.images),
                },
                "turns": turns_log,
                "raw_content": full_text,
                "tool_calls": tool_calls,
                "clean_content": clean_content,
                "result": {
                    "success": True,
                    "cost_usd": total_cost,
                    "duration_ms": duration_ms,
                    "first_token_ms": int((first_token_time - start_time) * 1000) if first_token_time else None,
                    "total_messages": msg_count,
                    "total_turns": turn_count,
                    "stream_events": stream_event_count,
                    "interrupted": False,
                },
            }
            save_debug_log(log_data, conversation_id)

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[SSE] 请求失败: {type(e).__name__}: {e}", exc_info=True)
            # 异常时也保存日志
            error_log = {
                "conversation_id": conversation_id,
                "timestamp": datetime.now().isoformat(),
                "request": {
                    "mode": "sse-stream",
                    "prompt_len": len(req.prompt),
                    "model": req.model or "default",
                    "max_turns": req.max_turns,
                    "tool_names": req.tool_names,
                },
                "result": {
                    "success": False,
                    "error": f"{type(e).__name__}: {e}",
                    "cost_usd": total_cost if 'total_cost' in dir() else 0.0,
                    "duration_ms": duration_ms,
                    "first_token_ms": int((first_token_time - start_time) * 1000) if first_token_time else None,
                    "total_turns": turn_count if 'turn_count' in dir() else 0,
                    "interrupted": True,
                },
            }
            save_debug_log(error_log, conversation_id)
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


@app.post("/api/ai/claude/tool-result", response_model=ToolResultResponse)
async def receive_tool_result(req: ToolResultRequest, request: Request):
    """
    接收前端返回的工具调用结果

    按 conversation_id + runId + toolCallId 做幂等接收。
    当前阶段仅接收/记录/排队，不主动喂给模型。
    """
    conversation_id = request.headers.get("X-Conversation-Id", "") or req.conversationId
    key = (conversation_id, req.runId, req.toolCallId)
    is_error = req.status == "failed"

    logger.info(f"[ToolResult] 收到: conversation_id={conversation_id}, runId={req.runId}, "
                f"toolCallId={req.toolCallId}, toolName={req.toolName}, status={req.status}")

    # 清理过期记录
    _cleanup_expired_tool_results()

    # 幂等检查
    if key in _tool_result_store:
        logger.info(f"[ToolResult] 重复接收: {key}")
        return ToolResultResponse(success=True, message="已接收过该工具结果", duplicate=True)

    # 存储完整请求体
    _tool_result_store[key] = {
        "request": model_to_dict(req),
        "conversation_id": conversation_id,
        "received_at": datetime.now(),
    }

    # 记录日志
    log_data = {
        "conversation_id": conversation_id,
        "timestamp": datetime.now().isoformat(),
        "type": "tool_result",
        "run_id": req.runId,
        "tool_call_id": req.toolCallId,
        "tool_name": req.toolName,
        "arguments": req.arguments,
        "status": req.status,
        "result": req.result,
        "error": req.error,
        "is_error": is_error,
    }
    save_debug_log(log_data, conversation_id)

    logger.info(f"[ToolResult] 已存储: {key}, 当前缓存 {len(_tool_result_store)} 条")
    return ToolResultResponse(success=True, message="工具结果已接收", duplicate=False)


@app.get("/api/ai/claude/tool-results/{conversation_id}/{run_id}")
async def get_tool_results(conversation_id: str, run_id: str):
    """获取指定会话和运行的所有工具结果（用于调试）"""
    _cleanup_expired_tool_results()
    results = []
    for (cid, rid, _), val in _tool_result_store.items():
        if cid == conversation_id and rid == run_id:
            results.append(val["request"])
    return {"conversation_id": conversation_id, "run_id": run_id, "results": results}


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
