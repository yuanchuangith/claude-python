from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .profile_store import ProviderProfile
from .provider_http import call_provider, stream_provider


async def call_mimo(req: Any, profile: ProviderProfile) -> dict[str, Any]:
    return await call_provider(req, profile)


async def stream_mimo(req: Any, profile: ProviderProfile) -> AsyncIterator[dict[str, Any]]:
    async for event in stream_provider(req, profile):
        yield event
