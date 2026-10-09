"""Read-only custody for non-OAuth provider API keys (e.g. example.com cloud keys).

Mirrors the structural shape of cookie_custody.CookieCustody but reads a bare
single-line key string instead of a JSON cookie jar. A provider API key is a
high-sensitivity, non-expiring, revocable credential: this custody never logs or
returns it beyond the single string handed to the final-hop credential injector,
and it is never returned to clients. There is no refresh flow; rotation is the
operator replacing the pass entry.
"""
from __future__ import annotations

import asyncio

from atelier.oauth_custody import TokenStore
from atelier.pass_store import PassStoreError


class CloudKeyNotAvailable(RuntimeError):
    """Raised when a cloud key is missing or empty from pass.

    The message carries only the caller-supplied account name (not a secret) for
    server-side diagnosis; HTTP responses map this to a generic 503 body.
    """


class CloudKeyCustody:
    """Loads a provider API key from pass for a given account.

    The key lives at `{path_prefix}/{account}/cloud-key` and is a bare string
    (single line, not a JSON object). Keys are high-sensitivity credentials: this
    class never logs them and returns them only as the in-process string handed to
    the final-hop header injector.
    """

    def __init__(
        self,
        *,
        store: TokenStore,
        path_prefix: str = "llm/cloud-provider-a/accounts",
    ) -> None:
        self._store = store
        self._path_prefix = path_prefix.rstrip("/")

    async def key(self, account: str) -> str:
        # Validate the segment first so a bad account raises ValueError before
        # any subprocess is spawned.
        path = self._pass_path(account)
        return await asyncio.to_thread(self._key_locked, account, path)

    async def peek(self, account: str) -> dict[str, object]:
        """Return non-secret status for a cloud-key account.

        Checks whether a key exists and is non-empty in pass without returning
        the key value. Cloud keys are revocable but non-expiring, so there is
        no expiry timestamp — only presence.
        """
        path = self._pass_path(account)
        return await asyncio.to_thread(self._peek_locked, account, path)

    def _peek_locked(self, account: str, path: str) -> dict[str, object]:
        try:
            raw = self._store.show(path)
        except PassStoreError:
            return {"key_present": False, "expiry": "non-expiring", "available": False}
        key = raw.strip()
        return {
            "key_present": bool(key),
            "expiry": "non-expiring",
            "available": bool(key),
        }

    def _key_locked(self, account: str, path: str) -> str:
        try:
            raw = self._store.show(path)
        except PassStoreError as exc:
            raise CloudKeyNotAvailable(
                f"cloud key not available for account {account!r}"
            ) from exc
        key = raw.strip()
        if not key:
            raise CloudKeyNotAvailable(
                f"cloud key empty for account {account!r}"
            ) from None
        return key

    def _pass_path(self, account: str) -> str:
        account = account.strip().strip("/")
        if not account or "/" in account:
            raise ValueError("account must be a single pass path segment")
        return f"{self._path_prefix}/{account}/cloud-key"