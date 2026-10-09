"""Operator-facing credential loading CLI for the atelier service user pass store.

Delegates to the atelier-pass-wrapper script via sudo so the operator can
write, delete, move, or copy credentials in the atelier service user's pass
store without ever being able to read them back.

Usage:
    atelier load put   llm/provider/accounts/default/cloud-key   < secret
    atelier load rm    llm/provider/accounts/default/cloud-key
    atelier load mv    llm/old/path llm/new/path
    atelier load cp    llm/source llm/dest
"""

from __future__ import annotations

import argparse
import subprocess
import sys

DEFAULT_WRAPPER = "/usr/local/bin/atelier-pass-wrapper"
DEFAULT_SERVICE_USER = "atelier"


def _validate_path(path: str) -> str:
    """Validate that a path is safe and lives under the llm/ namespace."""
    cleaned = path.strip().strip("/")
    if not cleaned:
        raise ValueError("path must not be empty")
    if not cleaned.startswith("llm/"):
        raise ValueError(f"path must start with 'llm/', got {cleaned!r}")
    segments = cleaned.split("/")
    if any(seg == ".." for seg in segments):
        raise ValueError("path must not contain '..' segments")
    return cleaned


def _run_wrapper(
    wrapper_args: list[str],
    *,
    stdin_data: str | None = None,
    wrapper: str = DEFAULT_WRAPPER,
    service_user: str = DEFAULT_SERVICE_USER,
) -> int:
    """Run the atelier-pass-wrapper via sudo as the service user."""
    cmd = ["sudo", "-u", service_user, wrapper] + wrapper_args
    result = subprocess.run(
        cmd,
        input=stdin_data,
        text=True,
        check=False,
    )
    return result.returncode


def load_main(argv: list[str] | None = None) -> int:
    """Entry point for the 'atelier load' subcommand."""
    ap = argparse.ArgumentParser(
        prog="atelier load",
        description=(
            "Load, remove, move, or copy credentials in the atelier "
            "service user's pass store. The operator can write but "
            "cannot read credentials back."
        ),
    )
    ap.add_argument(
        "--wrapper",
        default=DEFAULT_WRAPPER,
        help=f"path to the atelier-pass-wrapper script (default: {DEFAULT_WRAPPER})",
    )
    ap.add_argument(
        "--service-user",
        default=DEFAULT_SERVICE_USER,
        help=f"system user that owns the pass store (default: {DEFAULT_SERVICE_USER})",
    )
    sub = ap.add_subparsers(dest="load_command", required=True)

    put_p = sub.add_parser(
        "put", help="insert a credential from stdin into the pass store"
    )
    put_p.add_argument("path", help="pass path (must start with llm/)")

    rm_p = sub.add_parser("rm", help="remove a credential from the pass store")
    rm_p.add_argument("path", help="pass path (must start with llm/)")

    mv_p = sub.add_parser("mv", help="move or rename a credential in the pass store")
    mv_p.add_argument("source", help="source pass path (must start with llm/)")
    mv_p.add_argument("dest", help="destination pass path (must start with llm/)")

    cp_p = sub.add_parser("cp", help="copy a credential in the pass store")
    cp_p.add_argument("source", help="source pass path (must start with llm/)")
    cp_p.add_argument("dest", help="destination pass path (must start with llm/)")

    args = ap.parse_args(argv)
    wrapper = args.wrapper
    service_user = args.service_user

    if args.load_command == "put":
        try:
            path = _validate_path(args.path)
        except ValueError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
        stdin_data = sys.stdin.read()
        if not stdin_data:
            sys.stderr.write("error: no data received on stdin\n")
            return 2
        return _run_wrapper(
            ["insert", "-m", "-f", path],
            stdin_data=stdin_data,
            wrapper=wrapper,
            service_user=service_user,
        )

    elif args.load_command == "rm":
        try:
            path = _validate_path(args.path)
        except ValueError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
        return _run_wrapper(
            ["rm", "-f", path],
            wrapper=wrapper,
            service_user=service_user,
        )

    elif args.load_command == "mv":
        try:
            source = _validate_path(args.source)
            dest = _validate_path(args.dest)
        except ValueError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
        return _run_wrapper(
            ["mv", "-f", source, dest],
            wrapper=wrapper,
            service_user=service_user,
        )

    elif args.load_command == "cp":
        try:
            source = _validate_path(args.source)
            dest = _validate_path(args.dest)
        except ValueError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
        return _run_wrapper(
            ["cp", "-f", source, dest],
            wrapper=wrapper,
            service_user=service_user,
        )

    return 2  # unreachable due to required=True


if __name__ == "__main__":
    raise SystemExit(load_main())
