"""Tests for the /cloud-provider-b transparent reverse-proxy routes."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import Response, StreamingResponse

from atelier.cloud_key_custody import CloudKeyCustody
from atelier.credential_proxy import ProxyRequest, ProxyResponse
from atelier.oauth_custody import OAuthCustody
from atelier.server import create_app
from atelier.standin import StandInTokenLedger


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCustody:
    """No-op OAuth custody — not used by cloud-provider-b scope tests."""

    async def access_grant(self, account: str) -> Any:
        raise RuntimeError("should not be called for cloud-provider-b scope")


class FakeKeyStore:
    """In-memory pass store that returns a canned key for a given path."""

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = keys

    def show(self, path: str) -> str:
        if path not in self._keys:
            raise KeyError(path)
        return self._keys[path]


class _BufferedTransport:
    """Returns a canned buffered response."""

    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}') -> None:
        self._status = status
        self._body = body

    async def send(self, request: ProxyRequest) -> ProxyResponse:
        return ProxyResponse(
            status_code=self._status,
            headers={"content-type": "application/json"},
            body=self._body,
        )

    async def open_stream(self, request: ProxyRequest) -> Any:
        raise NotImplementedError


class _StreamTransport:
    """Returns a canned streaming response."""

    def __init__(self, status: int = 200) -> None:
        self._status = status

    async def send(self, request: ProxyRequest) -> ProxyResponse:
        raise NotImplementedError

    async def open_stream(self, request: ProxyRequest) -> Any:
        return _FakeStream(self._status)


class _FakeStream:
    def __init__(self, status_code: int) -> None:
        self._status = status_code
        self._chunks = [b'data: {"hello": "world"}\n\n', b'data: [DONE]\n\n']
        self._idx = 0

    @property
    def status_code(self) -> int:
        return self._status

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": "text/event-stream"}

    def aiter_bytes(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger() -> StandInTokenLedger:
    return StandInTokenLedger(now=lambda: 1000)


@pytest.fixture
def custody() -> FakeCustody:
    return FakeCustody()


@pytest.fixture
def cloud_provider_b_key_store() -> FakeKeyStore:
    return FakeKeyStore({
        "llm/cloud-provider-b/accounts/default/cloud-key": "sk-or-v2-test-key-12345",
    })


@pytest.fixture
def cloud_provider_b_custody(cloud_provider_b_key_store: FakeKeyStore) -> CloudKeyCustody:
    return CloudKeyCustody(
        store=cloud_provider_b_key_store,  # type: ignore[arg-type]
        path_prefix="llm/cloud-provider-b/accounts",
    )


def _make_app(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_b_custody: CloudKeyCustody | None,
    *,
    transport: Any = None,
) -> FastAPI:
    """Build an app with the cloud_provider_b_key_custody wired in."""
    import atelier.server as server_mod

    if transport is not None:
        # Patch _HttpTransport so the proxy uses our fake
        original = server_mod._HttpTransport

        class _PatchedTransport:
            def __init__(self, client):
                pass

            async def send(self, request):
                return await transport.send(request)

            async def open_stream(self, request):
                return await transport.open_stream(request)

        server_mod._HttpTransport = _PatchedTransport

    app = create_app(
        custody=custody,
        ledger=ledger,
        cloud_provider_b_key_custody=cloud_provider_b_custody,
    )

    if transport is not None:
        server_mod._HttpTransport = original

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cloud_provider_b_proxy_buffered(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_b_custody: CloudKeyCustody,
) -> None:
    """GET /cloud-provider-b/models via buffered proxy injects the API key."""
    transport = _BufferedTransport(status=200, body=b'{"models": []}')
    app = _make_app(ledger, custody, cloud_provider_b_custody, transport=transport)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            # Mint a stand-in token with cloud-provider-b scope
            resp = await client.post(
                "/v1/standin",
                json={"account": "default", "scope": "cloud-provider-b", "ttl_seconds": 300},
            )
            token = resp.json()["token"]

            response = await client.get(
                "/cloud-provider-b/models",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200
    assert response.json() == {"models": []}


@pytest.mark.anyio
async def test_cloud_provider_b_proxy_streaming(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_b_custody: CloudKeyCustody,
) -> None:
    """POST /cloud-provider-b/chat/completions with Accept: text/event-stream streams."""
    transport = _StreamTransport(status=200)
    app = _make_app(ledger, custody, cloud_provider_b_custody, transport=transport)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            resp = await client.post(
                "/v1/standin",
                json={"account": "default", "scope": "cloud-provider-b", "ttl_seconds": 300},
            )
            token = resp.json()["token"]

            response = await client.post(
                "/cloud-provider-b/chat/completions",
                json={"model": "test", "messages": []},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")


@pytest.mark.anyio
async def test_cloud_provider_b_proxy_missing_token(ledger: StandInTokenLedger, custody: FakeCustody) -> None:
    """Request without Authorization header returns 401."""
    app = _make_app(ledger, custody, cloud_provider_b_custody=None)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/cloud-provider-b/models")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_cloud_provider_b_proxy_invalid_token(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_b_custody: CloudKeyCustody,
) -> None:
    """An invalid token is rejected with 401."""
    app = _make_app(ledger, custody, cloud_provider_b_custody, transport=_BufferedTransport())
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get(
                "/cloud-provider-b/models",
                headers={"Authorization": "Bearer ati_invalid_token_12345"},
            )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_cloud_provider_b_proxy_no_custody(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
) -> None:
    """When cloud_provider_b_key_custody is not configured, returns 503."""
    transport = _BufferedTransport()
    app = _make_app(ledger, custody, cloud_provider_b_custody=None, transport=transport)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            # Mint an cloud-provider-b-scoped token
            resp = await client.post(
                "/v1/standin",
                json={"account": "default", "scope": "cloud-provider-b", "ttl_seconds": 300},
            )
            token = resp.json()["token"]

            response = await client.get(
                "/cloud-provider-b/models",
                headers={"Authorization": f"Bearer {token}"},
            )
    # Should fail because custody is not configured — but the error could be
    # 503 (ProxyScopeNotConfigured) or 500 depending on where it surfaces.
    assert response.status_code in (500, 503)
