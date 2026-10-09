# Security

## Credential Isolation Model

Atelier runs as a dedicated system user with its own GPG key and pass store. Only the Atelier process can decrypt credentials. Client tools and the operator cannot read credentials directly — they interact only through Atelier's HTTP proxy and write-only loading commands.

## Reporting

Do not report real credentials, OAuth tokens, pass entries, auth databases, or decrypted credential material in public issues or discussions.

If you believe you found a vulnerability, report only the minimum reproducible behavior and redact all secrets. Include:

- affected version or commit
- local-only or remote exposure conditions
- expected and actual credential disclosure behavior
- whether stand-in token revocation or expiry failed

Atelier treats provider access tokens, refresh tokens, full OAuth bundles, stand-in tokens, API keys, session cookies, and password-store exports as sensitive.

## Threat Model

- **Client tools**: receive only local, scoped, short-lived, revocable stand-in tokens. They never see provider credentials.
- **Operator**: can load, delete, move, and copy credentials through `atelier load` subcommands but cannot read them back.
- **Other local users**: cannot access the Atelier service user's pass store or GPG private key.
- **Root**: can bypass the isolation by switching to the Atelier service user. The threat model covers operator and client isolation, not root isolation.
