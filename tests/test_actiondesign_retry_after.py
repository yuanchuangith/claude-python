import time
from email.utils import formatdate

import httpx
import pytest
from fastapi import HTTPException

from claude_agent_sdk.actiondesign_gateway.app import (
    _error_detail,
    _sanitized_provider_exception,
)
from claude_agent_sdk.actiondesign_gateway.provider_http import (
    _provider_http_error,
    _retry_after_ms,
)


def test_retry_after_numeric_and_date():
    assert _retry_after_ms('2.5') == 2500
    delay = _retry_after_ms(formatdate(time.time() + 30, usegmt=True))
    assert delay is not None and 28_000 <= delay <= 30_000
    for value in (None, '-1', 'NaN', 'inf', 'invalid', '9' * 200):
        assert _retry_after_ms(value) is None


def test_rate_limit_delay_survives_sanitized_http_and_sse_projection():
    req = httpx.Request('POST', 'https://provider.test/v1/messages')
    res = httpx.Response(429, headers={'Retry-After': '3'}, request=req)
    error = _provider_http_error(httpx.HTTPStatusError('rate limited', request=req, response=res))
    assert error.headers == {'Retry-After': '3'}
    safe = _sanitized_provider_exception(error, ())
    assert safe.detail['code'] == 'PROVIDER_RATE_LIMITED'
    assert _error_detail(safe)['retryAfterMs'] == 3000


def test_permission_errors_never_acquire_retry_wait():
    req = httpx.Request('POST', 'https://provider.test/v1/messages')
    res = httpx.Response(403, headers={'Retry-After': '3'}, request=req)
    error = _provider_http_error(httpx.HTTPStatusError('forbidden', request=req, response=res))
    assert 'retryAfterMs' not in error.detail


@pytest.mark.parametrize('protocol', ['1.0', '1.1'])
@pytest.mark.parametrize('stream', [False, True])
def test_retry_after_endpoint_protocol_negotiation(tmp_path, monkeypatch, protocol, stream):
    from claude_agent_sdk.actiondesign_gateway import app as gateway_app
    from tests.test_actiondesign_gateway_app import (
        chat_payload,
        identity_headers,
        make_client,
    )

    def error():
        return HTTPException(status_code=429, detail={
            'code': 'PROVIDER_RATE_LIMITED', 'message': 'retry later', 'retryable': True,
            'retryAfterMs': 3000, 'failoverScope': 'none',
        }, headers={'Retry-After': '3'})

    async def fail_call(_req, _profile):
        raise error()

    async def fail_stream(_req, _profile):
        raise error()
        yield  # async generator, same interface as the provider stream

    monkeypatch.setattr(gateway_app, 'call_provider', fail_call)
    monkeypatch.setattr(gateway_app, 'stream_provider', fail_stream)
    payload = chat_payload(protocolVersion=protocol, stream=stream)
    client = make_client(tmp_path)
    endpoint = '/api/actiondesign-agent/mimo/chat' + ('/stream' if stream else '')
    response = client.post(endpoint, headers=identity_headers(payload), json=payload)
    if stream:
        import json
        envelopes = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and line[6:] != '[DONE]']
        detail = next(e['payload'] for e in envelopes if e['type'] == 'gateway.error')
    else:
        assert response.status_code == 429
        detail = response.json()['detail']
    assert detail['code'] == 'PROVIDER_RATE_LIMITED'
    assert ('retryAfterMs' in detail) == (protocol == '1.1')
    if protocol == '1.1':
        assert detail['retryAfterMs'] == 3000
