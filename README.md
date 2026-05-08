# Atelier

Atelier is a pass-backed local credential-custody and credential-header proxy for LLM tooling.

Durable originals, including OAuth refresh tokens and token bundles, live encrypted in `pass` / password-store. Atelier refreshes provider credentials, stores rotated refresh material back into `pass`, mints local stand-in tokens, and injects provider credentials only inside final upstream HTTP requests.

## Status

Early implementation skeleton. Atelier is not a general secrets platform, model router, prompt transformer, or agent launcher.

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
llm/provider/accounts/account-b/auth-json
```

Each entry stores the provider auth JSON object. Do not commit password-store data, exported credentials, local auth databases, refresh tokens, or decrypted values to this repo.

## Integration Model

A local client asks Atelier for a scoped stand-in token. The client builds an upstream HTTP request without provider credentials, then sends the final request through Atelier. Atelier validates the stand-in token, refreshes provider credentials if needed, injects the provider credential header internally, and returns the upstream response.

Leaking a stand-in token should be materially safer than leaking the provider credential: it is local-only, scoped, short-lived, and revocable.

## Architecture

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Integration contract: [docs/INTEGRATION_CONTRACT.md](docs/INTEGRATION_CONTRACT.md)
- Decision record: [docs/decisions/0001-adopt-pass.md](docs/decisions/0001-adopt-pass.md)

## Verification

```bash
scripts/verify-all
```
