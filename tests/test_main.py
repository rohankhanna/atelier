from __future__ import annotations

from atelier import __main__ as main_mod
from atelier.pass_store import PassStore


def test_create_custody_from_env_defaults_to_pass_store_and_oauth_provider(monkeypatch) -> None:
    monkeypatch.delenv("ATELIER_REFRESH_URL", raising=False)
    monkeypatch.delenv("ATELIER_CLIENT_ID", raising=False)
    monkeypatch.delenv("ATELIER_PASS_PATH_PREFIX", raising=False)

    custody = main_mod.create_custody_from_env()

    assert isinstance(custody._store, PassStore)
    assert custody._refresh_url == "https://auth.example.com/oauth/token"
    assert custody._client_id == ""
    assert custody._path_prefix == "llm/oauth-provider/accounts"


def test_create_custody_from_env_allows_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ATELIER_REFRESH_URL", "https://example.invalid/oauth/token")
    monkeypatch.setenv("ATELIER_CLIENT_ID", "CLIENT_ID_OVERRIDE")
    monkeypatch.setenv("ATELIER_PASS_PATH_PREFIX", "llm/example/accounts")

    custody = main_mod.create_custody_from_env()

    assert custody._refresh_url == "https://example.invalid/oauth/token"
    assert custody._client_id == "CLIENT_ID_OVERRIDE"
    assert custody._path_prefix == "llm/example/accounts"
