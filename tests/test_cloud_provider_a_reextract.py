"""Tests for the operator-run cloud-provider-a cookie re-extract CLI.

Synthetic cookies.sqlite only. Asserts the CLI never prints cookie values.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

from atelier import cloud_provider_a_reextract, cloud_provider_a_usage
from atelier.cloud_provider_a_reextract import main


class RecordingStore:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def show(self, path: str) -> str:
        raise KeyError(path)

    def insert_multiline(self, path: str, value: str) -> None:
        self.writes.append((path, value))


def _make_synthetic_cookies_db(path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, "
        "path TEXT, expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER)"
    )
    con.executemany(
        "INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)",
        [
            ("__Secure-session", "SYNTHETIC_SESSION_VALUE", "example.com"),
            ("aid", "SYNTHETIC_AID", ".example.com"),
            ("cf_clearance", "SYNTHETIC_CF", ".example.com"),
            ("__cf_bm", "SYNTHETIC_BM", ".example.com"),
        ],
    )
    con.commit()
    con.close()


def test_reextract_writes_jar_to_pass_direct_mode(
    monkeypatch, tmp_path, capsys
) -> None:
    """Direct mode (--wrapper '') writes through PassStore."""
    db = tmp_path / "cookies.sqlite"
    _make_synthetic_cookies_db(db)
    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", lambda: str(db))

    store = RecordingStore()
    monkeypatch.setattr(
        cloud_provider_a_reextract, "PassStore", lambda *, executable="pass": store
    )

    rc = main(["test-account", "--prefix", "llm/cloud-provider-a/accounts", "--wrapper", ""])
    assert rc == 0

    assert len(store.writes) == 1
    path, jar = store.writes[0]
    assert path == "llm/cloud-provider-a/accounts/test-account/cookies"
    cookies = json.loads(jar)
    assert set(cookies.keys()) == {
        "__Secure-session",
        "aid",
        "cf_clearance",
        "__cf_bm",
    }

    out = capsys.readouterr()
    # The CLI must never disclose cookie values.
    assert "SYNTHETIC_SESSION_VALUE" not in out.out
    assert "SYNTHETIC_SESSION_VALUE" not in out.err
    assert "SYNTHETIC_AID" not in out.out
    assert "test-account" in out.out


def test_reextract_writes_jar_through_wrapper(monkeypatch, tmp_path, capsys) -> None:
    """Wrapper mode (default) writes through sudo + atelier-pass-wrapper."""
    db = tmp_path / "cookies.sqlite"
    _make_synthetic_cookies_db(db)
    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", lambda: str(db))

    mock_result = MagicMock(returncode=0)
    with patch(
        "atelier.cloud_provider_a_reextract.subprocess.run", return_value=mock_result
    ) as mock_run:
        rc = main(["test-account", "--prefix", "llm/cloud-provider-a/accounts"])
    assert rc == 0

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[:4] == ["sudo", "-u", "atelier", "/usr/local/bin/atelier-pass-wrapper"]
    assert cmd[4:] == ["insert", "-m", "-f", "llm/cloud-provider-a/accounts/test-account/cookies"]
    stdin_data = mock_run.call_args[1]["input"]
    cookies = json.loads(stdin_data.strip())
    assert set(cookies.keys()) == {
        "__Secure-session",
        "aid",
        "cf_clearance",
        "__cf_bm",
    }

    out = capsys.readouterr()
    assert "SYNTHETIC_SESSION_VALUE" not in out.out
    assert "SYNTHETIC_AID" not in out.out


def test_reextract_wrapper_failure_returns_error(monkeypatch, tmp_path, capsys) -> None:
    """Wrapper non-zero return code propagates as an error."""
    db = tmp_path / "cookies.sqlite"
    _make_synthetic_cookies_db(db)
    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", lambda: str(db))

    mock_result = MagicMock(returncode=1)
    with patch("atelier.cloud_provider_a_reextract.subprocess.run", return_value=mock_result):
        rc = main(["test-account"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "atelier-pass-wrapper insert failed" in err


def test_reextract_rejects_bad_account(monkeypatch, tmp_path, capsys) -> None:
    db = tmp_path / "cookies.sqlite"
    _make_synthetic_cookies_db(db)
    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", lambda: str(db))
    monkeypatch.setattr(
        cloud_provider_a_reextract, "PassStore", lambda *, executable="pass": RecordingStore()
    )

    rc = main(["a/b"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "single pass path segment" in err


def test_reextract_no_firefox_profile(monkeypatch, capsys) -> None:
    def boom() -> str:
        raise RuntimeError(
            "no Firefox cookies.sqlite found under known profile roots: ..."
        )

    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", boom)
    rc = main(["test-account"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no Firefox cookies.sqlite" in err


def test_reextract_warns_on_missing_secure_session(
    monkeypatch, tmp_path, capsys
) -> None:
    db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, "
        "path TEXT, expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER)"
    )
    con.executemany(
        "INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)",
        [("aid", "SYNTHETIC_AID", ".example.com")],  # no __Secure-session
    )
    con.commit()
    con.close()
    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", lambda: str(db))
    store = RecordingStore()
    monkeypatch.setattr(
        cloud_provider_a_reextract, "PassStore", lambda *, executable="pass": store
    )

    rc = main(["test-account", "--wrapper", ""])
    assert rc == 0
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    # value still not disclosed
    assert "SYNTHETIC_AID" not in capsys.readouterr().out
