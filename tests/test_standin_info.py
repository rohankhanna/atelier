"""Tests for the GET /v1/standin/info endpoint."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from atelier.oauth_custody import OAuthCustody
from atelier.server import create_app
from atelier.standin import StandInTokenLedger


class FakeCustody:
    async def access_grant(self, account: str) -> object:
        raise RuntimeError("not used")


@pytest.fixture
def ledger() -> StandInTokenLedger:
    return StandInTokenLedger(now=lambda: 1000)


@pytest.fixture
def custody() -> FakeCustody:
    return FakeCustody()


def _make_app(ledger: StandInTokenLedger, custody: FakeCustody) -> FastAPI:
    return create_app(custody=custody, ledger=ledger)


@pytest.mark.anyio
async def test_standin_info_returns_tokens(
    ledger: StandInTokenLedger, custody: FakeCustody
) -> None:
    """The info endpoint returns metadata for minted tokens."""
    ledger.mint(
        audience="local-client",
        scope="cloud-provider-b",
        account="default",
        ttl_seconds=300,
    )
    ledger.mint(
        audience="local-client",
        scope="cloud-provider-c",
        account="default",
        ttl_seconds=600,
    )
    app = _make_app(ledger, custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/standin/info")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    scopes = {entry["scope"] for entry in data}
    assert scopes == {"cloud-provider-b", "cloud-provider-c"}


@pytest.mark.anyio
async def test_standin_info_excludes_secrets(
    ledger: StandInTokenLedger, custody: FakeCustody
) -> None:
    """The info endpoint must never include token_hash or raw token secrets."""
    token = ledger.mint(
        audience="local-client",
        scope="cloud-provider-c",
        account="default",
        ttl_seconds=300,
    )
    app = _make_app(ledger, custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/standin/info")

    data = response.json()
    assert len(data) == 1
    entry = data[0]
    # Must contain non-secret metadata fields
    assert "token_id" in entry
    assert "scope" in entry
    assert "account" in entry
    assert "audience" in entry
    assert "issued_at" in entry
    assert "expires_at" in entry
    assert "revoked" in entry
    # Must NOT contain secret fields
    assert "token_hash" not in entry
    assert "token" not in entry
    # The raw token must not appear anywhere in the response
    assert token.token not in response.text


@pytest.mark.anyio
async def test_standin_info_empty_when_no_tokens(
    ledger: StandInTokenLedger, custody: FakeCustody
) -> None:
    """The info endpoint returns an empty list when no tokens exist."""
    app = _make_app(ledger, custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/standin/info")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.anyio
async def test_standin_info_includes_revoked(
    ledger: StandInTokenLedger, custody: FakeCustody
) -> None:
    """Revoked tokens are included with revoked=True."""
    token = ledger.mint(
        audience="local-client",
        scope="cloud-provider-b",
        account="default",
        ttl_seconds=300,
    )
    ledger.revoke(token.token_id)
    app = _make_app(ledger, custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/standin/info")

    data = response.json()
    assert len(data) == 1
    assert data[0]["revoked"] is True


@pytest.mark.anyio
async def test_standin_info_no_auth_required(
    ledger: StandInTokenLedger, custody: FakeCustody
) -> None:
    """The info endpoint does not require an Authorization header."""
    ledger.mint(
        audience="local-client",
        scope="cloud-provider-c",
        account="default",
        ttl_seconds=300,
    )
    app = _make_app(ledger, custody)
    async with httpx.ASGITransport(app=app) as asgi:
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
            response = await client.get("/v1/standin/info")

    assert response.status_code == 200
