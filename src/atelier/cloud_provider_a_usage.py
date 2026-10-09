# SPDX-License-Identifier: MIT
"""Cloud Provider A usage fetch+parse and Firefox session-cookie extraction.

Ported from menubar/argos-migration/cloud-provider-a-usage-scrape.py (MIT-licensed). Atelier
is the single cookie custodian: this module owns the only code that reads the
local Firefox cookie store, plus the parser for the server-rendered usage meters
on https://example.com/cloud-provider-a/settings.

Cloud Provider A exposes no official usage API (github cloud-provider-a/cloud-provider-a#12532, #16448):
the 5h-session and 7-day-weekly meter numbers live only on the server-rendered
settings page, gated by the browser __Secure-session cookie (+ Cloudflare). The
Cloud Provider A CLI's Ed25519 keypair cannot reach them. This stopgap retires when Cloud Provider A
ships keypair/API-key auth or an official usage endpoint.

Security: cookies are session credentials. read_cloud_provider_a_cookies() is the sole
extractor; it copies cookies.sqlite to a short-lived temp dir (read scratch, not
durable storage) and returns a {name: value} dict. Callers must never log values.
parse_usage() returns only parsed non-credential meters, never cookies.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

DOMAIN = "example.com"
SETTINGS_URL = "https://example.com/cloud-provider-a/settings"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
COOKIE_NAMES = ("__Secure-session", "aid", "cf_clearance", "__cf_bm")


def firefox_cookies_db() -> str:
    """Return the most-recently-modified Firefox cookies.sqlite path.

    Covers classic Firefox plus snap/flatpak/LibreWolf/Floorp/Waterfox profile
    roots.
    """
    roots = [
        "~/.mozilla/firefox",
        "~/snap/firefox/common/.mozilla/firefox",
        "~/.var/app/org.mozilla.firefox/.mozilla/firefox",
        "~/.librewolf",
        "~/.floorp",
        "~/.waterfox",
    ]
    candidates: list[str] = []
    for root in roots:
        candidates.extend(glob.glob(os.path.expanduser(f"{root}/*/cookies.sqlite")))
    candidates = sorted(candidates, key=os.path.getmtime, reverse=True)
    if not candidates:
        raise RuntimeError(
            "no Firefox cookies.sqlite found under known profile roots: "
            + ", ".join(roots)
        )
    return candidates[0]


def read_cloud_provider_a_cookies() -> dict[str, str]:
    """Copy cookies.sqlite to a temp file (avoids WAL lock) and read example.com cookies.

    Returns a {name: value} dict of the known cloud-provider-a session/Cloudflare cookies
    plus any __Secure-* cookies present. Values are session credentials: callers
    must never log or return them.
    """
    src = firefox_cookies_db()
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "cookies.sqlite")
        shutil.copy2(src, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name, value, host FROM moz_cookies "
                "WHERE host LIKE ? OR host LIKE ?",
                (f"%.{DOMAIN}", f"%{DOMAIN}"),
            ).fetchall()
        finally:
            con.close()
    cookies: dict[str, str] = {}
    for name, value, _host in rows:
        if name in COOKIE_NAMES or name.startswith("__Secure-"):
            cookies[name] = value
    return cookies


def serialize_cookie_jar(cookies: dict[str, str]) -> str:
    """Serialize a cookie jar to a JSON string for pass storage."""
    return json.dumps(cookies, indent=2, sort_keys=True)


def parse_cookie_jar(raw: str) -> dict[str, str]:
    """Parse a pass-stored cookie jar back into a {name: value} dict."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("cookie-jar pass entry is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("cookie-jar pass entry must be a JSON object")
    cookies: dict[str, str] = {}
    for name, value in payload.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("cookie-jar pass entry must be a {name: value} string map")
        cookies[name] = value
    return cookies


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_usage(html: str) -> dict:
    """Parse plan + session/weekly used% and reset times from the settings page.

    example.com/settings renders two server-side meter blocks, each shaped:
        <span ...>Session usage</span><span ...>3.7% used</span>
        ... <div class="... local-time" data-time="2026-06-30T19:00:00Z">Resets in 5 hours.</div>
    (and analogously for Weekly usage). The plan tier may not be in the HTML text
    (rendered as an image); plan is left null in that case.

    Returns:
        {"plan": str|None, "session": {"percent": float, "resets_at": str}|{},
         "weekly": {"percent": float, "resets_at": str}|{}, "fetched_at": ISO8601 UTC}

    On an unparseable page, session/weekly are empty dicts and plan is null; the
    caller decides what to do with an empty result. Never raises on bad HTML.
    """
    out: dict = {"plan": None, "session": {}, "weekly": {}, "fetched_at": _iso(datetime.now())}

    # 1) Primary: per-block capture of label -> "X% used" -> data-time="ISO".
    for mt in re.finditer(
        r"(Session\s+usage|Weekly\s+usage)"
        r".*?([0-9]+(?:\.[0-9]+)?)\s*%\s*used"
        r".*?data-time=\"([^\"]+)\"",
        html, re.S | re.I,
    ):
        label, pct, t = mt.group(1), mt.group(2), mt.group(3)
        key = "session" if re.search(r"session", label, re.I) else "weekly"
        if not out[key]:
            out[key] = {"percent": float(pct), "resets_at": t}

    # 2) Fallback: embedded JSON state (Next.js __NEXT_DATA__ / data-props),
    #    in case the page switches to a JS-rendered shape.
    if not out["session"] or not out["weekly"]:
        m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            try:
                _walk_for_usage(json.loads(m.group(1)), out)
            except Exception:
                pass

    # 3) Fallback: regex over HTML text for "N%" near session/weekly labels,
    #    without the data-time association.
    if not out["session"]:
        out["session"] = _regex_meter(html, ("session", "5 hour", "5-hour", "five_hour"))
    if not out["weekly"]:
        out["weekly"] = _regex_meter(html, ("weekly", "7 day", "7-day", "seven_day"))

    if not out["plan"]:
        pm = re.search(r'"plan"\s*:\s*"([^"]+)"', html)
        if pm:
            out["plan"] = pm.group(1)
    return out


def _walk_for_usage(obj, out: dict) -> None:
    if isinstance(obj, dict):
        if "session" in obj and isinstance(obj["session"], dict):
            out["session"] = _meter_from(obj["session"], out["session"])
        if "weekly" in obj and isinstance(obj["weekly"], dict):
            out["weekly"] = _meter_from(obj["weekly"], out["weekly"])
        if "plan" in obj and isinstance(out["plan"], (str, type(None))):
            if isinstance(obj["plan"], str):
                out["plan"] = obj["plan"]
        for v in obj.values():
            _walk_for_usage(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_for_usage(v, out)


def _meter_from(d: dict, existing: dict) -> dict:
    res = dict(existing)
    for k, v in d.items():
        kl = k.lower()
        if kl in ("percent", "used_percent", "utilization", "used") and isinstance(v, (int, float, str)):
            try:
                res["percent"] = float(v)
            except (ValueError, TypeError):
                pass
        if kl in ("resets_at", "reset_at", "resets", "resetat", "reset_time") and isinstance(v, str):
            res["resets_at"] = v
    return res


def _regex_meter(html: str, labels) -> dict:
    for label in labels:
        m = re.search(
            rf"{re.escape(label)}[^0-9]{{0,40}}([0-9]+(?:\.[0-9]+)?)\s*%",
            html, re.I,
        )
        if m:
            return {"percent": float(m.group(1))}
    return {}