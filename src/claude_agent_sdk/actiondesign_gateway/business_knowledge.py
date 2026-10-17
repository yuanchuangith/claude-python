"""Authenticated, read-only access to one configured local business evidence source.

The gateway forwards evidence; the two browser Agents keep their own loops,
state, tool registries and execution approvals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

PREFIX = "/api/actiondesign-agent/knowledge"


class EvidenceSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    knowledge_base_id: UUID | None = None
    query: str = Field(min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=5, ge=1, le=20)
    max_snippet_chars: int = Field(default=400, ge=200, le=4000)
    result_version: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")


def create_knowledge_router(require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_auth)])

    def configured(request: Request) -> Any:
        settings = request.app.state.settings
        if not settings.localdoc_base_url:
            raise HTTPException(503, detail={"code": "BUSINESS_KNOWLEDGE_NOT_CONFIGURED"})
        return settings

    async def forward(request: Request, method: str, path: str, **kwargs: Any) -> JSONResponse:
        settings = configured(request)
        # Deliberately construct headers instead of forwarding browser credentials,
        # cookies, Origin or Fetch Metadata to the local document service.
        origin = settings.localdoc_base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180, connect=5), trust_env=False, follow_redirects=False,
            ) as client:
                upstream = await client.request(
                    method, origin + path,
                    headers={"Origin": origin, "X-LocalDoc-Request": "1"}, **kwargs,
                )
            if upstream.status_code not in {200, 400, 404, 409, 422}:
                raise ValueError("unexpected evidence response")
            if len(upstream.content) > 8 * 1024 * 1024:
                raise ValueError("evidence response too large")
            payload = upstream.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, detail={"code": "BUSINESS_KNOWLEDGE_UNAVAILABLE"}) from exc

        def source_links(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: origin + item if key in {"path", "preview_url"}
                    and isinstance(item, str) and item.startswith("/documents/")
                    else source_links(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [source_links(item) for item in value]
            return value

        # Citations open the independent management page, not a nonexistent page
        # beneath the gateway or platform origin. No browser API cross-origin fetch.
        if upstream.status_code == 200:
            payload = source_links(payload)
        return JSONResponse(payload, status_code=upstream.status_code, headers={
            "Cache-Control": "no-store", "Pragma": "no-cache",
        })

    @router.get("/config")
    async def config(request: Request) -> JSONResponse:
        settings = request.app.state.settings
        payload: dict[str, Any] = {"enabled": bool(settings.localdoc_base_url)}
        if settings.localdoc_base_url:
            payload.update({
                "knowledgeBaseId": settings.localdoc_knowledge_base_id,
                "processing": {"retrieval": "local", "model": "configured_gateway"},
            })
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.post("/search")
    async def search(value: EvidenceSearch, request: Request) -> JSONResponse:
        settings = configured(request)
        if value.knowledge_base_id and str(value.knowledge_base_id) != settings.localdoc_knowledge_base_id:
            raise HTTPException(403, detail={"code": "BUSINESS_KNOWLEDGE_SCOPE_MISMATCH"})
        payload = value.model_dump(mode="json", exclude_none=True)
        payload["knowledge_base_id"] = settings.localdoc_knowledge_base_id
        return await forward(request, "POST", "/api/agent/search", json=payload)

    @router.get("/documents/{chunk_id}")
    async def document(chunk_id: UUID, request: Request, knowledge_base_id: UUID | None = None) -> JSONResponse:
        settings = configured(request)
        if knowledge_base_id and str(knowledge_base_id) != settings.localdoc_knowledge_base_id:
            raise HTTPException(403, detail={"code": "BUSINESS_KNOWLEDGE_SCOPE_MISMATCH"})
        return await forward(request, "GET", f"/api/agent/knowledge/{chunk_id}", params={
            "knowledge_base_id": settings.localdoc_knowledge_base_id,
        })

    return router
