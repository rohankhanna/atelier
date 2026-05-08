from __future__ import annotations

import pytest

from atelier.oauth_custody import AccessGrant
from atelier.credential_proxy import CredentialProxy, ProxyRequest, ProxyResponse
from atelier.standin import StandInTokenLedger


class FakeCustody:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def access_grant(self, account: str) -> AccessGrant:
        self.requests.append(account)
        return AccessGrant(
            access_token="PROVIDER_ACCESS_TOKEN_PLACEHOLDER",
            account_id=account,
            expires_at=5000,
        )


class CaptureTransport:
    def __init__(self) -> None:
        self.request: ProxyRequest | None = None

    def send(self, request: ProxyRequest) -> ProxyResponse:
        self.request = request
        return ProxyResponse(status_code=200, headers={}, body=b"ok")


def test_proxy_injects_provider_credentials_after_stand_in_validation() -> None:
    ledger = StandInTokenLedger(now=lambda: 1000)
    stand_in = ledger.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=60,
    )
    custody = FakeCustody()
    transport = CaptureTransport()
    proxy = CredentialProxy(ledger=ledger, custody=custody, transport=transport)

    response = proxy.proxy_upstream(
        stand_in_token=stand_in.token,
        request=ProxyRequest(
            method="POST",
            url="https://provider.example/upstream",
            headers={"Authorization": "Bearer STAND_IN_SHOULD_NOT_FORWARD"},
            body=b"{}",
        ),
    )

    assert response.status_code == 200
    assert custody.requests == ["test-account"]
    assert transport.request is not None
    assert transport.request.headers["Authorization"] == "Bearer PROVIDER_ACCESS_TOKEN_PLACEHOLDER"
    assert transport.request.headers["x-oauth-account-id"] == "test-account"
    assert "STAND_IN_SHOULD_NOT_FORWARD" not in transport.request.headers["Authorization"]


def test_proxy_rejects_invalid_stand_in_before_custody_lookup() -> None:
    ledger = StandInTokenLedger(now=lambda: 1000)
    custody = FakeCustody()
    proxy = CredentialProxy(ledger=ledger, custody=custody, transport=CaptureTransport())

    with pytest.raises(PermissionError):
        proxy.proxy_upstream(
            stand_in_token="ati_missing_secret",
            request=ProxyRequest(method="GET", url="https://example.invalid", headers={}, body=b""),
        )

    assert custody.requests == []
