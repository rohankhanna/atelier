from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

REFRESH_SAFETY_WINDOW_SECONDS = 5 * 60


class TokenRefreshError(RuntimeError):
    pass


class TokenStore(Protocol):
    def show(self, path: str) -> str:
        raise NotImplementedError

    def insert_multiline(self, path: str, value: str) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class AccessGrant:
    access_token: str
    account_id: str | None
    expires_at: int | None


@dataclass(frozen=True)
class OAuthBundle:
    access_token: str
    refresh_token: str
    id_token: str | None
    account_id: str | None
    expires_at: int | None
    raw: dict[str, Any]


class OAuthCustody:
    def __init__(
        self,
        *,
        store: TokenStore,
        refresh_url: str,
        client_id: str,
        path_prefix: str = "llm/provider/accounts",
        now: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._path_prefix = path_prefix.rstrip("/")
        self._refresh_url = refresh_url
        self._client_id = client_id
        self._now = now or time.time

    def access_grant(self, account: str) -> AccessGrant:
        path = self._pass_path(account)
        bundle = parse_auth_json(self._store.show(path))
        if should_refresh(bundle, now=self._now()):
            bundle = self._refresh(bundle)
            self._store.insert_multiline(path, serialize_auth_json(bundle))
        return AccessGrant(
            access_token=bundle.access_token,
            account_id=bundle.account_id,
            expires_at=bundle.expires_at,
        )

    def _pass_path(self, account: str) -> str:
        account = account.strip().strip("/")
        if not account or "/" in account:
            raise ValueError("account must be a single pass path segment")
        return f"{self._path_prefix}/{account}/auth-json"

    def _refresh(self, bundle: OAuthBundle) -> OAuthBundle:
        payload = {
            "client_id": self._client_id,
            "grant_type": "refresh_token",
            "refresh_token": bundle.refresh_token,
        }
        request = urllib.request.Request(
            self._refresh_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise TokenRefreshError(f"refresh rejected: {exc.code} {detail}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TokenRefreshError(f"refresh failed: {exc}") from exc

        access_token = response_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise TokenRefreshError("refresh response missing access_token")
        refresh_token = response_payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = bundle.refresh_token
        id_token = response_payload.get("id_token")
        if not isinstance(id_token, str):
            id_token = bundle.id_token
        account_id = _account_id_from_id_token(id_token) or bundle.account_id

        raw = dict(bundle.raw)
        tokens = raw.get("tokens")
        if not isinstance(tokens, dict):
            tokens = {}
        tokens["access_token"] = access_token
        tokens["refresh_token"] = refresh_token
        if id_token is not None:
            tokens["id_token"] = id_token
        if account_id is not None:
            tokens["account_id"] = account_id
        raw["tokens"] = tokens
        raw["last_refresh"] = _format_timestamp(self._now())
        return parse_auth_json(json.dumps(raw))


def parse_auth_json(raw: str) -> OAuthBundle:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("auth-json pass entry is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("auth-json pass entry must be a JSON object")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("auth-json pass entry is missing tokens")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("auth-json pass entry is missing access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("auth-json pass entry is missing refresh_token")
    id_token_raw = tokens.get("id_token")
    id_token = id_token_raw if isinstance(id_token_raw, str) else None
    account_id_raw = tokens.get("account_id")
    account_id = account_id_raw if isinstance(account_id_raw, str) else None
    if account_id is None:
        account_id = _account_id_from_id_token(id_token)
    return OAuthBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=account_id,
        expires_at=_jwt_expiration(access_token),
        raw=payload,
    )


def serialize_auth_json(bundle: OAuthBundle) -> str:
    return json.dumps(bundle.raw, indent=2, sort_keys=True)


def should_refresh(bundle: OAuthBundle, *, now: float) -> bool:
    if bundle.expires_at is None:
        return False
    return bundle.expires_at - now <= REFRESH_SAFETY_WINDOW_SECONDS


def _format_timestamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _jwt_expiration(token: str) -> int | None:
    claims = _decode_jwt_claims(token)
    if claims is None:
        return None
    exp = claims.get("exp")
    return exp if isinstance(exp, int) else None


def _account_id_from_id_token(token: str | None) -> str | None:
    claims = _decode_jwt_claims(token)
    if claims is None:
        return None
    value = claims.get("https://example.com/auth")
    if isinstance(value, dict):
        account_id = value.get("oauth_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    return None


def _decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    if token is None:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None
