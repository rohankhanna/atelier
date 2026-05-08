# ADR 0001: Adopt pass-Backed OAuth Custody And Credential Proxying

Date: 2026-05-08

## Status

Accepted

## Context

Atelier started as a proposed local credential-custody service that would mint short-lived stand-in tokens for local LLM tools. A build-vs-buy review found that existing local credential tools already satisfy durable encrypted storage better than a new custom database.

Some local clients need to send upstream provider requests, but should not own durable refresh tokens, provider access tokens, or plaintext auth bundle files. Refresh tokens and provider access tokens are credential material that should stay behind a local custody boundary.

## Decision

Use `pass` / password-store as the encrypted durable storage backend and implement Atelier as the narrow OAuth custody and credential-header proxy layer on top.

Atelier loads OAuth bundles from `pass`, refreshes access tokens when needed, writes rotated refresh tokens back to `pass`, mints local stand-in tokens, validates those stand-in tokens, and injects provider credentials only inside the final upstream HTTP hop.

## Consequences

- Refresh tokens live encrypted in `pass`.
- Local clients receive stand-in tokens but not provider access tokens or refresh tokens.
- Local clients should move from plaintext auth bundle files to Atelier account ids.
- Local clients should send final upstream requests through Atelier for credential-header injection.
- A failed pass write after refresh is high severity because it may lose a rotated refresh token.
- Atelier remains out of prompt routing, model selection, LLM payload transformation, and process launch.
