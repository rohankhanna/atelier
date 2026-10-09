"""HTTP service for Atelier credential custody and proxying.

Exposes endpoints for stand-in token provisioning and credential-header injection
into upstream requests. Callers provide unsigned requests; Atelier injects credentials
and proxies to the provider.
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException, Header, Response, Body, Request
from fastapi.responses import StreamingResponse

from atelier.cloud_key_custody import CloudKeyCustody, CloudKeyNotAvailable
from atelier.cookie_custody import CookieCustody, CookieNotAvailable
from atelier.credential_proxy import (
    CredentialProxy,
    ProxyRequest,
    ProxyResponse,
    ProxyScopeNotConfigured,
)
from atelier.oauth_custody import OAuthCustody, TokenRefreshError
from atelier.cloud_provider_a_usage import SETTINGS_URL, UA, parse_usage
from atelier.standin import StandInTokenLedger

logger = logging.getLogger("atelier.server")


HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "content-type",
}
UPSTREAM_STATUS_HEADER = "x-atelier-upstream-status"
CLOUD_PROVIDER_A_USAGE_SCOPE = "cloud-provider-a-usage"
OAUTH_PROVIDER_API_UPSTREAM_BASE = "https://example.com/oauth-provider-api"
CLOUD_PROVIDER_B_UPSTREAM_BASE = "https://example.com/cloud-provider-b/v1"
CLOUD_PROVIDER_D_UPSTREAM_BASE = "https://example.com/cloud-provider-d"
CLOUD_PROVIDER_C_UPSTREAM_BASE = "https://example.com/cloud-provider-c/v1"
CLOUD_PROVIDER_A_UPSTREAM_BASE = "https://example.com/cloud-provider-a"

# Hop-by-hop headers that must not be forwarded on the *request* side
# (per-connection metadata that the proxy regenerates for the upstream hop).
HOP_BY_HOP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


class ProxyStandIn:
    """Request body for /v1/standin"""

    def __init__(self, *, account: str, ttl_seconds: int = 300) -> None:
        self.account = account
        self.ttl_seconds = ttl_seconds


class ProxyProxyRequest:
    """Request body for /v1/proxy and /v1/proxy/stream"""

    def __init__(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        body_b64: str,
    ) -> None:
        self.url = url
        self.method = method
        self.headers = headers
        self.body_b64 = body_b64


def _require_loopback(client_host: str | None) -> None:
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="loopback only")


def _httpx_cloud_provider_a_fetch(
    client: httpx.AsyncClient,
) -> Callable[[dict[str, str]], Awaitable[str]]:
    async def fetch(cookies: dict[str, str]) -> str:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        resp = await client.get(
            SETTINGS_URL,
            headers={"User-Agent": UA, "Accept": "text/html", "Cookie": cookie_header},
        )
        resp.raise_for_status()
        return resp.text

    return fetch


def create_app(
    *,
    custody: OAuthCustody,
    ledger: StandInTokenLedger,
    cookie_custody: CookieCustody | None = None,
    cloud_key_custody: CloudKeyCustody | None = None,
    cloud_provider_b_key_custody: CloudKeyCustody | None = None,
    cloud_provider_d_key_custody: CloudKeyCustody | None = None,
    cloud_provider_c_key_custody: CloudKeyCustody | None = None,
    cloud_provider_a_fetch: Callable[[dict[str, str]], Awaitable[str]] | None = None,
) -> FastAPI:
    """Create the Atelier HTTP service FastAPI app.

    Args:
        custody: OAuthCustody instance for token management
        ledger: StandInTokenLedger instance for token minting
        cookie_custody: optional CookieCustody for cloud-provider-a usage endpoint
        cloud_key_custody: optional CloudKeyCustody for provider API key injection
        cloud_provider_b_key_custody: optional CloudKeyCustody for Cloud Provider B API key injection
        cloud_provider_d_key_custody: optional CloudKeyCustody for Cloud Provider D API key injection
        cloud_provider_c_key_custody: optional CloudKeyCustody for Cloud Provider C API key injection
        cloud_provider_a_fetch: optional async fetcher taking cookies and returning HTML
    """
    # Async httpx client so upstream I/O never blocks the (single-worker) event loop.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)
    )

    # Create the credential proxy
    proxy = CredentialProxy(
        ledger=ledger,
        custody=custody,
        transport=_HttpTransport(http_client),
        cloud_key_custody=cloud_key_custody,
        cloud_provider_b_key_custody=cloud_provider_b_key_custody,
        cloud_provider_d_key_custody=cloud_provider_d_key_custody,
        cloud_provider_c_key_custody=cloud_provider_c_key_custody,
    )

    if cookie_custody is not None and cloud_provider_a_fetch is None:
        cloud_provider_a_fetch = _httpx_cloud_provider_a_fetch(http_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        yield
        # Shutdown
        await http_client.aclose()

    app = FastAPI(title="credential-proxy", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok"}

    @app.post("/v1/standin")
    async def standin(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Mint a stand-in token for an account.

        Request body: {"account": "test-account", "ttl_seconds": 300}
        Response: {"token": "ati_...", "token_id": "...", "account": "...", "expires_at": ...}
        """
        try:
            account = body.get("account")
            ttl_seconds = body.get("ttl_seconds", 300)
            if not isinstance(account, str) or not account.strip():
                raise HTTPException(status_code=400, detail="account is required")
            if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
                raise HTTPException(status_code=400, detail="ttl_seconds must be positive")

            scope = body.get("scope", "provider-upstream")
            if not isinstance(scope, str) or not scope.strip():
                raise HTTPException(status_code=400, detail="scope is required")

            token = ledger.mint(
                audience="local-client",
                scope=scope.strip(),
                account=account.strip(),
                ttl_seconds=ttl_seconds,
            )
            return {
                "token": token.token,
                "token_id": token.token_id,
                "account": token.account,
                "scope": scope.strip(),
                "expires_at": token.expires_at,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/standin/info")
    async def standin_info() -> list[dict[str, object]]:
        """Return non-secret metadata for all stand-in tokens.

        Loopback-only. Returns token_id, audience, scope, account,
        issued_at, expires_at, and revoked for each token. Token hashes
        and raw secrets are never included. No authentication required —
        the endpoint exposes only Atelier's own operational metadata
        (not provider credentials) and is loopback-bound.
        """
        return ledger.list_tokens()

    @app.get("/v1/custody/status")
    async def custody_status() -> dict[str, Any]:
        """Return a unified, non-secret view of all credential custody state.

        Combines stand-in token metadata with the underlying provider
        credential status for each unique (scope, account) pair. No
        authentication required (loopback-only, no secrets returned).

        For OAuth-backed accounts (scope=provider-upstream), returns the
        access-token expiry, whether a refresh is needed, and the last
        refresh timestamp — without triggering a refresh.

        For cloud-key-backed accounts (scope=cloud-provider-b, cloud-provider-c, cloud-provider-d,
        cloud-provider-a-key), returns key presence and the non-expiring status.

        For cookie-backed accounts (scope=cloud-provider-a-usage), returns cookie
        jar presence.
        """
        tokens = ledger.list_tokens()

        # Group stand-in tokens by (scope, account) and keep the one
        # with the furthest expiry (the active one).
        active: dict[tuple[str, str], dict[str, object]] = {}
        for t in tokens:
            if t.get("revoked"):
                continue
            key = (t["scope"], t["account"])
            existing = active.get(key)
            if existing is None or t["expires_at"] > existing["expires_at"]:
                active[key] = t

        accounts: list[dict[str, Any]] = []
        for (scope, account), token_meta in sorted(active.items()):
            entry: dict[str, Any] = {
                "standin": {
                    "token_id": token_meta["token_id"],
                    "scope": scope,
                    "account": account,
                    "expires_at": token_meta["expires_at"],
                    "issued_at": token_meta["issued_at"],
                },
                "credential": None,
            }

            if scope == "provider-upstream":
                try:
                    entry["credential"] = await custody.peek(account)
                except Exception:
                    entry["credential"] = {"available": False}
            elif scope in ("cloud-provider-b", "cloud-provider-c", "cloud-provider-d", "cloud-provider-a-key"):
                custody_map = {
                    "cloud-provider-b": cloud_provider_b_key_custody,
                    "cloud-provider-c": cloud_provider_c_key_custody,
                    "cloud-provider-d": cloud_provider_d_key_custody,
                    "cloud-provider-a-key": cloud_key_custody,
                }
                kc = custody_map.get(scope)
                if kc is not None:
                    try:
                        entry["credential"] = await kc.peek(account)
                    except Exception:
                        entry["credential"] = {"available": False}
                else:
                    entry["credential"] = {"available": False, "note": "custody not configured"}
            elif scope == "cloud-provider-a-usage":
                if cookie_custody is not None:
                    try:
                        # Cookie custody doesn't have peek; check existence
                        await cookie_custody.cookies(account)
                        entry["credential"] = {"available": True, "expiry": "session-scoped"}
                    except Exception:
                        entry["credential"] = {"available": False, "expiry": "session-scoped"}
                else:
                    entry["credential"] = {"available": False, "note": "cookie custody not configured"}

            accounts.append(entry)

        return {"accounts": accounts}

    @app.post("/v1/proxy")
    async def proxy_request(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(None),
    ) -> dict[str, Any]:
        """Proxy an unsigned request with credentials injected by Atelier.

        Request: Authorization header with stand-in token, body with url/method/headers/body_b64
        Response: {"status_code": ..., "headers": {...}, "body_b64": "..."}
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)

            url = body.get("url")
            method = body.get("method", "POST")
            headers = body.get("headers", {})
            body_b64 = body.get("body_b64", "")

            if not isinstance(url, str) or not url:
                raise HTTPException(status_code=400, detail="url is required")
            if not isinstance(method, str) or not method:
                raise HTTPException(status_code=400, detail="method is required")

            try:
                request_body = base64.b64decode(body_b64) if body_b64 else b""
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"invalid body_b64: {exc}") from exc

            # Forward through credential proxy
            request = ProxyRequest(
                method=method,
                url=url,
                headers=dict(headers) if headers else {},
                body=request_body,
            )
            response = await proxy.proxy_upstream(
                stand_in_token=stand_in_token,
                request=request,
            )

            return {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body_b64": base64.b64encode(response.body).decode("ascii"),
            }
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CloudKeyNotAvailable:
            raise HTTPException(status_code=503, detail="cloud-provider-a cloud key not provisioned") from None
        except Exception as exc:
            logger.exception("proxy_request failed")
            raise HTTPException(status_code=500, detail="proxy failed") from exc

    @app.post("/v1/proxy/stream")
    async def proxy_stream(
        body: dict[str, Any] = Body(...),
        authorization: str | None = Header(None),
    ) -> StreamingResponse:
        """Proxy a streaming (SSE) request with credentials injected by Atelier.

        Same request body as /v1/proxy. Returns raw upstream SSE bytes.
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)

            url = body.get("url")
            method = body.get("method", "POST")
            headers = body.get("headers", {})
            body_b64 = body.get("body_b64", "")

            if not isinstance(url, str) or not url:
                raise HTTPException(status_code=400, detail="url is required")

            try:
                request_body = base64.b64decode(body_b64) if body_b64 else b""
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"invalid body_b64: {exc}") from exc

            request = ProxyRequest(
                method=method,
                url=url,
                headers=dict(headers) if headers else {},
                body=request_body,
            )
            # Open the upstream stream (validates token, injects credentials). Returns
            # once upstream response headers arrive; the body is streamed lazily below.
            stream = await proxy.open_upstream_stream(
                stand_in_token=stand_in_token,
                request=request,
            )

            async def stream_generator():
                try:
                    async for chunk in stream.aiter_bytes():
                        yield chunk
                finally:
                    await stream.aclose()

            return StreamingResponse(
                stream_generator(),
                headers=_stream_response_headers(stream.status_code, stream.headers),
                media_type="text/event-stream",
                status_code=stream.status_code,
            )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CloudKeyNotAvailable:
            raise HTTPException(status_code=503, detail="cloud-provider-a cloud key not provisioned") from None
        except Exception as exc:
            logger.exception("proxy_stream setup failed")
            raise HTTPException(status_code=500, detail="proxy setup failed") from exc

    @app.get("/v1/cloud-provider-a/usage")
    async def cloud_provider_a_usage_endpoint(
        request: Request,
        authorization: str | None = Header(None),
    ) -> dict:
        """Return read-only cloud-provider-a usage meters for the token's account.

        Loopback-only and stand-in-token-authenticated; the caller must mint a
        token with scope=cloud-provider-a-usage. Cookies are fetched from cookie custody
        and used only inside the final upstream hop; no cookies or upstream HTML
        are returned to the caller.
        """
        if cookie_custody is None or cloud_provider_a_fetch is None:
            raise HTTPException(status_code=503, detail="cloud-provider-a usage not configured")
        try:
            stand_in_token = _extract_bearer_token(authorization)
            record = ledger.validate(
                stand_in_token, audience="local-client", scope=CLOUD_PROVIDER_A_USAGE_SCOPE
            )
            _require_loopback(request.client.host if request.client else None)
            cookies = await cookie_custody.cookies(record.account)
            html = await cloud_provider_a_fetch(cookies)
            return parse_usage(html)
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except CookieNotAvailable:
            raise HTTPException(status_code=502, detail="cloud-provider-a cookies not available") from None
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="cloud-provider-a upstream fetch failed") from None
        except Exception as exc:
            logger.exception("cloud_provider_a_usage failed")
            raise HTTPException(status_code=500, detail="cloud-provider-a usage failed") from exc


    # ------------------------------------------------------------------
    # Transparent reverse-proxy routes for OAuth Provider API-compatible clients.
    #
    # The client backend sends requests to
    # ``{base_url}/responses`` and ``{base_url}/models``.  These routes
    # accept those calls, validate the stand-in token from the Authorization
    # header, inject the real OAuth credential via CredentialProxy, and
    # forward to the upstream OAuth Provider API backend.  The caller never sees
    # the real credential; only the stand-in token traverses the loopback
    # hop from the client to Atelier.
    # ------------------------------------------------------------------

    @app.get("/oauth-provider-api/models")
    async def oauth_provider_api_models(
        request: Request,
        authorization: str | None = Header(None),
    ) -> Response:
        """Transparent reverse proxy for OAuth Provider API model discovery.

        Validates the stand-in Bearer token, injects the real OAuth
        credential, and forwards ``GET /models`` to the upstream OAuth Provider
        OAuth Provider API backend.
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)
            query = request.url.query
            upstream_url = f"{OAUTH_PROVIDER_API_UPSTREAM_BASE}/models"
            if query:
                upstream_url += f"?{query}"
            forwardable = _forwardable_request_headers(request.headers)
            proxy_request = ProxyRequest(
                method="GET",
                url=upstream_url,
                headers=forwardable,
                body=b"",
            )
            response = await proxy.proxy_upstream(
                stand_in_token=stand_in_token,
                request=proxy_request,
            )
            return Response(
                content=response.body,
                status_code=response.status_code,
                headers=_forwardable_response_headers(response.headers),
            )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("oauth_provider_api_models proxy failed")
            raise HTTPException(status_code=500, detail="proxy failed") from exc

    @app.post("/oauth-provider-api/responses")
    async def oauth_provider_api_responses(
        request: Request,
        authorization: str | None = Header(None),
    ) -> StreamingResponse:
        """Transparent streaming reverse proxy for OAuth Provider API responses.

        Validates the stand-in Bearer token, injects the real OAuth
        credential, and forwards ``POST /responses`` to the upstream
        OAuth Provider API backend.  Always streams because callers
        request ``Accept: text/event-stream``.
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)
            body = await request.body()
            forwardable = _forwardable_request_headers(request.headers)
            proxy_request = ProxyRequest(
                method="POST",
                url=f"{OAUTH_PROVIDER_API_UPSTREAM_BASE}/responses",
                headers=forwardable,
                body=body,
            )
            stream = await proxy.open_upstream_stream(
                stand_in_token=stand_in_token,
                request=proxy_request,
            )

            async def stream_generator():
                try:
                    async for chunk in stream.aiter_bytes():
                        yield chunk
                finally:
                    await stream.aclose()

            return StreamingResponse(
                stream_generator(),
                headers=_stream_response_headers(stream.status_code, stream.headers),
                media_type="text/event-stream",
                status_code=stream.status_code,
            )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("oauth_provider_api_responses proxy failed")
            raise HTTPException(status_code=500, detail="proxy failed") from exc


    # ------------------------------------------------------------------
    # Transparent reverse-proxy routes for Cloud Provider B.
    #
    # The Cloud Provider B client can be configured with
    # ``base_url = "http://127.0.0.1:7342/cloud-provider-b/v1"`` and an
    # atelier stand-in token (scope=cloud-provider-b) as the ``api_key``.
    # These routes validate the stand-in token, inject the real
    # Cloud Provider B API key from pass-backed CloudKeyCustody, and forward
    # to the upstream Cloud Provider B API.  The caller never sees the real
    # key; only the stand-in token traverses the loopback hop.
    # ------------------------------------------------------------------

    @app.api_route("/cloud-provider-b/{path:path}", methods=["GET", "POST"], response_model=None)
    async def cloud_provider_b_proxy(
        path: str,
        request: Request,
        authorization: str | None = Header(None),
    ) -> Response | StreamingResponse:
        """Transparent reverse proxy for Cloud Provider B API endpoints.

        Validates the stand-in Bearer token (scope=cloud-provider-b), injects
        the real Cloud Provider B API key, and forwards to
        ``https://example.com/cloud-provider-b/v1/{path}``.

        Streaming is used when the client sends ``Accept:
        text/event-stream``; otherwise the response is buffered.
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)
            body = await request.body()
            query = request.url.query
            upstream_url = f"{CLOUD_PROVIDER_B_UPSTREAM_BASE}/{path}"
            if query:
                upstream_url += f"?{query}"
            forwardable = _forwardable_request_headers(request.headers)
            proxy_request = ProxyRequest(
                method=request.method,
                url=upstream_url,
                headers=forwardable,
                body=body,
            )

            accept = request.headers.get("accept", "")
            wants_stream = "text/event-stream" in accept

            if wants_stream:
                stream = await proxy.open_upstream_stream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )

                async def stream_generator():
                    try:
                        async for chunk in stream.aiter_bytes():
                            yield chunk
                    finally:
                        await stream.aclose()

                return StreamingResponse(
                    stream_generator(),
                    headers=_stream_response_headers(stream.status_code, stream.headers),
                    media_type="text/event-stream",
                    status_code=stream.status_code,
                )
            else:
                response = await proxy.proxy_upstream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )
                return Response(
                    content=response.body,
                    status_code=response.status_code,
                    headers=_forwardable_response_headers(response.headers),
                )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CloudKeyNotAvailable:
            raise HTTPException(
                status_code=503,
                detail="cloud-provider-b key not provisioned in pass",
            ) from None
        except Exception as exc:
            logger.exception("cloud-provider-b proxy failed")
            raise HTTPException(status_code=500, detail="proxy failed") from exc

    # ------------------------------------------------------------------
    # Transparent reverse-proxy routes for Cloud Provider D models.
    #
    # A local client can be configured with
    # ``base_url = "http://127.0.0.1:7342/cloud-provider-d"`` and an
    # atelier stand-in token (scope=cloud-provider-d) as the ``api_key``.
    # These routes validate the stand-in token, inject the real
    # Cloud Provider D API key from pass-backed CloudKeyCustody, and forward
    # to the upstream Cloud Provider D API.  The caller never sees the real
    # key; only the stand-in token traverses the loopback hop.
    # ------------------------------------------------------------------

    @app.api_route("/cloud-provider-d/{path:path}", methods=["GET", "POST"], response_model=None)
    async def cloud_provider_d_proxy(
        path: str,
        request: Request,
        authorization: str | None = Header(None),
    ) -> Response | StreamingResponse:
        """Transparent reverse proxy for Cloud Provider D API endpoints.

        Validates the stand-in Bearer token (scope=cloud-provider-d), injects
        the real Cloud Provider D API key, and forwards to
        ``https://example.com/cloud-provider-d/{path}``.

        Streaming is used when the client sends ``Accept:
        text/event-stream``; otherwise the response is buffered.
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)
            body = await request.body()
            query = request.url.query
            upstream_url = f"{CLOUD_PROVIDER_D_UPSTREAM_BASE}/{path}"
            if query:
                upstream_url += f"?{query}"
            forwardable = _forwardable_request_headers(request.headers)
            proxy_request = ProxyRequest(
                method=request.method,
                url=upstream_url,
                headers=forwardable,
                body=body,
            )

            accept = request.headers.get("accept", "")
            wants_stream = "text/event-stream" in accept

            if wants_stream:
                stream = await proxy.open_upstream_stream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )

                async def stream_generator():
                    try:
                        async for chunk in stream.aiter_bytes():
                            yield chunk
                    finally:
                        await stream.aclose()

                return StreamingResponse(
                    stream_generator(),
                    headers=_stream_response_headers(stream.status_code, stream.headers),
                    media_type="text/event-stream",
                    status_code=stream.status_code,
                )
            else:
                response = await proxy.proxy_upstream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )
                return Response(
                    content=response.body,
                    status_code=response.status_code,
                    headers=_forwardable_response_headers(response.headers),
                )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CloudKeyNotAvailable:
            raise HTTPException(
                status_code=503,
                detail="cloud-provider-d key not provisioned in pass",
            ) from None
        except Exception as exc:
            logger.exception("cloud-provider-d proxy failed")
            raise HTTPException(status_code=500, detail="proxy failed") from exc


    # ------------------------------------------------------------------
    # Transparent reverse-proxy routes for Cloud Provider C build catalog models.
    #
    # A local client can be configured with
    # ``base_url = "http://127.0.0.1:7342/cloud-provider-c"`` and an
    # atelier stand-in token (scope=cloud-provider-c) as the ``api_key``.
    # These routes validate the stand-in token, inject the real
    # Cloud Provider C API key from pass-backed CloudKeyCustody, and forward
    # to the upstream Cloud Provider C API.  The caller never sees the real
    # key; only the stand-in token traverses the loopback hop.
    # ------------------------------------------------------------------

    @app.api_route("/cloud-provider-c/{path:path}", methods=["GET", "POST"], response_model=None)
    async def cloud_provider_c_proxy(
        path: str,
        request: Request,
        authorization: str | None = Header(None),
    ) -> Response | StreamingResponse:
        """Transparent reverse proxy for Cloud Provider C API endpoints.

        Validates the stand-in Bearer token (scope=cloud-provider-c), injects
        the real Cloud Provider C API key, and forwards to
        ``https://example.com/cloud-provider-c/v1/{path}``.

        Streaming is used when the client sends ``Accept:
        text/event-stream``; otherwise the response is buffered.
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)
            body = await request.body()
            query = request.url.query
            upstream_url = f"{CLOUD_PROVIDER_C_UPSTREAM_BASE}/{path}"
            if query:
                upstream_url += f"?{query}"
            forwardable = _forwardable_request_headers(request.headers)
            proxy_request = ProxyRequest(
                method=request.method,
                url=upstream_url,
                headers=forwardable,
                body=body,
            )

            accept = request.headers.get("accept", "")
            wants_stream = "text/event-stream" in accept

            if wants_stream:
                stream = await proxy.open_upstream_stream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )

                async def stream_generator():
                    try:
                        async for chunk in stream.aiter_bytes():
                            yield chunk
                    finally:
                        await stream.aclose()

                return StreamingResponse(
                    stream_generator(),
                    headers=_stream_response_headers(stream.status_code, stream.headers),
                    media_type="text/event-stream",
                    status_code=stream.status_code,
                )
            else:
                response = await proxy.proxy_upstream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )
                return Response(
                    content=response.body,
                    status_code=response.status_code,
                    headers=_forwardable_response_headers(response.headers),
                )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CloudKeyNotAvailable:
            raise HTTPException(
                status_code=503,
                detail="cloud-provider-c key not provisioned in pass",
            ) from None
        except Exception as exc:
            logger.exception("cloud-provider-c proxy failed")
            raise HTTPException(status_code=500, detail="proxy failed") from exc

    # ------------------------------------------------------------------
    # Transparent reverse-proxy routes for Cloud Provider A.
    #
    # A local client can be configured with
    # ``base_url = "http://127.0.0.1:7342/cloud-provider-a-key"`` and an
    # atelier stand-in token (scope=cloud-provider-a-key) as the ``api_key``.
    # These routes validate the stand-in token, inject the real
    # example.com API key from pass-backed CloudKeyCustody, and forward
    # to the upstream example.com API.  The caller never sees the real
    # key; only the stand-in token traverses the loopback hop.
    # ------------------------------------------------------------------

    @app.api_route("/cloud-provider-a-key/{path:path}", methods=["GET", "POST"], response_model=None)
    async def cloud_provider_a_cloud_proxy(
        path: str,
        request: Request,
        authorization: str | None = Header(None),
    ) -> Response | StreamingResponse:
        """Transparent reverse proxy for Cloud Provider A API endpoints.

        Validates the stand-in Bearer token (scope=cloud-provider-a-key), injects
        the real example.com API key, and forwards to
        ``https://example.com/cloud-provider-a/{path}``.

        Streaming is used when the client sends ``Accept:
        text/event-stream``; otherwise the response is buffered.
        """
        try:
            stand_in_token = _extract_bearer_token(authorization)
            body = await request.body()
            query = request.url.query
            upstream_url = f"{CLOUD_PROVIDER_A_UPSTREAM_BASE}/{path}"
            if query:
                upstream_url += f"?{query}"
            forwardable = _forwardable_request_headers(request.headers)
            proxy_request = ProxyRequest(
                method=request.method,
                url=upstream_url,
                headers=forwardable,
                body=body,
            )

            accept = request.headers.get("accept", "")
            wants_stream = "text/event-stream" in accept

            if wants_stream:
                stream = await proxy.open_upstream_stream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )

                async def stream_generator():
                    try:
                        async for chunk in stream.aiter_bytes():
                            yield chunk
                    finally:
                        await stream.aclose()

                return StreamingResponse(
                    stream_generator(),
                    headers=_stream_response_headers(stream.status_code, stream.headers),
                    media_type="text/event-stream",
                    status_code=stream.status_code,
                )
            else:
                response = await proxy.proxy_upstream(
                    stand_in_token=stand_in_token,
                    request=proxy_request,
                )
                return Response(
                    content=response.body,
                    status_code=response.status_code,
                    headers=_forwardable_response_headers(response.headers),
                )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ProxyScopeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CloudKeyNotAvailable:
            raise HTTPException(
                status_code=503,
                detail="cloud-provider-a cloud key not provisioned",
            ) from None
        except Exception as exc:
            logger.exception("cloud-provider-a-key proxy failed")
            raise HTTPException(status_code=500, detail="proxy failed") from exc

    return app


def _extract_bearer_token(authorization: str | None) -> str:
    """Extract the bearer token from an Authorization header.

    Args:
        authorization: Authorization header value (e.g., "Bearer ati_...")

    Returns:
        The bearer token (without "Bearer " prefix)

    Raises:
        PermissionError: If no valid bearer token is provided
    """
    if not authorization:
        raise PermissionError("Authorization header required")
    if not authorization.lower().startswith("bearer "):
        raise PermissionError("Authorization header must be Bearer token")
    token = authorization[7:].strip()
    if not token:
        raise PermissionError("Authorization header missing token")
    return token


def _forwardable_response_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_RESPONSE_HEADERS
    }

def _forwardable_request_headers(headers) -> dict[str, str]:
    """Strip hop-by-hop headers from an incoming request before forwarding.

    ``authorization`` and ``x-oauth-account-id`` are NOT stripped here;
    CredentialProxy._credential_headers handles those by replacing the
    stand-in token with the real OAuth credential.
    """
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_REQUEST_HEADERS
    }


def _stream_response_headers(status_code: int, headers: dict[str, str]) -> dict[str, str]:
    forwardable = _forwardable_response_headers(headers)
    forwardable[UPSTREAM_STATUS_HEADER] = str(status_code)
    return forwardable


class _HttpxUpstreamStream:
    """Adapter wrapping a streaming httpx response as an UpstreamStream."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._response.headers)

    def aiter_bytes(self):
        # aiter_bytes decodes content-encoding (matching the non-stream .content path);
        # the handler strips content-encoding/transfer-encoding response headers.
        return self._response.aiter_bytes()

    async def aclose(self) -> None:
        await self._response.aclose()


class _HttpTransport:
    """Transport adapter for CredentialProxy using httpx.AsyncClient."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, request: ProxyRequest) -> ProxyResponse:
        """Send an HTTP request and buffer the full response body."""
        httpx_request = self._client.build_request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            content=request.body,
        )
        httpx_response = await self._client.send(httpx_request)
        return ProxyResponse(
            status_code=httpx_response.status_code,
            headers=dict(httpx_response.headers),
            body=httpx_response.content,
        )

    async def open_stream(self, request: ProxyRequest) -> _HttpxUpstreamStream:
        """Open a streaming upstream request; body is iterated chunk-by-chunk."""
        httpx_request = self._client.build_request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            content=request.body,
            # Long SSE streams can idle between tokens; don't kill them on read.
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0),
        )
        httpx_response = await self._client.send(httpx_request, stream=True)
        return _HttpxUpstreamStream(httpx_response)
