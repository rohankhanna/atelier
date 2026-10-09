"""Tests for CookieCustody: read-only session-cookie jar loading from pass.

Synthetic fixtures only. Cookie values are obvious placeholders.
"""
from __future__ import annotations

import json

import pytest

from atelier.cookie_custody import CookieCustody, CookieNotAvailable

pytestmark = pytest.mark.anyio


class MemoryStore:
    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = entries
        self.shown: list[str] = []

    def show(self, path: str) -> str:
        self.shown.append(path)
        if path not in self.entries:
            raise KeyError(path)  # PassStore raises on missing; fakes raise similarly
        return self.entries[path]

    def insert_multiline(self, path: str, value: str) -> None:
        self.entries[path] = value


class PassErrorStore(MemoryStore):
    """Simulates PassStore raising PassStoreError on a missing entry."""

    def show(self, path: str) -> str:
        self.shown.append(path)
        from atelier.pass_store import PassStoreError

        raise PassStoreError(f"pass show failed for {path!r}")


def _jar(cookies: dict[str, str]) -> str:
    return json.dumps(cookies, indent=2, sort_keys=True)


async def test_cookies_happy_path() -> None:
    store = MemoryStore(
        {"llm/cloud-provider-a/accounts/test-account/cookies": _jar({"__Secure-session": "SYNTHETIC"})}
    )
    custody = CookieCustody(store=store)
    cookies = await custody.cookies("test-account")
    assert cookies == {"__Secure-session": "SYNTHETIC"}
    assert store.shown == ["llm/cloud-provider-a/accounts/test-account/cookies"]


async def test_cookies_missing_entry_raises_cookie_not_available() -> None:
    custody = CookieCustody(store=PassErrorStore({}))
    with pytest.raises(CookieNotAvailable):
        await custody.cookies("test-account")


async def test_cookies_unreadable_jar_raises_cookie_not_available() -> None:
    store = MemoryStore({"llm/cloud-provider-a/accounts/test-account/cookies": "not json"})
    custody = CookieCustody(store=store)
    with pytest.raises(CookieNotAvailable):
        await custody.cookies("test-account")


def test_account_must_be_single_path_segment() -> None:
    custody = CookieCustody(store=MemoryStore({}))
    with pytest.raises(ValueError):
        custody._pass_path("../x")
    with pytest.raises(ValueError):
        custody._pass_path("")
    with pytest.raises(ValueError):
        custody._pass_path("a/b")


def test_pass_path_uses_cloud_provider_a_prefix() -> None:
    store = MemoryStore({"llm/cloud-provider-a/accounts/test-account/cookies": _jar({})})
    custody = CookieCustody(store=store)
    # exercise the path builder directly
    assert custody._pass_path("test-account") == "llm/cloud-provider-a/accounts/test-account/cookies"


def test_pass_path_respects_custom_prefix() -> None:
    custody = CookieCustody(store=MemoryStore({}), path_prefix="custom/cloud-provider-a")
    assert custody._pass_path("test-account") == "custom/cloud-provider-a/test-account/cookies"