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
