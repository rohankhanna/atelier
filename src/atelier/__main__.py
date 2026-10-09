"""Atelier entrypoint — dispatches between the HTTP service and credential loading."""

from __future__ import annotations

import os
import sys

import uvicorn

from atelier.cloud_key_custody import CloudKeyCustody
from atelier.cookie_custody import CookieCustody
from atelier.oauth_custody import OAuthCustody
from atelier.pass_store import PassStore
from atelier.server import create_app
from atelier.standin import StandInTokenLedger

DEFAULT_REFRESH_URL = "https://auth.example.com/oauth/token"
DEFAULT_CLIENT_ID = ""
DEFAULT_PASS_PATH_PREFIX = "llm/oauth-provider/accounts"
DEFAULT_CLOUD_PROVIDER_A_PASS_PATH_PREFIX = "llm/cloud-provider-a/accounts"
DEFAULT_CLOUD_PROVIDER_B_PASS_PATH_PREFIX = "llm/cloud-provider-b/accounts"
DEFAULT_CLOUD_PROVIDER_D_PASS_PATH_PREFIX = "llm/cloud-provider-d/accounts"
DEFAULT_CLOUD_PROVIDER_C_PASS_PATH_PREFIX = "llm/cloud-provider-c/accounts"


def create_custody_from_env() -> OAuthCustody:
    refresh_url = os.getenv("ATELIER_REFRESH_URL", DEFAULT_REFRESH_URL)
    client_id = os.getenv("ATELIER_CLIENT_ID", DEFAULT_CLIENT_ID)
    path_prefix = os.getenv("ATELIER_PASS_PATH_PREFIX", DEFAULT_PASS_PATH_PREFIX)
    return OAuthCustody(
        store=PassStore(),
        refresh_url=refresh_url,
        client_id=client_id,
        path_prefix=path_prefix,
    )


def create_cookie_custody_from_env() -> CookieCustody:
    path_prefix = os.getenv("ATELIER_CLOUD_PROVIDER_A_PASS_PATH_PREFIX", DEFAULT_CLOUD_PROVIDER_A_PASS_PATH_PREFIX)
    return CookieCustody(store=PassStore(), path_prefix=path_prefix)


def create_cloud_key_custody_from_env() -> CloudKeyCustody:
    path_prefix = os.getenv("ATELIER_CLOUD_PROVIDER_A_PASS_PATH_PREFIX", DEFAULT_CLOUD_PROVIDER_A_PASS_PATH_PREFIX)
    return CloudKeyCustody(store=PassStore(), path_prefix=path_prefix)


def create_cloud_provider_b_key_custody_from_env() -> CloudKeyCustody:
    path_prefix = os.getenv("ATELIER_CLOUD_PROVIDER_B_PASS_PATH_PREFIX", DEFAULT_CLOUD_PROVIDER_B_PASS_PATH_PREFIX)
    return CloudKeyCustody(store=PassStore(), path_prefix=path_prefix)


def create_cloud_provider_d_key_custody_from_env() -> CloudKeyCustody:
    path_prefix = os.getenv("ATELIER_CLOUD_PROVIDER_D_PASS_PATH_PREFIX", DEFAULT_CLOUD_PROVIDER_D_PASS_PATH_PREFIX)
    return CloudKeyCustody(store=PassStore(), path_prefix=path_prefix)


def create_cloud_provider_c_key_custody_from_env() -> CloudKeyCustody:
    path_prefix = os.getenv("ATELIER_CLOUD_PROVIDER_C_PASS_PATH_PREFIX", DEFAULT_CLOUD_PROVIDER_C_PASS_PATH_PREFIX)
    return CloudKeyCustody(store=PassStore(), path_prefix=path_prefix)


def run_server() -> None:
    """Launch Atelier HTTP service."""
    host = os.getenv("ATELIER_HOST", "127.0.0.1")
    port = int(os.getenv("ATELIER_PORT", "7342"))
    custody = create_custody_from_env()

    # Initialize stand-in token ledger with disk persistence so tokens
    # survive atelier restarts.  The persist file lives in the service
    # user's state directory and stores only token hashes (never raw tokens).
    persist_dir = os.getenv("ATELIER_STATE_DIR", os.path.expanduser("~/.local/state/atelier"))
    standin_persist_path = os.path.join(persist_dir, "standin-tokens.json")
    ledger = StandInTokenLedger(persist_path=standin_persist_path)

    # Initialize cookie custody for session-cookie accounts
    cookie_custody = create_cookie_custody_from_env()

    # Initialize cloud key custody for provider API key injection
    cloud_key_custody = create_cloud_key_custody_from_env()

    # Initialize Cloud Provider B key custody (same CloudKeyCustody class, different
    # pass path prefix). The key lives at llm/cloud-provider-b/accounts/{account}/cloud-key.
    cloud_provider_b_key_custody = create_cloud_provider_b_key_custody_from_env()

    # Initialize Cloud Provider D key custody (same CloudKeyCustody class, different
    # pass path prefix). The key lives at llm/cloud-provider-d/accounts/{account}/cloud-key.
    cloud_provider_d_key_custody = create_cloud_provider_d_key_custody_from_env()

    # Initialize Cloud Provider C key custody (same CloudKeyCustody class, different
    # pass path prefix). The key lives at llm/cloud-provider-c/accounts/{account}/cloud-key.
    cloud_provider_c_key_custody = create_cloud_provider_c_key_custody_from_env()

    # Create FastAPI app
    app = create_app(
        custody=custody,
        ledger=ledger,
        cookie_custody=cookie_custody,
        cloud_key_custody=cloud_key_custody,
        cloud_provider_b_key_custody=cloud_provider_b_key_custody,
        cloud_provider_d_key_custody=cloud_provider_d_key_custody,
        cloud_provider_c_key_custody=cloud_provider_c_key_custody,
    )

    # Run uvicorn
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )


def main() -> None:
    """Dispatch between 'atelier serve' and 'atelier load' subcommands.

    With no subcommand, defaults to 'serve' for backward compatibility.
    """
    if len(sys.argv) >= 2 and sys.argv[1] == "load":
        from atelier.load_cli import load_main
        raise SystemExit(load_main(sys.argv[2:]))
    run_server()


if __name__ == "__main__":
    main()
