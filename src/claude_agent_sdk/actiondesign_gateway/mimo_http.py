from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import HTTPException

from .mimo_stream import iter_sse_events


async def post_mimo(body: dict[str, Any], settings: Any) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout(settings)) as client:
            response = await client.post(
                base_url(settings),
                headers=headers(settings),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise mimo_http_exception(
            502,
            "MIMO_UPSTREAM_ERROR",
            f"MiMo HTTP {exc.response.status_code}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise mimo_http_exception(
            504,
            "MIMO_UPSTREAM_TIMEOUT",
            "MiMo upstream request timed out",
        ) from exc
    except httpx.RequestError as exc:
        raise mimo_http_exception(
            502,
            "MIMO_UPSTREAM_NETWORK_ERROR",
            str(exc) or "MiMo upstream network error",
        ) from exc
    except ValueError as exc:
        raise mimo_http_exception(
            502,
            "MIMO_RESPONSE_INVALID",
            "MiMo upstream response is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise mimo_http_exception(
            502,
            "MIMO_RESPONSE_INVALID",
            "MiMo upstream response JSON is not an object",
        )
    return payload


async def stream_post_mimo(
    body: dict[str, Any],
    settings: Any,
) -> AsyncIterator[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=timeout(settings)) as client:
            async with client.stream(
                "POST",
                base_url(settings),
                headers=headers(settings),
                json=body,
            ) as response:
                response.raise_for_status()
                async for event in iter_sse_events(response.aiter_lines()):
                    yield event
    except httpx.HTTPStatusError as exc:
        raise mimo_http_exception(
            502,
            "MIMO_UPSTREAM_ERROR",
            f"MiMo HTTP {exc.response.status_code}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise mimo_http_exception(
            504,
            "MIMO_UPSTREAM_TIMEOUT",
            "MiMo upstream request timed out",
        ) from exc
    except httpx.RequestError as exc:
        raise mimo_http_exception(
            502,
            "MIMO_UPSTREAM_NETWORK_ERROR",
            str(exc) or "MiMo upstream network error",
        ) from exc


def headers(settings: Any) -> dict[str, str]:
    api_key = setting(settings, "mimo_api_key", "api_key", default="")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MIMO_API_KEY_MISSING",
                "message": "Set GXP_MIMO_API_KEY on the backend",
            },
        )
    result = {
        "Content-Type": "application/json",
        "anthropic-version": setting(
            settings,
            "mimo_anthropic_version",
            default="2023-06-01",
        ),
    }
    auth_mode = str(
        setting(settings, "mimo_auth_mode", "mimo_auth_type", default="api-key")
    ).lower()
    if auth_mode in {"bearer", "authorization", "auth-bearer"}:
        result["Authorization"] = f"Bearer {api_key}"
    else:
        result["api-key"] = api_key
    return result


def base_url(settings: Any) -> str:
    return str(
        setting(
            settings,
            "mimo_api_url",
            "mimo_base_url",
            "mimo_messages_url",
            default="https://api.xiaomimimo.com/anthropic/v1/messages",
        )
    )


def timeout(settings: Any) -> float:
    return float(
        setting(
            settings,
            "mimo_timeout_seconds",
            "mimo_timeout",
            "request_timeout",
            default=120.0,
        )
    )


def mimo_http_exception(
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    )


def setting(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default
