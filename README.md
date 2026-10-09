# Atelier

Atelier is a pass-backed local credential-custody and credential-header proxy for LLM tooling.

Durable originals, including OAuth refresh tokens and token bundles, live encrypted in `pass` / password-store. Atelier refreshes provider credentials, stores rotated refresh material back into `pass`, mints local stand-in tokens, and injects provider credentials only inside final upstream HTTP requests.

## Operator Isolation

Atelier runs as a dedicated system user with its own GPG key and pass store. This ensures that only the Atelier process can decrypt credentials. Client tools and the operator interact with credentials only through Atelier's HTTP proxy and write-only loading commands.

| Identity | Can decrypt credentials? | How it accesses them |
|---|---|---|
| Atelier service user | Yes — owns the GPG key | Direct `pass show` at runtime |
| Client tools | No | HTTP proxy with stand-in tokens |
| Operator | No | Write-only via `atelier load` subcommands |

See [docs/decisions/0002-operator-isolated-custody.md](docs/decisions/0002-operator-isolated-custody.md) for the decision record.

## Status

Early implementation. Atelier is not a general secrets platform, model router, prompt transformer, or agent launcher.

## Roles

- `original`: durable provider credential material stored encrypted in `pass`
- `pass`: encrypted local storage backend
- `Atelier`: OAuth refresh, secure re-storage, stand-in token, and credential-header proxy boundary
- `stand-in token`: local scoped credential minted by Atelier for a trusted client
- `client`: a trusted local tool that receives stand-in tokens but not provider tokens

## Credential Layout

Use a provider/account namespace for OAuth bundles:

```text
llm/provider/accounts/test-account/auth-json
llm/provider/accounts/account-b/auth-key
```

For provider API keys:

```text
llm/provider/accounts/test-account/cloud-key
```

Each entry stores the provider auth JSON object or a bare API key string. Do not commit password-store data, exported credentials, local auth databases, refresh tokens, or decrypted values to this repository.

## Integration Model

A local client asks Atelier for a scoped stand-in token. The client builds an upstream HTTP request without provider credentials, then sends the final request through Atelier. Atelier validates the stand-in token, refreshes provider credentials if needed, injects the provider credential header internally, and returns the upstream response.

Leaking a stand-in token should be materially safer than leaking the provider credential: it is local-only, scoped, short-lived, and revocable.

## Loading Credentials

Credentials are loaded into the Atelier service user's pass store through `atelier load` subcommands:

```bash
echo "sk-..." | atelier load put   llm/provider/accounts/default/cloud-key
atelier load rm    llm/provider/accounts/default/cloud-key
atelier load mv    llm/old/path llm/new/path
atelier load cp    llm/source llm/dest
```

The operator can write, delete, move, and copy credentials but cannot read them back. Only the Atelier service process can decrypt entries.

## Architecture

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Integration contract: [docs/INTEGRATION_CONTRACT.md](docs/INTEGRATION_CONTRACT.md)
- Decision records: [0001](docs/decisions/0001-adopt-pass.md), [0002](docs/decisions/0002-operator-isolated-custody.md)

## Deployment

Install as a system service running as the dedicated `atelier` user:

EXAMPLE_ONLY sudo deploy/systemd/install.sh
EXAMPLE_ONLY sudo systemctl enable --now atelier.service

Verify:

```bash
curl -fsS http://127.0.0.1:7342/health
```

See [deploy/systemd/README.md](deploy/systemd/README.md) for details.

## Verification

```bash
scripts/verify-all
```
