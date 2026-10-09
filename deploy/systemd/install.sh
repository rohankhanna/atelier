#!/usr/bin/env bash
# Install the atelier system service for the dedicated atelier service user.
#
# Usage:
#   sudo UV_PATH=/home/<operator-username>/.local/bin/uv ./install.sh
#   sudo UV_PATH=/home/<operator-username>/.local/bin/uv ./install.sh --dry-run
#   sudo ./install.sh --uninstall
#
# Prerequisites (done once by the operator, see deploy/operator-isolation-setup.md):
#   - atelier system user created with home directory
#   - GPG key generated for the atelier user
#   - pass store initialized for the atelier user
#   - atelier-pass-wrapper installed at /usr/local/bin/atelier-pass-wrapper
#   - sudoers rule installed at /etc/sudoers.d/atelier-write-only

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root_default="$(cd "${here}/../.." && pwd -P)"

REPO_ROOT="${REPO_ROOT:-${repo_root_default}}"
VENV_DIR="${VENV_DIR:-/var/lib/atelier/venv}"
SERVICE_DST="/etc/systemd/system/atelier.service"

dry_run=0
uninstall=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --uninstall) uninstall=1 ;;
        --help|-h)
            sed -n '1,/^set -e/p' "$0" | sed -n '/^# /p' | sed 's/^# //'
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
    shift
done

if [[ ${uninstall} -eq 1 ]]; then
    if [[ ${dry_run} -eq 1 ]]; then
        echo "(dry-run) would: systemctl disable --now atelier.service"
        echo "(dry-run) would: rm ${SERVICE_DST}"
        echo "(dry-run) would: systemctl daemon-reload"
    else
        systemctl disable --now atelier.service 2>/dev/null || true
        rm -f "${SERVICE_DST}"
        systemctl daemon-reload
        echo "uninstalled."
    fi
    exit 0
fi

# Verify the atelier service user exists
if ! id atelier >/dev/null 2>&1; then
    echo "error: the 'atelier' system user does not exist." >&2
    echo "       Create it first — see deploy/operator-isolation-setup.md" >&2
    exit 2
fi

# Verify the wrapper script exists
if [[ ! -x /usr/local/bin/atelier-pass-wrapper ]]; then
    echo "error: /usr/local/bin/atelier-pass-wrapper not found." >&2
    echo "       Install it first — see deploy/operator-isolation-setup.md" >&2
    exit 2
fi

# Verify the sudoers rule exists
if [[ ! -f /etc/sudoers.d/atelier-write-only ]]; then
    echo "error: /etc/sudoers.d/atelier-write-only not found." >&2
    echo "       Install it first — see deploy/operator-isolation-setup.md" >&2
    exit 2
fi

# Find uv for building the package (only needed for the build step, run as root)
UV_PATH="${UV_PATH:-}"
if [[ -z "${UV_PATH}" ]]; then
    UV_PATH="$(command -v uv 2>/dev/null || true)"
fi
if [[ -z "${UV_PATH}" || ! -x "${UV_PATH}" ]]; then
    echo "error: could not find 'uv' on PATH. Install uv or set UV_PATH." >&2
    exit 2
fi

# Find python3 for venv creation (available to the atelier service user)
PYTHON3="${PYTHON3:-$(command -v python3 2>/dev/null || true)}"
if [[ -z "${PYTHON3}" || ! -x "${PYTHON3}" ]]; then
    echo "error: could not find 'python3' on PATH." >&2
    exit 2
fi

# Build the package and install into the service user's venv
if [[ ${dry_run} -eq 0 ]]; then
    echo "building atelier package..."
    "${UV_PATH}" build --out-dir /tmp/atelier-build "${REPO_ROOT}"

    echo "creating venv at ${VENV_DIR}..."
    mkdir -p "$(dirname "${VENV_DIR}")"
    chown atelier:atelier "$(dirname "${VENV_DIR}")"
    sudo -u atelier "${PYTHON3}" -m venv "${VENV_DIR}"

    echo "installing atelier into venv..."
    WHEEL=$(ls /tmp/atelier-build/atelier-*.whl | head -1)
    sudo -u atelier "${VENV_DIR}/bin/pip" install "${WHEEL}"
    rm -rf /tmp/atelier-build
else
    echo "(dry-run) would: build wheel, create venv at ${VENV_DIR}, install wheel"
fi

echo "installing atelier systemd system unit with:"
echo "  REPO_ROOT   = ${REPO_ROOT}"
echo "  VENV_DIR    = ${VENV_DIR}"
echo "  destination = ${SERVICE_DST}"

render() {
    sed \
        -e "s|@REPO_ROOT@|${REPO_ROOT}|g" \
        "${here}/atelier.service.template"
}

if [[ ${dry_run} -eq 1 ]]; then
    render
    echo "(dry-run) no files written."
    exit 0
fi

render >"${SERVICE_DST}"
systemctl daemon-reload

echo "installed:"
echo "  ${SERVICE_DST}"
echo
echo "service NOT enabled. activate explicitly with:"
echo "  sudo systemctl enable --now atelier.service"
echo
echo "verify with:"
echo "  curl -fsS http://127.0.0.1:7342/health"
echo
echo "follow logs with:"
echo "  sudo journalctl -u atelier.service -f"
