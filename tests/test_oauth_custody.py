from __future__ import annotations

import asyncio
import base64
import json
import threading
import time

import pytest

from atelier.oauth_custody import OAuthCustody, parse_auth_json, should_refresh

pytestmark = pytest.mark.anyio


class MemoryStore:
    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = entries
        self.writes: list[tuple[str, str]] = []

    def show(self, path: str) -> str:
        return self.entries[path]

    def insert_multiline(self, path: str, value: str) -> None:
        self.entries[path] = value
        self.writes.append((path, value))


def test_parse_auth_json_extracts_expiry_and_account_id() -> None:
    raw = json.dumps(
        {
            "tokens": {
                "access_token": _jwt({"exp": 2000}),
                "refresh_token": "REFRESH_TOKEN_PLACEHOLDER",
                "id_token": _jwt(
                    {
                        "https://example.com/auth": {
                            "oauth_account_id": "account-placeholder"
                        }
                    }
                ),
            }
        }
    )

    bundle = parse_auth_json(raw)

    assert bundle.expires_at == 2000
    assert bundle.account_id == "account-placeholder"


async def test_access_grant_does_not_restore_when_access_token_is_fresh() -> None:
    path = "llm/provider/accounts/test-account/auth-json"
    store = MemoryStore(
        {
            path: json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt({"exp": 5000}),
                        "refresh_token": "REFRESH_TOKEN_PLACEHOLDER",
                        "account_id": "test-account",
                    }
                }
            )
        }
    )
    custody = OAuthCustody(
        store=store,
        refresh_url="https://provider.example/oauth/token",
        client_id="CLIENT_ID_PLACEHOLDER",
        now=lambda: 1000,
    )

    grant = await custody.access_grant("test-account")

    assert grant.access_token
    assert grant.account_id == "test-account"
    assert store.writes == []


def test_should_refresh_inside_safety_window() -> None:
    bundle = parse_auth_json(
        json.dumps(
            {
                "tokens": {
                    "access_token": _jwt({"exp": 1200}),
                    "refresh_token": "REFRESH_TOKEN_PLACEHOLDER",
                }
            }
        )
    )

    assert should_refresh(bundle, now=1000)


async def test_access_grant_refreshes_and_restores_rotated_refresh_token(monkeypatch) -> None:
    path = "llm/provider/accounts/test-account/auth-json"
    store = MemoryStore(
        {
            path: json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt({"exp": 1001}),
                        "refresh_token": "OLD_REFRESH_TOKEN_PLACEHOLDER",
                        "account_id": "test-account",
                    }
                }
            )
        }
    )
    custody = OAuthCustody(
        store=store,
        refresh_url="https://provider.example/oauth/token",
        client_id="CLIENT_ID_PLACEHOLDER",
        now=lambda: 1000,
    )

    def fake_refresh(bundle):
        raw = dict(bundle.raw)
        raw["tokens"] = {
            "access_token": _jwt({"exp": 5000}),
            "refresh_token": "NEW_REFRESH_TOKEN_PLACEHOLDER",
            "account_id": "test-account",
        }
        return parse_auth_json(json.dumps(raw))

    monkeypatch.setattr(custody, "_refresh", fake_refresh)

    grant = await custody.access_grant("test-account")
    restored = json.loads(store.writes[0][1])

    assert grant.expires_at == 5000
    assert store.writes[0][0] == path
    assert restored["tokens"]["refresh_token"] == "NEW_REFRESH_TOKEN_PLACEHOLDER"


async def test_account_id_must_be_single_path_segment() -> None:
    custody = OAuthCustody(
        store=MemoryStore({}),
        refresh_url="https://provider.example/oauth/token",
        client_id="CLIENT_ID_PLACEHOLDER",
    )

    try:
        await custody.access_grant("../test-account")
    except ValueError as exc:
        assert "single pass path segment" in str(exc)
    else:
        raise AssertionError("path traversal account id was accepted")


async def test_access_grant_serializes_refresh_for_same_account(monkeypatch) -> None:
    path = "llm/provider/accounts/test-account/auth-json"
    store = MemoryStore(
        {
            path: json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt({"exp": 1001}),
                        "refresh_token": "OLD_REFRESH_TOKEN_PLACEHOLDER",
                        "account_id": "test-account",
                    }
                }
            )
        }
    )
    custody = OAuthCustody(
        store=store,
        refresh_url="https://provider.example/oauth/token",
        client_id="CLIENT_ID_PLACEHOLDER",
        now=lambda: 1000,
    )
    active_refreshes = 0
    max_active_refreshes = 0
    refresh_count = 0

    def fake_refresh(bundle):
        nonlocal active_refreshes, max_active_refreshes, refresh_count
        active_refreshes += 1
        max_active_refreshes = max(max_active_refreshes, active_refreshes)
        refresh_count += 1
        time.sleep(0.01)
        raw = dict(bundle.raw)
        raw["tokens"] = {
            "access_token": _jwt({"exp": 5000}),
            "refresh_token": f"NEW_REFRESH_TOKEN_PLACEHOLDER_{refresh_count}",
            "account_id": "test-account",
        }
        active_refreshes -= 1
        return parse_auth_json(json.dumps(raw))

    monkeypatch.setattr(custody, "_refresh", fake_refresh)

    await asyncio.gather(custody.access_grant("test-account"), custody.access_grant("test-account"))

    assert max_active_refreshes == 1
    assert len(store.writes) == 1


async def test_access_grant_does_not_serialize_different_accounts(monkeypatch) -> None:
    paths = {
        "test-account": "llm/provider/accounts/test-account/auth-json",
        "account-b": "llm/provider/accounts/account-b/auth-json",
    }
    store = MemoryStore(
        {
            path: json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt({"exp": 1001}),
                        "refresh_token": f"OLD_REFRESH_TOKEN_PLACEHOLDER_{account}",
                        "account_id": account,
                    }
                }
            )
            for account, path in paths.items()
        }
    )
    custody = OAuthCustody(
        store=store,
        refresh_url="https://provider.example/oauth/token",
        client_id="CLIENT_ID_PLACEHOLDER",
        now=lambda: 1000,
    )
    entered: set[str] = set()
    entered_lock = threading.Lock()
    both_entered = threading.Event()

    async def call(account: str) -> None:
        await custody.access_grant(account)

    def fake_refresh(bundle):
        account = bundle.account_id or "unknown"
        with entered_lock:
            entered.add(account)
            if len(entered) == 2:
                both_entered.set()
        if account == "test-account":
            assert both_entered.wait(timeout=1)
        raw = dict(bundle.raw)
        raw["tokens"] = {
            "access_token": _jwt({"exp": 5000}),
            "refresh_token": f"NEW_REFRESH_TOKEN_PLACEHOLDER_{account}",
            "account_id": account,
        }
        return parse_auth_json(json.dumps(raw))

    monkeypatch.setattr(custody, "_refresh", fake_refresh)
    await asyncio.gather(call("test-account"), call("account-b"))

    assert entered == {"test-account", "account-b"}
    assert len(store.writes) == 2


def _jwt(claims: dict) -> str:
    header = _b64({"alg": "none"})
    payload = _b64(claims)
    return f"{header}.{payload}."


def _b64(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
