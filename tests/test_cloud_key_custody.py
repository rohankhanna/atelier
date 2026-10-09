"""Tests for CloudKeyCustody: read-only provider API key loading from pass.

Synthetic fixtures only. Key values are obvious placeholders.
"""
from __future__ import annotations

import pytest

from atelier.cloud_key_custody import CloudKeyCustody, CloudKeyNotAvailable

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


async def test_cloud_key_happy_path() -> None:
    store = MemoryStore(
        {"llm/cloud-provider-a/accounts/test-account/cloud-key": "SYNTHETIC_CLOUD_KEY"}
    )
    custody = CloudKeyCustody(store=store)
    key = await custody.key("test-account")
    assert key == "SYNTHETIC_CLOUD_KEY"
    assert store.shown == ["llm/cloud-provider-a/accounts/test-account/cloud-key"]


async def test_cloud_key_missing_entry_raises_not_available() -> None:
    custody = CloudKeyCustody(store=PassErrorStore({}))
    with pytest.raises(CloudKeyNotAvailable):
        await custody.key("test-account")


async def test_cloud_key_empty_raises_not_available() -> None:
    store = MemoryStore({"llm/cloud-provider-a/accounts/test-account/cloud-key": ""})
    custody = CloudKeyCustody(store=store)
    with pytest.raises(CloudKeyNotAvailable):
        await custody.key("test-account")


async def test_cloud_key_whitespace_only_raises_not_available() -> None:
    store = MemoryStore({"llm/cloud-provider-a/accounts/test-account/cloud-key": "   \n\t  "})
    custody = CloudKeyCustody(store=store)
    with pytest.raises(CloudKeyNotAvailable):
        await custody.key("test-account")


async def test_cloud_key_strips_surrounding_whitespace() -> None:
    store = MemoryStore(
        {"llm/cloud-provider-a/accounts/test-account/cloud-key": "  SYNTHETIC_CLOUD_KEY  \n"}
    )
    custody = CloudKeyCustody(store=store)
    key = await custody.key("test-account")
    assert key == "SYNTHETIC_CLOUD_KEY"


def test_cloud_key_account_must_be_single_path_segment() -> None:
    custody = CloudKeyCustody(store=MemoryStore({}))
    with pytest.raises(ValueError):
        custody._pass_path("../x")
    with pytest.raises(ValueError):
        custody._pass_path("")
    with pytest.raises(ValueError):
        custody._pass_path("a/b")


def test_cloud_key_pass_path_uses_cloud_provider_a_prefix() -> None:
    store = MemoryStore({"llm/cloud-provider-a/accounts/test-account/cloud-key": "SYNTHETIC"})
    custody = CloudKeyCustody(store=store)
    assert custody._pass_path("test-account") == "llm/cloud-provider-a/accounts/test-account/cloud-key"


def test_cloud_key_pass_path_respects_custom_prefix() -> None:
    custody = CloudKeyCustody(store=MemoryStore({}), path_prefix="custom/cloud-provider-a")
    assert custody._pass_path("test-account") == "custom/cloud-provider-a/test-account/cloud-key"