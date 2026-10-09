"""Tests for cloud_provider_a_usage: cookie-jar (de)serialization, parse_usage, Firefox extraction.

All fixtures are SYNTHETIC. No real cookies, cookie values, or browser profiles
are used. Cookie values are obvious placeholder strings so a leaked value would
be visually unmistakable in test output.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from atelier import cloud_provider_a_usage
from atelier.cloud_provider_a_usage import (
    COOKIE_NAMES,
    parse_cookie_jar,
    parse_usage,
    read_cloud_provider_a_cookies,
    serialize_cookie_jar,
)

# --- cookie jar (de)serialization -------------------------------------------


def test_serialize_parse_cookie_jar_roundtrip() -> None:
    cookies = {"__Secure-session": "SYNTHETIC_SESSION_VALUE", "aid": "SYNTHETIC_AID"}
    raw = serialize_cookie_jar(cookies)
    # values must never appear in the serialized form beyond the JSON itself
    assert "SYNTHETIC_SESSION_VALUE" in raw  # roundtrip carries values; storage is pass-encrypted
    assert parse_cookie_jar(raw) == cookies


def test_parse_cookie_jar_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_cookie_jar(json.dumps(["not", "an", "object"]))


def test_parse_cookie_jar_rejects_non_string_values() -> None:
    with pytest.raises(ValueError, match="string map"):
        parse_cookie_jar(json.dumps({"aid": 123}))


def test_parse_cookie_jar_rejects_bad_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_cookie_jar("not json at all")


# --- parse_usage: three strategies + empty ---------------------------------


def test_parse_usage_per_block_regex() -> None:
    html = (
        '<span class="x">Session usage</span>'
        '<span class="y">3.7% used</span>'
        '<div class="local-time" data-time="2026-06-30T19:00:00Z">Resets in 5 hours.</div>'
        '<span class="x">Weekly usage</span>'
        '<span class="y">42.1% used</span>'
        '<div class="local-time" data-time="2026-07-03T00:00:00Z">Resets in 3 days.</div>'
    )
    out = parse_usage(html)
    assert out["session"] == {"percent": 3.7, "resets_at": "2026-06-30T19:00:00Z"}
    assert out["weekly"] == {"percent": 42.1, "resets_at": "2026-07-03T00:00:00Z"}
    assert "fetched_at" in out
    assert out["plan"] is None


def test_parse_usage_next_data_fallback() -> None:
    state = {
        "session": {"percent": 12.3, "resets_at": "2026-06-30T20:00:00Z"},
        "weekly": {"percent": 55.0, "resets_at": "2026-07-04T01:00:00Z"},
        "plan": "plus",
    }
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(state)}</script>'
    out = parse_usage(html)
    assert out["session"] == {"percent": 12.3, "resets_at": "2026-06-30T20:00:00Z"}
    assert out["weekly"] == {"percent": 55.0, "resets_at": "2026-07-04T01:00:00Z"}
    assert out["plan"] == "plus"


def test_parse_usage_label_regex_fallback() -> None:
    # No "% used" phrasing and no data-time, so primary + NEXT_DATA miss;
    # label-near-N% fallback should fire.
    html = "Session usage: 12.5% (resets soon) | Weekly usage: 45.0% (resets later)"
    out = parse_usage(html)
    assert out["session"] == {"percent": 12.5}
    assert out["weekly"] == {"percent": 45.0}


def test_parse_usage_empty_html_returns_nulls() -> None:
    out = parse_usage("<html></html>")
    assert out["plan"] is None
    assert out["session"] == {}
    assert out["weekly"] == {}
    assert "fetched_at" in out


def test_parse_usage_never_raises_on_garbage() -> None:
    out = parse_usage("<<<not html at all>>>")
    assert out["plan"] is None
    assert out["session"] == {}
    assert out["weekly"] == {}


# --- read_cloud_provider_a_cookies: synthetic cookies.sqlite --------------------------


def _make_synthetic_cookies_db(path, rows) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, "
        "path TEXT, expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER)"
    )
    con.executemany(
        "INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)", rows
    )
    con.commit()
    con.close()


def test_read_cloud_provider_a_cookies_synthetic(monkeypatch, tmp_path) -> None:
    db = tmp_path / "cookies.sqlite"
    rows = [
        ("__Secure-session", "SYNTHETIC_SESSION_VALUE", "example.com"),
        ("aid", "SYNTHETIC_AID", ".example.com"),
        ("cf_clearance", "SYNTHETIC_CF", ".example.com"),
        ("__cf_bm", "SYNTHETIC_BM", ".example.com"),
        ("unrelated", "SYNTHETIC_OTHER", "example.com"),  # different host, excluded
    ]
    _make_synthetic_cookies_db(db, rows)
    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", lambda: str(db))

    cookies = read_cloud_provider_a_cookies()
    assert set(cookies.keys()) == set(COOKIE_NAMES)
    assert cookies["__Secure-session"] == "SYNTHETIC_SESSION_VALUE"
    assert "unrelated" not in cookies


def test_read_cloud_provider_a_cookies_includes_secure_prefix(monkeypatch, tmp_path) -> None:
    db = tmp_path / "cookies.sqlite"
    rows = [
        ("__Secure-session", "SYNTHETIC_SESSION_VALUE", "example.com"),
        ("__Secure-other", "SYNTHETIC_SECURE_OTHER", "example.com"),
    ]
    _make_synthetic_cookies_db(db, rows)
    monkeypatch.setattr(cloud_provider_a_usage, "firefox_cookies_db", lambda: str(db))

    cookies = read_cloud_provider_a_cookies()
    assert "__Secure-other" in cookies  # __Secure-* included even if not in COOKIE_NAMES