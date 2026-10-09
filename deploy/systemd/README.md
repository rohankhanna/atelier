# Atelier systemd system service

Atelier runs as a dedicated `atelier` system user so that only the Atelier
process can decrypt credentials from the service user's pass store. The
operator can load credentials but cannot read them back.

## Prerequisites

Complete the operator isolation setup first — see
[operator-isolation-setup.md](../operator-isolation-setup.md). At minimum:

- `atelier` system user created with home directory
- GPG key generated for the `atelier` user (passphrase-less, RSA 4096)
- pass store initialized for the `atelier` user
- `atelier-pass-wrapper` installed at `/usr/local/bin/atelier-pass-wrapper`
- sudoers rule installed at `/etc/sudoers.d/atelier-write-only`
- Existing credentials migrated into the service user's pass store

## Install

```bash
sudo deploy/systemd/install.sh
```

The installer:

1. Builds the atelier package as a wheel
2. Creates a Python virtual environment at `/var/lib/atelier/venv` owned by
   the `atelier` service user
3. Installs atelier into that virtual environment
4. Renders `atelier.service.template` into `/etc/systemd/system/atelier.service`
5. Reloads the systemd manager

It does **not** enable or start the service.

## Activate

```bash
sudo systemctl enable --now atelier.service
```

Verify the service:

```bash
curl -fsS http://127.0.0.1:7342/health
```

Follow logs:

```bash
sudo journalctl -u atelier.service -f
```

## Load credentials

Credentials are managed through the `atelier load` subcommands, which
delegate to the `atelier-pass-wrapper` script via sudo:

```bash
echo "sk-..." | atelier load put llm/cloud-provider-a/accounts/primary/cloud-key
atelier load rm  llm/cloud-provider-a/accounts/primary/cloud-key
atelier load mv  llm/old/path llm/new/path
atelier load cp  llm/source llm/dest
```

The operator can write, delete, move, and copy credentials but cannot read
them back. Only the Atelier service process can decrypt entries.

## Configuration

The rendered unit binds Atelier to `127.0.0.1:7342` and uses:

```text
ATELIER_REFRESH_URL=https://auth.example.com/oauth/token
ATELIER_CLIENT_ID=your-oauth-client-id
ATELIER_PASS_PATH_PREFIX=llm/oauth-provider/accounts
ATELIER_STATE_DIR=/var/lib/atelier
```

Edit the installed unit or override it with normal systemd drop-ins if
your local namespace differs.

## Remove

```bash
sudo deploy/systemd/install.sh --uninstall
```
