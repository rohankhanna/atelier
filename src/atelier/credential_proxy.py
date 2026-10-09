from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Protocol

from atelier.cloud_key_custody import CloudKeyCustody
from atelier.oauth_custody import OAuthCustody
from atelier.standin import StandInTokenLedger

UPSTREAM_SCOPE = "provider-upstream"
CLOUD_PROVIDER_A_KEY_SCOPE = "cloud-provider-a-key"
CLOUD_PROVIDER_B_SCOPE = "cloud-provider-b"
CLOUD_PROVIDER_D_SCOPE = "cloud-provider-d"
CLOUD_PROVIDER_C_SCOPE = "cloud-provider-c"


class ProxyScopeNotConfigured(RuntimeError):
    """Raised when a stand-in token's scope has no custody wired (e.g.
    cloud-provider-a-key requested but cloud_key_custody is not configured).

    The server maps this to a 503 with a non-disclosing body.
    """


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


class UpstreamStream(Protocol):
    """A live upstream response whose body is streamed chunk-by-chunk.

    Response status and headers are available immediately (after upstream
    response headers arrive); the body is iterated lazily so the event loop is
    never blocked waiting for a slow/long upstream stream to finish.
    """

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> dict[str, str]: ...

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class HttpTransport(Protocol):
    async def send(self, request: ProxyRequest) -> ProxyResponse:
        raise NotImplementedError

    async def open_stream(self, request: ProxyRequest) -> UpstreamStream:
        raise NotImplementedError


class CredentialProxy:
    def __init__(
        self,
        *,
        ledger: StandInTokenLedger,
        custody: OAuthCustody,
        transport: HttpTransport,
        cloud_key_custody: CloudKeyCustody | None = None,
        cloud_provider_b_key_custody: CloudKeyCustody | None = None,
        cloud_provider_d_key_custody: CloudKeyCustody | None = None,
        cloud_provider_c_key_custody: CloudKeyCustody | None = None,
    ) -> None:
        self._ledger = ledger
        self._custody = custody
        self._transport = transport
        self._cloud_custody = cloud_key_custody
        self._cloud_provider_b_custody = cloud_provider_b_key_custody
        self._cloud_provider_d_custody = cloud_provider_d_key_custody
        self._cloud_provider_c_custody = cloud_provider_c_key_custody

    async def proxy_upstream(self, *, stand_in_token: str, request: ProxyRequest) -> ProxyResponse:
        record = self._ledger.validate_any(
            stand_in_token,
            audience="local-client",
            scopes={UPSTREAM_SCOPE, CLOUD_PROVIDER_A_KEY_SCOPE, CLOUD_PROVIDER_B_SCOPE, CLOUD_PROVIDER_D_SCOPE, CLOUD_PROVIDER_C_SCOPE},
        )
        if record.scope == CLOUD_PROVIDER_A_KEY_SCOPE:
            if self._cloud_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-a cloud key custody not configured"
                )
            key = await self._cloud_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        elif record.scope == CLOUD_PROVIDER_B_SCOPE:
            if self._cloud_provider_b_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-b key custody not configured"
                )
            key = await self._cloud_provider_b_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        elif record.scope == CLOUD_PROVIDER_D_SCOPE:
            if self._cloud_provider_d_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-d key custody not configured"
                )
            key = await self._cloud_provider_d_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        elif record.scope == CLOUD_PROVIDER_C_SCOPE:
            if self._cloud_provider_c_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-c key custody not configured"
                )
            key = await self._cloud_provider_c_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        else:
            grant = await self._custody.access_grant(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=grant.access_token,
                    account_id=grant.account_id,
                ),
            )
        return await self._transport.send(upstream)

    async def open_upstream_stream(
        self, *, stand_in_token: str, request: ProxyRequest
    ) -> UpstreamStream:
        """Open a credential-injected streaming upstream request.

        Validates the stand-in token, resolves the credential for the token's
        scope (provider OAuth access grant for provider-upstream, or a provider
        API key for cloud-provider-a-key), injects credential headers, and opens a
        streaming upstream connection whose body is yielded chunk-by-chunk by
        the caller.
        """
        record = self._ledger.validate_any(
            stand_in_token,
            audience="local-client",
            scopes={UPSTREAM_SCOPE, CLOUD_PROVIDER_A_KEY_SCOPE, CLOUD_PROVIDER_B_SCOPE, CLOUD_PROVIDER_D_SCOPE, CLOUD_PROVIDER_C_SCOPE},
        )
        if record.scope == CLOUD_PROVIDER_A_KEY_SCOPE:
            if self._cloud_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-a cloud key custody not configured"
                )
            key = await self._cloud_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        elif record.scope == CLOUD_PROVIDER_B_SCOPE:
            if self._cloud_provider_b_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-b key custody not configured"
                )
            key = await self._cloud_provider_b_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        elif record.scope == CLOUD_PROVIDER_D_SCOPE:
            if self._cloud_provider_d_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-d key custody not configured"
                )
            key = await self._cloud_provider_d_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        elif record.scope == CLOUD_PROVIDER_C_SCOPE:
            if self._cloud_provider_c_custody is None:
                raise ProxyScopeNotConfigured(
                    "cloud-provider-c key custody not configured"
                )
            key = await self._cloud_provider_c_custody.key(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=key,
                    account_id=None,
                ),
            )
        else:
            grant = await self._custody.access_grant(record.account)
            upstream = replace(
                request,
                headers=_credential_headers(
                    request.headers,
                    access_token=grant.access_token,
                    account_id=grant.account_id,
                ),
            )
        return await self._transport.open_stream(upstream)


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
