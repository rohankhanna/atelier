"""Stand-in token ledger with optional disk persistence.

Tokens are stored as ``StandInRecord`` (which contains only a SHA-256 hash
of the token, never the raw token itself).  When a ``persist_path`` is
provided, the ledger loads on construction and saves on every ``mint`` and
``revoke``, so tokens survive process restarts.

The on-disk format is a JSON array of record dicts.  Expired records are
pruned on load to keep the file from growing without bound.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger("atelier.standin")

DEFAULT_STANDIN_TTL_SECONDS = 300
MAX_STANDIN_TTL_SECONDS = 2592000  # 30 days — clients may use long-lived stand-in tokens

# Default persistence location, overridable via environment variable.
DEFAULT_PERSIST_DIR = os.path.expanduser("~/.local/state/atelier")


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
    """In-memory stand-in token ledger with optional disk persistence.

    Parameters
    ----------
    now:
        Clock callable (defaults to ``time.time``).  Injected for tests.
    persist_path:
        Path to a JSON file for persistence.  When provided, records are
        loaded on construction and saved atomically on every ``mint`` and
        ``revoke``.  Expired records are pruned on load.  When ``None``
        (the default), the ledger is purely in-memory — same as before.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        persist_path: str | os.PathLike | None = None,
    ) -> None:
        self._now = now or time.time
        self._records: dict[str, StandInRecord] = {}
        self._persist_path: Path | None = Path(persist_path) if persist_path else None
        if self._persist_path is not None:
            self._load_from_disk()

    # ---- persistence --------------------------------------------------

    def _load_from_disk(self) -> None:
        """Load records from the persist file, pruning expired ones."""
        assert self._persist_path is not None
        if not self._persist_path.exists():
            return
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "stand-in token persist file %s is corrupt or unreadable (%s); "
                "starting with an empty ledger. Existing stand-in tokens are no "
                "longer valid and must be re-minted.",
                self._persist_path,
                exc,
            )
            return
        if not isinstance(data, list):
            logger.warning(
                "stand-in token persist file %s has unexpected top-level type "
                "(expected list); starting with an empty ledger.",
                self._persist_path,
            )
            return
        now = self._now()
        loaded = 0
        pruned = 0
        for item in data:
            rec = _record_from_dict(item)
            if rec is None:
                continue
            if rec.is_expired(now) or rec.revoked:
                pruned += 1
                continue
            self._records[rec.token_id] = rec
            loaded += 1
        if loaded > 0:
            logger.info(
                "loaded %d stand-in token(s) from %s (pruned %d expired/revoked)",
                loaded,
                self._persist_path,
                pruned,
            )
        # If we pruned any expired/revoked records, rewrite the file so it
        # stays clean.
        if pruned > 0:
            self._save_to_disk()

    def _save_to_disk(self) -> None:
        """Atomically write all non-expired, non-revoked records to disk."""
        assert self._persist_path is not None
        now = self._now()
        records = [
            asdict(r)
            for r in self._records.values()
            if not r.is_expired(now) and not r.revoked
        ]
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: temp file in the same directory, then rename.
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._persist_path.parent),
                prefix=".standin-tokens-",
                suffix=".tmp",
            )
            os.write(fd, json.dumps(records, indent=2).encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, self._persist_path)
        except OSError as exc:
            logger.error("failed to persist stand-in tokens to %s: %s", self._persist_path, exc)

    # ---- public API ---------------------------------------------------

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
        if self._persist_path is not None:
            self._save_to_disk()
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
            raise PermissionError(
                "stand-in token is invalid — the token was not minted by this "
                "atelier instance or has been removed. Re-mint via "
                "POST /v1/standin with {\"account\": \"<account>\", "
                "\"scope\": \"provider-upstream\"}."
            )
        if record.revoked:
            raise PermissionError(
                "stand-in token is revoked. Re-mint via POST /v1/standin "
                "with {\"account\": \"<account>\", \"scope\": \"provider-upstream\"}."
            )
        if record.is_expired(self._now()):
            raise PermissionError(
                f"stand-in token for account '{record.account}' (scope "
                f"'{record.scope}') expired at {record.expires_at}. Re-mint "
                f"a new token via POST /v1/standin with "
                f"{{\"account\": \"{record.account}\", \"scope\": \"{record.scope}\"}}."
            )
        if record.audience != audience:
            raise PermissionError("stand-in token audience mismatch")
        if record.scope != scope:
            raise PermissionError("stand-in token scope mismatch")
        return record

    def validate_any(
        self, token: str, *, audience: str, scopes: set[str]
    ) -> StandInRecord:
        """Validate a stand-in token against an allowed set of scopes.

        Same checks as ``validate`` (invalid/revoked/expired/audience-mismatch
        → ``PermissionError``) but accepts any scope in *scopes*; the token's
        scope is returned on the record so callers can dispatch on it.  Used
        by the credential proxy to accept {provider-upstream, cloud-provider-a-key}
        and branch.
        """
        token_id = _token_id(token)
        record = self._records.get(token_id)
        if record is None or record.token_hash != _hash_secret(token):
            raise PermissionError(
                "stand-in token is invalid — the token was not minted by this "
                "atelier instance or has been removed. Re-mint via "
                "POST /v1/standin with {\"account\": \"<account>\", "
                "\"scope\": \"provider-upstream\"}."
            )
        if record.revoked:
            raise PermissionError(
                "stand-in token is revoked. Re-mint via POST /v1/standin "
                "with {\"account\": \"<account>\", \"scope\": \"provider-upstream\"}."
            )
        if record.is_expired(self._now()):
            raise PermissionError(
                f"stand-in token for account '{record.account}' (scope "
                f"'{record.scope}') expired at {record.expires_at}. Re-mint "
                f"a new token via POST /v1/standin with "
                f"{{\"account\": \"{record.account}\", \"scope\": \"{record.scope}\"}}."
            )
        if record.audience != audience:
            raise PermissionError("stand-in token audience mismatch")
        if record.scope not in scopes:
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
        if self._persist_path is not None:
            self._save_to_disk()

    def list_tokens(self) -> list[dict[str, object]]:
        """Return non-secret metadata for all stand-in tokens.

        Each entry contains ``token_id``, ``audience``, ``scope``,
        ``account``, ``issued_at``, ``expires_at``, and ``revoked``.
        The token hash and raw token secret are never included.

        Expired and revoked tokens are included so callers can display
        full lifecycle state; callers filter as needed.
        """
        return [
            {
                "token_id": r.token_id,
                "audience": r.audience,
                "scope": r.scope,
                "account": r.account,
                "issued_at": r.issued_at,
                "expires_at": r.expires_at,
                "revoked": r.revoked,
            }
            for r in self._records.values()
        ]


def _record_from_dict(data: object) -> StandInRecord | None:
    """Reconstruct a StandInRecord from a JSON-deserialised dict.

    Returns ``None`` if the dict is malformed (missing keys, wrong types).
    """
    if not isinstance(data, dict):
        return None
    try:
        return StandInRecord(
            token_id=str(data["token_id"]),
            token_hash=str(data["token_hash"]),
            audience=str(data["audience"]),
            scope=str(data["scope"]),
            account=str(data["account"]),
            issued_at=int(data["issued_at"]),
            expires_at=int(data["expires_at"]),
            revoked=bool(data.get("revoked", False)),
        )
    except (KeyError, TypeError, ValueError):
        return None


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
