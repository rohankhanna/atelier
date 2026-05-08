from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_STANDIN_TTL_SECONDS = 300
MAX_STANDIN_TTL_SECONDS = 1800


@dataclass(frozen=True)
class StandInToken:
    token: str
    token_id: str
    audience: str
    scope: str
    account: str
    issued_at: int
    expires_at: int
    revoked: bool = False


@dataclass(frozen=True)
class StandInRecord:
    token_id: str
    token_hash: str
    audience: str
    scope: str
    account: str
    issued_at: int
    expires_at: int
    revoked: bool = False

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class StandInTokenLedger:
    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.time
        self._records: dict[str, StandInRecord] = {}

    def mint(
        self,
        *,
        audience: str,
        scope: str,
        account: str,
        ttl_seconds: int = DEFAULT_STANDIN_TTL_SECONDS,
    ) -> StandInToken:
        audience = _required_segment("audience", audience)
        scope = _required_segment("scope", scope)
        account = _required_segment("account", account)
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if ttl_seconds > MAX_STANDIN_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be <= {MAX_STANDIN_TTL_SECONDS}")

        issued_at = int(self._now())
        token_id = secrets.token_hex(12)
        token = f"ati_{token_id}_{secrets.token_urlsafe(32)}"
        self._records[token_id] = StandInRecord(
            token_id=token_id,
            token_hash=_hash_secret(token),
            audience=audience,
            scope=scope,
            account=account,
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
        )
        return StandInToken(
            token=token,
            token_id=token_id,
            audience=audience,
            scope=scope,
            account=account,
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
        )

    def validate(self, token: str, *, audience: str, scope: str) -> StandInRecord:
        token_id = _token_id(token)
        record = self._records.get(token_id)
        if record is None or record.token_hash != _hash_secret(token):
            raise PermissionError("stand-in token is invalid")
        if record.revoked:
            raise PermissionError("stand-in token is revoked")
        if record.is_expired(self._now()):
            raise PermissionError("stand-in token is expired")
        if record.audience != audience:
            raise PermissionError("stand-in token audience mismatch")
        if record.scope != scope:
            raise PermissionError("stand-in token scope mismatch")
        return record

    def revoke(self, token_id: str) -> None:
        record = self._records.get(token_id)
        if record is None:
            raise LookupError(f"stand-in token not found: {token_id}")
        self._records[token_id] = StandInRecord(
            token_id=record.token_id,
            token_hash=record.token_hash,
            audience=record.audience,
            scope=record.scope,
            account=record.account,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            revoked=True,
        )


def _token_id(token: str) -> str:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != "ati" or not parts[1]:
        raise PermissionError("stand-in token is malformed")
    return parts[1]


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _required_segment(name: str, value: str) -> str:
    value = value.strip()
    if not value or "/" in value:
        raise ValueError(f"{name} must be a non-empty single path segment")
    return value
