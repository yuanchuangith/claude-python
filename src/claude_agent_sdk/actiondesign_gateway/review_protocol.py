from __future__ import annotations

import json
from typing import Any

from .models import CodeReviewIssue, CodeReviewResponse
from .tool_protocol import extract_tool_calls

REVIEW_SYSTEM_PROMPT = """You are an ActionDesign code reviewer.
Review generated orchestration code and completion evidence.
Return only valid JSON. Do not use markdown fences or explanatory prose.
Do not emit [TOOL_CALL], [TOL_CALL], or any frontend write tool.

Required JSON schema:
{
  "pass": true,
  "summary": "short review summary",
  "issues": [
    {
      "severity": "error",
      "code": "ISSUE_CODE",
      "message": "what failed",
      "actionKey": "optional action key",
      "nodeKey": "optional node key",
      "evidence": "optional evidence",
      "fix": "optional fix guidance"
    }
  ]
}

Use pass=false when blocking issues remain. Use issues=[] only when review passes."""


def review_prompt(prompt: str) -> str:
    return f"{REVIEW_SYSTEM_PROMPT}\n\nReview input:\n{prompt}"


def parse_code_review_response(
    content: str,
    *,
    provider: str,
    model: str,
    duration_ms: int,
    usage: dict[str, Any] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw = str(content or "").strip()
    disallowed_tool_calls = list(tool_calls or []) + extract_tool_calls(raw)
    if disallowed_tool_calls or "[TOOL_CALL]" in raw or "[TOL_CALL]" in raw:
        return _review_response(
            provider=provider,
            model=model,
            passed=False,
            summary="Reviewer returned a frontend tool call instead of JSON.",
            issues=[
                CodeReviewIssue(
                    severity="error",
                    code="REVIEW_TOOL_CALL_NOT_ALLOWED",
                    message="Reviewer endpoints must return JSON only and cannot request frontend tool execution.",
                )
            ],
            success=False,
            code="REVIEW_TOOL_CALL_NOT_ALLOWED",
            error="Reviewer returned a frontend tool call",
            duration_ms=duration_ms,
            usage=usage or {},
            raw=raw,
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _invalid_json_response(provider, model, duration_ms, usage or {}, raw)

    if not isinstance(parsed, dict):
        return _invalid_json_response(provider, model, duration_ms, usage or {}, raw)

    issues_value = parsed.get("issues", [])
    if not isinstance(issues_value, list):
        return _invalid_json_response(provider, model, duration_ms, usage or {}, raw)

    issues = [
        CodeReviewIssue(**issue)
        if isinstance(issue, dict)
        else CodeReviewIssue(message=str(issue))
        for issue in issues_value
    ]
    passed = bool(parsed.get("pass", parsed.get("passed", False)))
    return _review_response(
        provider=provider,
        model=model,
        passed=passed,
        summary=str(parsed.get("summary") or ""),
        issues=issues,
        success=True,
        code=None,
        error=None,
        duration_ms=duration_ms,
        usage=usage or {},
        raw=raw,
    )


def _invalid_json_response(
    provider: str,
    model: str,
    duration_ms: int,
    usage: dict[str, Any],
    raw: str,
) -> dict[str, Any]:
    return _review_response(
        provider=provider,
        model=model,
        passed=False,
        summary="Reviewer did not return valid JSON.",
        issues=[
            CodeReviewIssue(
                severity="error",
                code="REVIEW_RESPONSE_INVALID_JSON",
                message="Reviewer endpoints require a single valid JSON object.",
            )
        ],
        success=False,
        code="REVIEW_RESPONSE_INVALID_JSON",
        error="Reviewer did not return valid JSON",
        duration_ms=duration_ms,
        usage=usage,
        raw=raw,
    )


def _review_response(
    *,
    provider: str,
    model: str,
    passed: bool,
    summary: str,
    issues: list[CodeReviewIssue],
    success: bool,
    code: str | None,
    error: str | None,
    duration_ms: int,
    usage: dict[str, Any],
    raw: str,
) -> dict[str, Any]:
    response = CodeReviewResponse(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        passed=passed,
        summary=summary,
        issues=issues,
        success=success,
        code=code,
        error=error,
        duration_ms=duration_ms,
        usage=usage,
        raw=raw,
    )
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True)
    return response.dict(by_alias=True)
