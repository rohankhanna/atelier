"""Tests for the GET /v1/custody/status endpoint."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from atelier.cloud_key_custody import CloudKeyCustody
from atelier.cookie_custody import CookieCustody
from atelier.oauth_custody import OAuthCustody
from atelier.server import create_app
from atelier.standin import StandInTokenLedger


class FakeKeyStore:
    """In-memory pass store for testing."""

    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = entries

    def show(self, path: str) -> str:
        if path not in self._entries:
            from atelier.pass_store import PassStoreError
            raise PassStoreError(f"not found: {path}")
        return self._entries[path]

    def insert_multiline(self, path: str, value: str) -> None:
        self._entries[path] = value


def _make_jwt(exp: int) -> str:
    """Create a minimal JWT with the given exp claim."""
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b'=').decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b'=').decode()
    return f"{header}.{payload}."


def _make_oauth_bundle(access_token: str, refresh_token: str = "rt-xyz",
                       last_refresh: str | None = None) -> str:
    raw = {"tokens": {"access_token": access_token, "refresh_token": refresh_token}}
    if last_refresh:
        raw["last_refresh"] = last_refresh
    return json.dumps(raw)


@pytest.fixture
def ledger() -> StandInTokenLedger:
    return StandInTokenLedger(now=lambda: 1000)


@pytest.fixture
def key_store() -> FakeKeyStore:
    return FakeKeyStore({
        "llm/cloud-provider-b/accounts/default/cloud-key": "sk-or-test-key",
        "llm/cloud-provider-c/accounts/default/cloud-key": "nvapi-test-key",
    })


@pytest.fixture
def oauth_store() -> FakeKeyStore:
    return FakeKeyStore({
        "llm/oauth-provider/accounts/test-account-1/auth-json": _make_oauth_bundle(
            _make_jwt(2000), last_refresh="2026-10-06T05:00:00Z"
        ),
    })


@pytest.fixture
def custody(oauth_store: FakeKeyStore) -> OAuthCustody:
    return OAuthCustody(
        store=oauth_store,
        refresh_url="https://example.com/token",
        client_id="test-client",
        path_prefix="llm/oauth-provider/accounts",
        now=lambda: 1000,
    )


@pytest.fixture
def cloud_provider_b_custody(key_store: FakeKeyStore) -> CloudKeyCustody:
    return CloudKeyCustody(store=key_store, path_prefix="llm/cloud-provider-b/accounts")


@pytest.fixture
def cloud_provider_c_custody(key_store: FakeKeyStore) -> CloudKeyCustody:
    return CloudKeyCustody(store=key_store, path_prefix="llm/cloud-provider-c/accounts")


def _make_app(
    ledger: StandInTokenLedger,
    custody: OAuthCustody,
    cloud_provider_b_custody: CloudKeyCustody | None = None,
    cloud_provider_c_custody: CloudKeyCustody | None = None,
) -> FastAPI:
    return create_app(
        custody=custody,
        ledger=ledger,
        cloud_provider_b_key_custody=cloud_provider_b_custody,
        cloud_provider_c_key_custody=cloud_provider_c_custody,
    )


@pytest.mark.anyio
async def test_custody_status_combines_standin_and_credential(
    ledger: StandInTokenLedger,
    custody: OAuthCustody,
    cloud_provider_b_custody: CloudKeyCustody,
    cloud_provider_c_custody: CloudKeyCustody,
) -> None:
    """The status endpoint returns both stand-in token and credential metadata."""
    ledger.mint(audience="local-client", scope="cloud-provider-b", account="default", ttl_seconds=300)
    ledger.mint(audience="local-client", scope="cloud-provider-c", account="default", ttl_seconds=300)
    ledger.mint(audience="local-client", scope="provider-upstream", account="test-account-1", ttl_seconds=300)

    app = _make_app(ledger, custody, cloud_provider_b_custody, cloud_provider_c_custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/custody/status")

    assert response.status_code == 200
    data = response.json()
    accounts = data["accounts"]
    assert len(accounts) == 3

    by_scope = {a["standin"]["scope"]: a for a in accounts}

    # Cloud Provider B: stand-in token + cloud key status
    or_entry = by_scope["cloud-provider-b"]
    assert or_entry["standin"]["account"] == "default"
    assert or_entry["credential"]["key_present"] is True
    assert or_entry["credential"]["expiry"] == "non-expiring"

    # Cloud Provider C: stand-in token + cloud key status
    nv_entry = by_scope["cloud-provider-c"]
    assert nv_entry["credential"]["key_present"] is True

    # OAuth: stand-in token + access token expiry
    oauth_entry = by_scope["provider-upstream"]
    assert oauth_entry["standin"]["account"] == "test-account-1"
    assert oauth_entry["credential"]["available"] is True
    assert oauth_entry["credential"]["access_token_expires_at"] == 2000
    assert oauth_entry["credential"]["needs_refresh"] is False
    assert oauth_entry["credential"]["last_refresh"] == "2026-10-06T05:00:00Z"


@pytest.mark.anyio
async def test_custody_status_excludes_secrets(
    ledger: StandInTokenLedger,
    custody: OAuthCustody,
    cloud_provider_b_custody: CloudKeyCustody,
) -> None:
    """No token values, hashes, or raw credentials appear in the response."""
    token = ledger.mint(audience="local-client", scope="cloud-provider-b", account="default", ttl_seconds=300)
    app = _make_app(ledger, custody, cloud_provider_b_custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/custody/status")

    body = response.text
    assert "sk-or-test-key" not in body
    assert token.token not in body
    assert "token_hash" not in body


@pytest.mark.anyio
async def test_custody_status_oauth_needs_refresh(
    ledger: StandInTokenLedger,
    oauth_store: FakeKeyStore,
) -> None:
    """OAuth token within safety window shows needs_refresh=True."""
    # expires_at=1004, now=1000, safety window=300 → 1004-1000=4 <= 300 → needs refresh
    oauth_store._entries["llm/oauth-provider/accounts/test-account-1/auth-json"] = _make_oauth_bundle(
        _make_jwt(1004)
    )
    custody = OAuthCustody(
        store=oauth_store, refresh_url="https://example.com/token",
        client_id="test", path_prefix="llm/oauth-provider/accounts", now=lambda: 1000,
    )
    ledger.mint(audience="local-client", scope="provider-upstream", account="test-account-1", ttl_seconds=300)
    app = _make_app(ledger, custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/custody/status")

    data = response.json()
    oauth_entry = [a for a in data["accounts"] if a["standin"]["scope"] == "provider-upstream"][0]
    assert oauth_entry["credential"]["needs_refresh"] is True


@pytest.mark.anyio
async def test_custody_status_empty_when_no_tokens(
    ledger: StandInTokenLedger,
    custody: OAuthCustody,
) -> None:
    """Returns empty accounts list when no stand-in tokens exist."""
    app = _make_app(ledger, custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/custody/status")

    assert response.status_code == 200
    assert response.json() == {"accounts": []}


@pytest.mark.anyio
async def test_custody_status_revoked_tokens_excluded(
    ledger: StandInTokenLedger,
    custody: OAuthCustody,
    cloud_provider_b_custody: CloudKeyCustody,
) -> None:
    """Revoked stand-in tokens are not included in the status."""
    token = ledger.mint(audience="local-client", scope="cloud-provider-b", account="default", ttl_seconds=300)
    ledger.revoke(token.token_id)
    app = _make_app(ledger, custody, cloud_provider_b_custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/custody/status")

    assert response.json() == {"accounts": []}


@pytest.mark.anyio
async def test_custody_status_missing_key(
    ledger: StandInTokenLedger,
    custody: OAuthCustody,
    key_store: FakeKeyStore,
) -> None:
    """Cloud key scope with no key in pass shows key_present=False."""
    cloud_provider_c_custody = CloudKeyCustody(store=key_store, path_prefix="llm/cloud-provider-c/accounts")
    # Mint token for an account that has no key in pass
    ledger.mint(audience="local-client", scope="cloud-provider-c", account="nonexistent", ttl_seconds=300)
    app = _make_app(ledger, custody, cloud_provider_c_custody=cloud_provider_c_custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/custody/status")

    data = response.json()
    nv_entry = [a for a in data["accounts"] if a["standin"]["scope"] == "cloud-provider-c"][0]
    assert nv_entry["credential"]["key_present"] is False
    assert nv_entry["credential"]["available"] is False
