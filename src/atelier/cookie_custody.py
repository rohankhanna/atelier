"""Read-only custody for non-OAuth browser session cookies (e.g. example.com).

Mirrors the structural shape of oauth_custody.OAuthCustody but without refresh,
write-back, or locking: cookies are session-scoped credentials with no refresh
flow, and `pass show` is a read-only subprocess safe to run concurrently. The
rotation path is the operator-run re-extract CLI (atelier.cloud_provider_a_reextract),
which writes a fresh jar to pass; this custody simply loads it.
"""
from __future__ import annotations

import asyncio

from atelier.oauth_custody import TokenStore
from atelier.cloud_provider_a_usage import parse_cookie_jar
from atelier.pass_store import PassStoreError


class CookieNotAvailable(RuntimeError):
    """Raised when a cookie jar is missing or unreadable from pass.

    The message carries only the caller-supplied account name (not a secret) for
    server-side diagnosis; HTTP responses map this to a generic 502 body.
    """


class CookieCustody:
    """Loads a session-cookie jar from pass for a given account.

    The jar lives at `{path_prefix}/{account}/cookies` and is a JSON object
    {name: value}. Cookies are session credentials: this class never logs or
    returns them beyond the in-process dict handed to the final-hop fetcher.
    """

    def __init__(
        self,
        *,
        store: TokenStore,
        path_prefix: str = "llm/cloud-provider-a/accounts",
    ) -> None:
        self._store = store
        self._path_prefix = path_prefix.rstrip("/")

    async def cookies(self, account: str) -> dict[str, str]:
        # Validate the segment first so a bad account raises ValueError before
        # any subprocess is spawned.
        path = self._pass_path(account)
        return await asyncio.to_thread(self._cookies_locked, account, path)

    def _cookies_locked(self, account: str, path: str) -> dict[str, str]:
        try:
            raw = self._store.show(path)
        except PassStoreError as exc:
            raise CookieNotAvailable(f"cookie jar not available for account {account!r}") from exc
        try:
            return parse_cookie_jar(raw)
        except ValueError as exc:
            raise CookieNotAvailable(f"cookie jar unreadable for account {account!r}") from exc

    def _pass_path(self, account: str) -> str:
        account = account.strip().strip("/")
        if not account or "/" in account:
            raise ValueError("account must be a single pass path segment")
        return f"{self._path_prefix}/{account}/cookies"