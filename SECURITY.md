# Security

Do not report real credentials, OAuth tokens, pass entries, auth databases, or decrypted credential material in public issues or discussions.

If you believe you found a vulnerability, report only the minimum reproducible behavior and redact all secrets. Include:

- affected version or commit
- local-only or remote exposure conditions
- expected and actual credential disclosure behavior
- whether stand-in token revocation or expiry failed

Atelier treats provider access tokens, refresh tokens, full OAuth bundles, stand-in tokens, and password-store exports as sensitive.
