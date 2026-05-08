from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from atelier.oauth_custody import OAuthCustody
from atelier.standin import StandInTokenLedger

UPSTREAM_SCOPE = "provider-upstream"


@dataclass(frozen=True)
class ProxyRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class ProxyResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    def send(self, request: ProxyRequest) -> ProxyResponse:
        raise NotImplementedError


class CredentialProxy:
    def __init__(
        self,
        *,
        ledger: StandInTokenLedger,
        custody: OAuthCustody,
        transport: HttpTransport,
    ) -> None:
        self._ledger = ledger
        self._custody = custody
        self._transport = transport

    def proxy_upstream(self, *, stand_in_token: str, request: ProxyRequest) -> ProxyResponse:
        record = self._ledger.validate(
            stand_in_token,
            audience="local-client",
            scope=UPSTREAM_SCOPE,
        )
        grant = self._custody.access_grant(record.account)
        upstream = replace(
            request,
            headers=_credential_headers(
                request.headers,
                access_token=grant.access_token,
                account_id=grant.account_id,
            ),
        )
        return self._transport.send(upstream)


def _credential_headers(
    headers: dict[str, str], *, access_token: str, account_id: str | None
) -> dict[str, str]:
    scrubbed = {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"authorization", "x-oauth-account-id"}
    }
    scrubbed["Authorization"] = f"Bearer {access_token}"
    if account_id is not None:
        scrubbed["x-oauth-account-id"] = account_id
    return scrubbed
