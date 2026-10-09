"""Tests for Atelier HTTP server."""

from __future__ import annotations

import base64

import httpx
import pytest

from atelier.oauth_custody import AccessGrant
from atelier.credential_proxy import ProxyRequest, ProxyResponse
from atelier.server import create_app
from atelier.standin import StandInTokenLedger


class FakeCustody:
    """Fake OAuthCustody for testing."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.grants: dict[str, AccessGrant] = {}

    def set_grant(self, account: str, grant: AccessGrant) -> None:
        self.grants[account] = grant

    async def access_grant(self, account: str) -> AccessGrant:
        self.requests.append(account)
        if account not in self.grants:
            raise ValueError(f"Unknown account: {account}")
        return self.grants[account]


SYNTHETIC_HEADERS = {
    "x-oauth-provider-api-primary-used-percent": "12.5",
    "x-oauth-provider-api-secondary-used-percent": "34.5",
    "x-oauth-provider-api-plan-type": "plus",
    "connection": "keep-alive",
    "content-length": "9999",
    "transfer-encoding": "chunked",
    "content-encoding": "gzip",
}


def _synthetic_status(url: str) -> int:
    if url.endswith("/401"):
        return 401
    if url.endswith("/429"):
        return 429
    return 200


class _SyntheticUpstreamStream:
    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(SYNTHETIC_HEADERS)

    async def aiter_bytes(self):
        yield b"data: synthetic\n\n"

    async def aclose(self) -> None:
        return None


class SyntheticHeaderTransport:
    def __init__(self, client) -> None:
        self.client = client

    async def send(self, request: ProxyRequest) -> ProxyResponse:
        return ProxyResponse(
            status_code=_synthetic_status(request.url),
            headers=dict(SYNTHETIC_HEADERS),
            body=b"data: synthetic\n\n",
        )

    async def open_stream(self, request: ProxyRequest) -> _SyntheticUpstreamStream:
        return _SyntheticUpstreamStream(_synthetic_status(request.url))


@pytest.fixture
def ledger():
    return StandInTokenLedger(now=lambda: 1000)


@pytest.fixture
def custody():
    custody = FakeCustody()
    custody.set_grant(
        "test-account",
        AccessGrant(
            access_token="PROVIDER_ACCESS_TOKEN_ACCOUNT_A",
            account_id="test-account",
            expires_at=5000,
        ),
    )
    custody.set_grant(
        "account-b",
        AccessGrant(
            access_token="PROVIDER_ACCESS_TOKEN_ACCOUNT_B",
            account_id="account-b",
            expires_at=5000,
        ),
    )
    return custody


@pytest.fixture
async def client(ledger, custody):
    """HTTP client for testing the FastAPI app."""
    app = create_app(custody=custody, ledger=ledger)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_health_endpoint(client):
    """GET /health returns ok."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_standin_happy_path(client, ledger):
    """POST /v1/standin mints a stand-in token."""
    response = await client.post(
        "/v1/standin",
        json={"account": "test-account", "ttl_seconds": 300},
    )
    assert response.status_code == 200
    body = response.json()
    assert "token" in body
    assert body["token"].startswith("ati_")
    assert body["account"] == "test-account"
    assert body["expires_at"] == 1300  # 1000 + 300
    assert "token_id" in body


@pytest.mark.anyio
async def test_standin_missing_account(client):
    """POST /v1/standin without account returns 400."""
    response = await client.post("/v1/standin", json={"ttl_seconds": 300})
    assert response.status_code == 400
    assert "account is required" in response.json()["detail"]


@pytest.mark.anyio
async def test_standin_invalid_ttl(client):
    """POST /v1/standin with invalid ttl_seconds returns 400."""
    response = await client.post(
        "/v1/standin",
        json={"account": "test-account", "ttl_seconds": -1},
    )
    assert response.status_code == 400
    assert "ttl_seconds must be positive" in response.json()["detail"]


@pytest.mark.anyio
async def test_standin_default_ttl(client):
    """POST /v1/standin defaults ttl_seconds to 300."""
    response = await client.post("/v1/standin", json={"account": "test-account"})
    assert response.status_code == 200
    body = response.json()
    assert body["expires_at"] == 1300


@pytest.mark.anyio
async def test_standin_access_token_not_in_response(client):
    """Stand-in response never includes provider access token."""
    response = await client.post(
        "/v1/standin",
        json={"account": "test-account"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "PROVIDER_ACCESS_TOKEN" not in str(body)


@pytest.mark.anyio
async def test_proxy_happy_path(client, ledger, custody):
    """POST /v1/proxy injects credentials and proxies the request."""
    # Mint a stand-in token
    standin_response = await client.post(
        "/v1/standin",
        json={"account": "test-account", "ttl_seconds": 300},
    )
    stand_in_token = standin_response.json()["token"]

    # Create a request body
    request_body = b'{"prompt": "hello"}'
    request_body_b64 = base64.b64encode(request_body).decode("ascii")

    # Proxy the request
    response = await client.post(
        "/v1/proxy",
        json={
            "url": "https://example.com/oauth-provider/v1/chat/completions",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body_b64": request_body_b64,
        },
        headers={"Authorization": f"Bearer {stand_in_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "status_code" in body
    assert "headers" in body
    assert "body_b64" in body


@pytest.mark.anyio
async def test_proxy_missing_authorization(client):
    """POST /v1/proxy without Authorization header returns 401."""
    response = await client.post(
        "/v1/proxy",
        json={
            "url": "https://example.com/oauth-provider/v1/chat/completions",
            "method": "POST",
            "headers": {},
            "body_b64": "",
        },
    )
    assert response.status_code == 401
    assert "Authorization header required" in response.json()["detail"]


@pytest.mark.anyio
async def test_proxy_invalid_bearer_token(client):
    """POST /v1/proxy with invalid bearer token returns 401."""
    response = await client.post(
        "/v1/proxy",
        json={
            "url": "https://example.com/oauth-provider/v1/chat/completions",
            "method": "POST",
            "headers": {},
            "body_b64": "",
        },
        headers={"Authorization": "Bearer ati_invalid"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_proxy_missing_url(client):
    """POST /v1/proxy without url returns 400."""
    response = await client.post(
        "/v1/proxy",
        json={
            "method": "POST",
            "headers": {},
            "body_b64": "",
        },
        headers={"Authorization": "Bearer ati_test"},
    )
    assert response.status_code == 400
    assert "url is required" in response.json()["detail"]


@pytest.mark.anyio
async def test_proxy_invalid_body_b64(client, ledger):
    """POST /v1/proxy with invalid base64 body returns 400."""
    # Mint a stand-in token
    standin_response = await client.post(
        "/v1/standin",
        json={"account": "test-account"},
    )
    stand_in_token = standin_response.json()["token"]

    response = await client.post(
        "/v1/proxy",
        json={
            "url": "https://example.com/oauth-provider/v1/chat/completions",
            "method": "POST",
            "headers": {},
            "body_b64": "not-valid-base64!!!",
        },
        headers={"Authorization": f"Bearer {stand_in_token}"},
    )
    assert response.status_code == 400
    assert "invalid body_b64" in response.json()["detail"]


@pytest.mark.anyio
async def test_proxy_access_token_not_in_response_body(client, ledger):
    """POST /v1/proxy response never includes provider access token."""
    # Mint a stand-in token
    standin_response = await client.post(
        "/v1/standin",
        json={"account": "test-account"},
    )
    stand_in_token = standin_response.json()["token"]

    response = await client.post(
        "/v1/proxy",
        json={
            "url": "https://example.com/oauth-provider/v1/chat/completions",
            "method": "POST",
            "headers": {},
            "body_b64": "",
        },
        headers={"Authorization": f"Bearer {stand_in_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    # The response body_b64 is base64 encoded, but we check the full response text
    assert "PROVIDER_ACCESS_TOKEN" not in response.text


@pytest.mark.anyio
async def test_proxy_stream_happy_path(monkeypatch, ledger, custody):
    """POST /v1/proxy/stream returns streaming response with upstream status."""
    import atelier.server as server_mod

    monkeypatch.setattr(server_mod, "_HttpTransport", SyntheticHeaderTransport)
    app = create_app(custody=custody, ledger=ledger)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        standin_response = await client.post(
            "/v1/standin",
            json={"account": "test-account"},
        )
        stand_in_token = standin_response.json()["token"]

        response = await client.post(
            "/v1/proxy/stream",
            json={
                "url": "https://example.com/oauth-provider-api/responses/200",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_b64": "",
            },
            headers={"Authorization": f"Bearer {stand_in_token}"},
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]


@pytest.mark.anyio
@pytest.mark.parametrize("upstream_status", [200, 401, 429])
async def test_proxy_stream_forwards_upstream_headers_and_status(
    monkeypatch, ledger, custody, upstream_status
):
    """POST /v1/proxy/stream forwards upstream quota headers."""
    import atelier.server as server_mod

    monkeypatch.setattr(server_mod, "_HttpTransport", SyntheticHeaderTransport)
    app = create_app(custody=custody, ledger=ledger)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        standin_response = await client.post("/v1/standin", json={"account": "test-account"})
        stand_in_token = standin_response.json()["token"]

        response = await client.post(
            "/v1/proxy/stream",
            json={
                "url": f"https://example.com/oauth-provider-api/responses/{upstream_status}",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_b64": "",
            },
            headers={"Authorization": f"Bearer {stand_in_token}"},
        )

    assert response.status_code == upstream_status
    assert response.headers["x-atelier-upstream-status"] == str(upstream_status)
    assert response.headers["x-oauth-provider-api-primary-used-percent"] == "12.5"
    assert response.headers["x-oauth-provider-api-secondary-used-percent"] == "34.5"
    assert response.headers["x-oauth-provider-api-plan-type"] == "plus"
    assert "connection" not in response.headers
    assert "content-length" not in response.headers
    assert "transfer-encoding" not in response.headers
    assert "content-encoding" not in response.headers


@pytest.mark.anyio
async def test_proxy_stream_invalid_token(client):
    """POST /v1/proxy/stream with invalid token returns 401."""
    response = await client.post(
        "/v1/proxy/stream",
        json={
            "url": "https://example.com/oauth-provider/v1/chat/completions",
            "method": "POST",
            "headers": {},
            "body_b64": "",
        },
        headers={"Authorization": "Bearer ati_invalid"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# /v1/cloud-provider-a/usage endpoint tests
# ---------------------------------------------------------------------------

from atelier.cookie_custody import CookieNotAvailable  # noqa: E402
from atelier.server import _require_loopback  # noqa: E402
import fastapi  # noqa: E402


SAMPLE_USAGE_HTML = (
    "<html><body>"
    '<span>Session usage</span><span>3.7% used</span>'
    '<div class="local-time" data-time="2026-06-30T19:00:00Z">Resets in 5 hours.</div>'
    '<span>Weekly usage</span><span>42.1% used</span>'
    '<div class="local-time" data-time="2026-07-03T00:00:00Z">Resets in 3 days.</div>'
    "</body></html>"
)


class FakeCookieCustody:
    """Fake CookieCustody for testing the cloud-provider-a usage endpoint.

    Returns a synthetic cookie jar for known accounts; raises CookieNotAvailable
    when configured to simulate a missing jar or for unknown accounts.
    """

    def __init__(self, *, raise_missing: bool = False) -> None:
        self.raise_missing = raise_missing
        self.requests: list[str] = []
        # SYNTHETIC placeholder cookie values only; never real session cookies.
        self._jars: dict[str, dict[str, str]] = {
            "test-account": {"__Secure-session": "SYNTHETIC", "aid": "SYNTHETIC"},
            "account-b": {"__Secure-session": "SYNTHETIC", "aid": "SYNTHETIC"},
        }

    def set_jar(self, account: str, jar: dict[str, str]) -> None:
        self._jars[account] = jar

    async def cookies(self, account: str) -> dict[str, str]:
        self.requests.append(account)
        if self.raise_missing or account not in self._jars:
            raise CookieNotAvailable(f"cookie jar not available for account {account!r}")
        return self._jars[account]


def _make_cloud_provider_a_fetch(html: str = SAMPLE_USAGE_HTML):
    """Build a fake cloud_provider_a_fetch async callable returning the given HTML."""

    async def fetch(cookies: dict[str, str]) -> str:
        return html

    return fetch


@pytest.fixture
def cookie_custody():
    return FakeCookieCustody()


@pytest.fixture
async def usage_client(ledger, custody, cookie_custody):
    """HTTP client for testing the /v1/cloud-provider-a/usage endpoint."""
    app = create_app(
        custody=custody,
        ledger=ledger,
        cookie_custody=cookie_custody,
        cloud_provider_a_fetch=_make_cloud_provider_a_fetch(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _mint_cloud_provider_a_usage_token(client) -> str:
    """Mint an cloud-provider-a-usage-scoped stand-in token and return the token string."""
    response = await client.post(
        "/v1/standin",
        json={"account": "test-account", "scope": "cloud-provider-a-usage"},
    )
    assert response.status_code == 200
    return response.json()["token"]


@pytest.mark.anyio
async def test_cloud_provider_a_usage_happy_path(usage_client):
    """GET /v1/cloud-provider-a/usage returns parsed meters and never leaks cookies."""
    token = await _mint_cloud_provider_a_usage_token(usage_client)
    response = await usage_client.get(
        "/v1/cloud-provider-a/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["session"]["percent"] == 3.7
    assert body["weekly"]["percent"] == 42.1
    assert "fetched_at" in body
    # Synthetic cookie values must never leak into the response.
    assert "SYNTHETIC" not in response.text


@pytest.mark.anyio
async def test_cloud_provider_a_usage_missing_authorization(usage_client):
    """GET /v1/cloud-provider-a/usage with no Authorization header returns 401."""
    response = await usage_client.get("/v1/cloud-provider-a/usage")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_cloud_provider_a_usage_wrong_scope(usage_client):
    """GET /v1/cloud-provider-a/usage with a provider-upstream-scoped token returns 401."""
    standin_response = await usage_client.post(
        "/v1/standin",
        json={"account": "test-account"},
    )
    token = standin_response.json()["token"]
    response = await usage_client.get(
        "/v1/cloud-provider-a/usage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_cloud_provider_a_usage_missing_cookie(ledger, custody):
    """GET /v1/cloud-provider-a/usage returns 502 when cookie custody has no jar."""
    missing_custody = FakeCookieCustody(raise_missing=True)
    app = create_app(
        custody=custody,
        ledger=ledger,
        cookie_custody=missing_custody,
        cloud_provider_a_fetch=_make_cloud_provider_a_fetch(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _mint_cloud_provider_a_usage_token(client)
        response = await client.get(
            "/v1/cloud-provider-a/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 502


@pytest.mark.anyio
async def test_cloud_provider_a_usage_fetch_failure(ledger, custody, cookie_custody):
    """GET /v1/cloud-provider-a/usage returns 502 when the upstream fetch raises httpx.ConnectError."""
    async def failing_fetch(cookies: dict[str, str]) -> str:
        raise httpx.ConnectError("synthetic connection failure")

    app = create_app(
        custody=custody,
        ledger=ledger,
        cookie_custody=cookie_custody,
        cloud_provider_a_fetch=failing_fetch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _mint_cloud_provider_a_usage_token(client)
        response = await client.get(
            "/v1/cloud-provider-a/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 502


@pytest.mark.anyio
async def test_cloud_provider_a_usage_parse_empty(ledger, custody, cookie_custody):
    """GET /v1/cloud-provider-a/usage returns 200 with empty meters on unparseable HTML."""
    app = create_app(
        custody=custody,
        ledger=ledger,
        cookie_custody=cookie_custody,
        cloud_provider_a_fetch=_make_cloud_provider_a_fetch("<html></html>"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _mint_cloud_provider_a_usage_token(client)
        response = await client.get(
            "/v1/cloud-provider-a/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["session"] == {}
    assert body["weekly"] == {}


@pytest.mark.anyio
async def test_cloud_provider_a_usage_not_configured(ledger, custody):
    """GET /v1/cloud-provider-a/usage returns 503 when cookie_custody is not wired."""
    app = create_app(custody=custody, ledger=ledger)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _mint_cloud_provider_a_usage_token(client)
        response = await client.get(
            "/v1/cloud-provider-a/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503


@pytest.mark.anyio
async def test_standin_accepts_scope_field(client):
    """POST /v1/standin honors a custom scope field."""
    response = await client.post(
        "/v1/standin",
        json={"account": "test-account", "scope": "cloud-provider-a-usage"},
    )
    assert response.status_code == 200
    assert response.json()["scope"] == "cloud-provider-a-usage"


@pytest.mark.anyio
async def test_standin_default_scope_unchanged(client):
    """POST /v1/standin defaults scope to provider-upstream when omitted."""
    response = await client.post(
        "/v1/standin",
        json={"account": "test-account"},
    )
    assert response.status_code == 200
    assert response.json()["scope"] == "provider-upstream"


def test_loopback_guard_accepts_loopback():
    """_require_loopback passes for loopback addresses."""
    _require_loopback("127.0.0.1")
    _require_loopback("::1")


def test_loopback_guard_rejects_non_loopback():
    """_require_loopback raises 403 HTTPException for non-loopback hosts."""
    with pytest.raises(fastapi.HTTPException) as exc_info:
        _require_loopback("10.0.0.1")
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# cloud-provider-a-key scope: provider API key injection via final hop
# ---------------------------------------------------------------------------

from atelier.cloud_key_custody import CloudKeyCustody  # noqa: E402
import atelier.server as _server_mod  # noqa: E402

# Module-level holder for the most recent proxied request, so the capturing
# transport (instantiated inside create_app) can expose what it sent upstream.
_CAPTURED_REQUESTS: list[ProxyRequest] = []


class _CapturingUpstreamStream:
    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(SYNTHETIC_HEADERS)

    async def aiter_bytes(self):
        yield b"data: synthetic\n\n"

    async def aclose(self) -> None:
        return None


class CapturingHeaderTransport:
    """Transport fake that records each proxied request's headers for assertion."""

    def __init__(self, client) -> None:
        self.client = client

    async def send(self, request: ProxyRequest) -> ProxyResponse:
        _CAPTURED_REQUESTS.append(request)
        return ProxyResponse(
            status_code=200,
            headers=dict(SYNTHETIC_HEADERS),
            body=b"data: synthetic\n\n",
        )

    async def open_stream(self, request: ProxyRequest) -> _CapturingUpstreamStream:
        _CAPTURED_REQUESTS.append(request)
        return _CapturingUpstreamStream(200)


class _MemoryPassStore:
    """In-memory stand-in for PassStore used by CloudKeyCustody in tests."""

    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = entries
        self.shown: list[str] = []

    def show(self, path: str) -> str:
        self.shown.append(path)
        if path not in self.entries:
            from atelier.pass_store import PassStoreError

            raise PassStoreError(f"pass show failed for {path!r}")
        return self.entries[path]


def _cloud_key_custody() -> CloudKeyCustody:
    store = _MemoryPassStore(
        {"llm/cloud-provider-a/accounts/test-account/cloud-key": "SYNTHETIC_CLOUD_KEY"}
    )
    return CloudKeyCustody(store=store)


async def _mint_scoped_token(client, *, account: str, scope: str) -> str:
    """Mint a stand-in token with an explicit scope and return the token string."""
    response = await client.post(
        "/v1/standin",
        json={"account": account, "scope": scope},
    )
    assert response.status_code == 200
    return response.json()["token"]


@pytest.mark.anyio
async def test_proxy_stream_cloud_provider_a_cloud_injects_cloud_key(monkeypatch, ledger, custody):
    """cloud-provider-a-key-scoped stand-in token injects the cloud API key (not an OAuth
    token) into the upstream Authorization header, with no x-oauth-account-id."""
    monkeypatch.setattr(_server_mod, "_HttpTransport", CapturingHeaderTransport)
    _CAPTURED_REQUESTS.clear()
    app = create_app(
        custody=custody,
        ledger=ledger,
        cloud_key_custody=_cloud_key_custody(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stand_in_token = await _mint_scoped_token(
            client, account="test-account", scope="cloud-provider-a-key"
        )
        response = await client.post(
            "/v1/proxy/stream",
            json={
                "url": "https://example.com/cloud-provider-a/v1/chat/completions",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_b64": base64.b64encode(b'{"prompt":"hi"}').decode("ascii"),
            },
            headers={"Authorization": f"Bearer {stand_in_token}"},
        )

    assert response.status_code == 200
    assert _CAPTURED_REQUESTS, "transport should have captured the upstream request"
    upstream = _CAPTURED_REQUESTS[-1]
    header_keys = {k.lower() for k in upstream.headers}
    assert upstream.headers["Authorization"] == "Bearer SYNTHETIC_CLOUD_KEY"
    assert "x-oauth-account-id" not in header_keys
    # The synthetic cloud key must never appear in the response body.
    assert "SYNTHETIC_CLOUD_KEY" not in response.text


@pytest.mark.anyio
async def test_proxy_stream_cloud_scope_and_provider_scope_use_different_credentials(
    monkeypatch, ledger, custody
):
    """Regression guard: cloud-provider-a-key injects the cloud key, provider-upstream
    injects the OAuth access token — the two scopes draw from different sources."""
    monkeypatch.setattr(_server_mod, "_HttpTransport", CapturingHeaderTransport)
    _CAPTURED_REQUESTS.clear()
    app = create_app(
        custody=custody,
        ledger=ledger,
        cloud_key_custody=_cloud_key_custody(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        cloud_token = await _mint_scoped_token(
            client, account="test-account", scope="cloud-provider-a-key"
        )
        await client.post(
            "/v1/proxy/stream",
            json={
                "url": "https://example.com/cloud-provider-a/v1/chat/completions",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_b64": "",
            },
            headers={"Authorization": f"Bearer {cloud_token}"},
        )
        provider_token = await _mint_scoped_token(
            client, account="test-account", scope="provider-upstream"
        )
        await client.post(
            "/v1/proxy/stream",
            json={
                "url": "https://example.com/oauth-provider/v1/chat/completions",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_b64": "",
            },
            headers={"Authorization": f"Bearer {provider_token}"},
        )

    assert len(_CAPTURED_REQUESTS) == 2
    cloud_upstream = _CAPTURED_REQUESTS[0]
    provider_upstream = _CAPTURED_REQUESTS[1]
    assert cloud_upstream.headers["Authorization"] == "Bearer SYNTHETIC_CLOUD_KEY"
    assert "x-oauth-account-id" not in {k.lower() for k in cloud_upstream.headers}
    assert provider_upstream.headers["Authorization"] == "Bearer PROVIDER_ACCESS_TOKEN_ACCOUNT_A"
    assert provider_upstream.headers["x-oauth-account-id"] == "test-account"
    # The two scopes must not produce the same bearer.
    assert (
        cloud_upstream.headers["Authorization"]
        != provider_upstream.headers["Authorization"]
    )


@pytest.mark.anyio
async def test_proxy_stream_cloud_provider_a_cloud_without_custody_returns_503(monkeypatch, ledger, custody):
    """cloud-provider-a-key token with cloud_key_custody=None maps to 503."""
    monkeypatch.setattr(_server_mod, "_HttpTransport", CapturingHeaderTransport)
    _CAPTURED_REQUESTS.clear()
    # cloud_key_custody defaults to None.
    app = create_app(custody=custody, ledger=ledger)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stand_in_token = await _mint_scoped_token(
            client, account="test-account", scope="cloud-provider-a-key"
        )
        response = await client.post(
            "/v1/proxy/stream",
            json={
                "url": "https://example.com/cloud-provider-a/v1/chat/completions",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_b64": "",
            },
            headers={"Authorization": f"Bearer {stand_in_token}"},
        )
    assert response.status_code == 503
    # Non-disclosing body: no key value present.
    assert "SYNTHETIC_CLOUD_KEY" not in response.text


@pytest.mark.anyio
async def test_proxy_stream_rejects_cloud_provider_a_usage_scope(monkeypatch, ledger, custody):
    """An cloud-provider-a-usage-scoped token is not in the proxy's allowed scope set and
    is rejected with 401 (scope isolation between proxy and usage)."""
    monkeypatch.setattr(_server_mod, "_HttpTransport", CapturingHeaderTransport)
    _CAPTURED_REQUESTS.clear()
    app = create_app(
        custody=custody,
        ledger=ledger,
        cloud_key_custody=_cloud_key_custody(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stand_in_token = await _mint_scoped_token(
            client, account="test-account", scope="cloud-provider-a-usage"
        )
        response = await client.post(
            "/v1/proxy/stream",
            json={
                "url": "https://example.com/oauth-provider/v1/chat/completions",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_b64": "",
            },
            headers={"Authorization": f"Bearer {stand_in_token}"},
        )
    assert response.status_code == 401
    assert not _CAPTURED_REQUESTS, "no upstream request should have been issued"
