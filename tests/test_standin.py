from __future__ import annotations

import pytest

from atelier.standin import StandInTokenLedger


def test_minted_stand_in_token_validates_for_matching_audience_and_scope() -> None:
    ledger = StandInTokenLedger(now=lambda: 1000)

    token = ledger.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=60,
    )
    record = ledger.validate(token.token, audience="local-client", scope="provider-upstream")

    assert token.token.startswith("ati_")
    assert record.account == "test-account"
    assert record.token_hash != token.token


def test_stand_in_token_rejects_wrong_scope() -> None:
    ledger = StandInTokenLedger(now=lambda: 1000)
    token = ledger.mint(audience="local-client", scope="provider-upstream", account="test-account")

    with pytest.raises(PermissionError, match="scope mismatch"):
        ledger.validate(token.token, audience="local-client", scope="other")


def test_stand_in_token_expires() -> None:
    now = 1000
    ledger = StandInTokenLedger(now=lambda: now)
    token = ledger.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=1,
    )
    now = 1002

    with pytest.raises(PermissionError, match="expired"):
        ledger.validate(token.token, audience="local-client", scope="provider-upstream")


def test_stand_in_token_revokes_by_id() -> None:
    ledger = StandInTokenLedger(now=lambda: 1000)
    token = ledger.mint(audience="local-client", scope="provider-upstream", account="test-account")

    ledger.revoke(token.token_id)

    with pytest.raises(PermissionError, match="revoked"):
        ledger.validate(token.token, audience="local-client", scope="provider-upstream")


# ---- Disk persistence tests ----


def test_persisted_tokens_survive_new_ledger_instance(tmp_path) -> None:
    """Tokens minted with one ledger are visible to a new ledger that loads
    from the same persist file."""
    persist = tmp_path / "tokens.json"
    now = 1000
    ledger1 = StandInTokenLedger(now=lambda: now, persist_path=persist)
    token = ledger1.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=3600,
    )
    assert persist.exists()

    # Simulate a process restart: create a new ledger from the same file.
    ledger2 = StandInTokenLedger(now=lambda: now, persist_path=persist)
    record = ledger2.validate(
        token.token, audience="local-client", scope="provider-upstream"
    )
    assert record.account == "test-account"


def test_expired_tokens_pruned_on_load(tmp_path) -> None:
    """Expired tokens should not be loaded from disk."""
    persist = tmp_path / "tokens.json"
    now = 1000
    ledger1 = StandInTokenLedger(now=lambda: now, persist_path=persist)
    ledger1.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=60,
    )
    assert persist.exists()

    # Simulate time passing beyond expiry, then restart.
    now = 2000
    ledger2 = StandInTokenLedger(now=lambda: now, persist_path=persist)
    # Ledger should be empty — the only token expired.
    assert len(ledger2._records) == 0


def test_revoked_tokens_not_persisted(tmp_path) -> None:
    """Revoked tokens should be excluded from the persist file."""
    persist = tmp_path / "tokens.json"
    ledger1 = StandInTokenLedger(now=lambda: 1000, persist_path=persist)
    token = ledger1.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=3600,
    )
    ledger1.revoke(token.token_id)

    # Load a new ledger — the revoked token should not be present.
    ledger2 = StandInTokenLedger(now=lambda: 1000, persist_path=persist)
    assert len(ledger2._records) == 0


def test_corrupt_persist_file_starts_empty(tmp_path) -> None:
    """A corrupt persist file should not crash; ledger starts empty."""
    persist = tmp_path / "tokens.json"
    persist.write_text("{not valid json", encoding="utf-8")

    ledger = StandInTokenLedger(now=lambda: 1000, persist_path=persist)
    assert len(ledger._records) == 0


def test_no_persist_path_means_in_memory_only() -> None:
    """Without a persist_path, the ledger is purely in-memory (backward compat)."""
    ledger = StandInTokenLedger(now=lambda: 1000)
    token = ledger.mint(
        audience="local-client",
        scope="provider-upstream",
        account="test-account",
        ttl_seconds=60,
    )
    # A new ledger without persistence can't see the token.
    ledger2 = StandInTokenLedger(now=lambda: 1000)
    assert len(ledger2._records) == 0


def test_expired_error_message_includes_account_and_reissue_guidance() -> None:
    """The expired-token error message should mention the account and how to
    re-mint, so operators see an actionable message, not a generic error."""
    now = 1000
    ledger = StandInTokenLedger(now=lambda: now)
    token = ledger.mint(
        audience="local-client",
        scope="provider-upstream",
        account="my-account",
        ttl_seconds=1,
    )
    now = 1002
    with pytest.raises(PermissionError) as exc_info:
        ledger.validate(token.token, audience="local-client", scope="provider-upstream")
    msg = str(exc_info.value)
    assert "expired" in msg.lower()
    assert "my-account" in msg
    assert "/v1/standin" in msg
