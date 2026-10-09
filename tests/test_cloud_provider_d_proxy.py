"""Tests for the /cloud-provider-d transparent reverse-proxy routes."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from atelier.cloud_key_custody import CloudKeyCustody
from atelier.credential_proxy import ProxyRequest, ProxyResponse
from atelier.oauth_custody import OAuthCustody
from atelier.server import create_app
from atelier.standin import StandInTokenLedger


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCustody:
    """No-op OAuth custody — not used by cloud-provider-d scope tests."""

    async def access_grant(self, account: str) -> Any:
        raise RuntimeError("should not be called for cloud-provider-d scope")


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
        self.last_request: ProxyRequest | None = None

    async def send(self, request: ProxyRequest) -> ProxyResponse:
        self.last_request = request
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
def cloud_provider_d_key_store() -> FakeKeyStore:
    return FakeKeyStore({
        "llm/cloud-provider-d/accounts/default/cloud-key": "tsk-test-cloud-provider-d-key-98765",
    })


@pytest.fixture
def cloud_provider_d_custody(cloud_provider_d_key_store: FakeKeyStore) -> CloudKeyCustody:
    return CloudKeyCustody(
        store=cloud_provider_d_key_store,  # type: ignore[arg-type]
        path_prefix="llm/cloud-provider-d/accounts",
    )


def _make_app(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_d_custody: CloudKeyCustody | None,
    *,
    transport: Any = None,
) -> FastAPI:
    """Build an app with the cloud_provider_d_key_custody wired in."""
    import atelier.server as server_mod

    if transport is not None:
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
        cloud_provider_d_key_custody=cloud_provider_d_custody,
    )

    if transport is not None:
        server_mod._HttpTransport = original

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cloud_provider_d_proxy_buffered(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_d_custody: CloudKeyCustody,
) -> None:
    """GET /cloud-provider-d/v1/models via buffered proxy injects the API key."""
    transport = _BufferedTransport(status=200, body=b'{"models": []}')
    app = _make_app(ledger, custody, cloud_provider_d_custody, transport=transport)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            resp = await client.post(
                "/v1/standin",
                json={"account": "default", "scope": "cloud-provider-d", "ttl_seconds": 300},
            )
            token = resp.json()["token"]

            response = await client.get(
                "/cloud-provider-d/v1/models",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200
    assert response.json() == {"models": []}
    # The real key must be injected on the final hop, replacing the stand-in token.
    assert transport.last_request is not None
    assert transport.last_request.headers["Authorization"] == "Bearer tsk-test-cloud-provider-d-key-98765"
    # The forwarded URL points at the real upstream, not the loopback proxy path.
    assert transport.last_request.url == "https://example.com/cloud-provider-d/v1/models"


@pytest.mark.anyio
async def test_cloud_provider_d_proxy_systemone_post(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_d_custody: CloudKeyCustody,
) -> None:
    """POST /cloud-provider-d/v1/systemone forwards the body and injects the key."""
    transport = _BufferedTransport(status=200, body=b'{"answers": {}}')
    app = _make_app(ledger, custody, cloud_provider_d_custody, transport=transport)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            resp = await client.post(
                "/v1/standin",
                json={"account": "default", "scope": "cloud-provider-d", "ttl_seconds": 300},
            )
            token = resp.json()["token"]

            response = await client.post(
                "/cloud-provider-d/v1/systemone",
                json={"model": "model-latest", "state": "hello", "questions": {}},
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200
    assert transport.last_request.url == "https://example.com/cloud-provider-d/v1/systemone"
    assert transport.last_request.headers["Authorization"] == "Bearer tsk-test-cloud-provider-d-key-98765"


@pytest.mark.anyio
async def test_cloud_provider_d_proxy_streaming(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_d_custody: CloudKeyCustody,
) -> None:
    """POST /cloud-provider-d/v1/systemone with Accept: text/event-stream streams."""
    transport = _StreamTransport(status=200)
    app = _make_app(ledger, custody, cloud_provider_d_custody, transport=transport)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            resp = await client.post(
                "/v1/standin",
                json={"account": "default", "scope": "cloud-provider-d", "ttl_seconds": 300},
            )
            token = resp.json()["token"]

            response = await client.post(
                "/cloud-provider-d/v1/systemone",
                json={"model": "model-latest", "state": "hello", "questions": {}},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")


@pytest.mark.anyio
async def test_cloud_provider_d_proxy_missing_token(ledger: StandInTokenLedger, custody: FakeCustody) -> None:
    """Request without Authorization header returns 401."""
    app = _make_app(ledger, custody, cloud_provider_d_custody=None)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/cloud-provider-d/v1/models")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_cloud_provider_d_proxy_invalid_token(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
    cloud_provider_d_custody: CloudKeyCustody,
) -> None:
    """An invalid token is rejected with 401."""
    app = _make_app(ledger, custody, cloud_provider_d_custody, transport=_BufferedTransport())
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get(
                "/cloud-provider-d/v1/models",
                headers={"Authorization": "Bearer ati_invalid_token_12345"},
            )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_cloud_provider_d_proxy_no_custody(
    ledger: StandInTokenLedger,
    custody: FakeCustody,
) -> None:
    """When cloud_provider_d_key_custody is not configured, returns 503."""
    transport = _BufferedTransport()
    app = _make_app(ledger, custody, cloud_provider_d_custody=None, transport=transport)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            resp = await client.post(
                "/v1/standin",
                json={"account": "default", "scope": "cloud-provider-d", "ttl_seconds": 300},
            )
            token = resp.json()["token"]

            response = await client.get(
                "/cloud-provider-d/v1/models",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code in (500, 503)
