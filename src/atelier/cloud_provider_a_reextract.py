# SPDX-License-Identifier: MIT
"""Operator-run re-extract CLI for the cloud-provider-a session-cookie jar.

Reads the local Firefox cookies.sqlite, extracts the example.com session cookies,
and writes them (encrypted via pass/GPG) to the atelier service user's pass
store through the atelier-pass-wrapper. Run this on cookie expiry/rotation --
detected by GET /v1/cloud-provider-a/usage returning empty meters or a 502 -- to
repopulate pass. Not automatic and not wired into the server; the operator
invokes it explicitly.

Output is non-disclosing: it prints only the cookie count and account, never
cookie names-with-values.

Usage:
    atelier-cloud-provider-a-reextract <account> [--prefix llm/cloud-provider-a/accounts] [--wrapper /usr/local/bin/atelier-pass-wrapper] [--service-user atelier]
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from atelier.cloud_provider_a_usage import (
    COOKIE_NAMES,
    read_cloud_provider_a_cookies,
    serialize_cookie_jar,
)
from atelier.pass_store import PassStore, PassStoreError

DEFAULT_WRAPPER = "/usr/local/bin/atelier-pass-wrapper"
DEFAULT_SERVICE_USER = "atelier"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="atelier-cloud-provider-a-reextract",
        description="Re-extract cloud-provider-a session cookies from Firefox and store them in the atelier service user's pass store.",
    )
    ap.add_argument("account", help="single pass path segment naming the cloud-provider-a account")
    ap.add_argument(
        "--prefix",
        default="llm/cloud-provider-a/accounts",
        help="pass path prefix (default: llm/cloud-provider-a/accounts)",
    )
    ap.add_argument(
        "--wrapper",
        default=DEFAULT_WRAPPER,
        help=f"path to the atelier-pass-wrapper script (default: {DEFAULT_WRAPPER}). Pass an empty string to use direct pass instead of the wrapper.",
    )
    ap.add_argument(
        "--service-user",
        default=DEFAULT_SERVICE_USER,
        help=f"system user that owns the pass store (default: {DEFAULT_SERVICE_USER})",
    )
    ap.add_argument(
        "--store-executable",
        default="pass",
        help="pass executable for direct mode (default: pass, ignored when --wrapper is set)",
    )
    args = ap.parse_args(argv)

    account = args.account.strip().strip("/")
    if not account or "/" in account:
        sys.stderr.write(f"error: account must be a single pass path segment, got {args.account!r}\n")
        return 2

    try:
        cookies = read_cloud_provider_a_cookies()
    except RuntimeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    if "__Secure-session" not in cookies:
        # Match menubar semantics: warn but proceed with whatever was found.
        sys.stderr.write(
            "warning: __Secure-session cookie not found in Firefox store; "
            "fetch may redirect to login\n"
        )

    jar = serialize_cookie_jar(cookies)
    path = f"{args.prefix.rstrip('/')}/{account}/cookies"

    if args.wrapper:
        # Write through the atelier-pass-wrapper via sudo so the cookie jar
        # lands in the service user's pass store, not the operator's.
        cmd = ["sudo", "-u", args.service_user, args.wrapper, "insert", "-m", "-f", path]
        result = subprocess.run(
            cmd,
            input=jar if jar.endswith("\n") else jar + "\n",
            text=True,
            check=False,
        )
        if result.returncode != 0:
            sys.stderr.write(f"error: atelier-pass-wrapper insert failed for {path!r}\n")
            return 1
    else:
        # Direct mode for development/testing — writes to the current user's pass store.
        store = PassStore(executable=args.store_executable)
        try:
            store.insert_multiline(path, jar)
        except PassStoreError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1

    # Non-disclosing: count only, plus which known names were present (names are
    # not secret; values are never printed).
    present = sorted(n for n in COOKIE_NAMES if n in cookies)
    sys.stdout.write(
        f"wrote {len(cookies)} cookies for account {account!r} to {path} "
        f"(known names present: {len(present)} of {len(COOKIE_NAMES)})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
