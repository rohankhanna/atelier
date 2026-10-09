# Atelier Architecture

Atelier is a local credential-custody and credential-header proxy boundary for LLM tooling.

```text
pass -> Atelier custody -> stand-in token -> local client -> Atelier final-hop proxy -> provider upstream
```

Diagram source: [architecture.dot](architecture.dot) — generated: [architecture.svg](architecture.svg)

## Responsibilities

`pass` owns:

- encrypted durable storage
- GPG-backed access control
- durable OAuth token bundles

Atelier owns:

- loading OAuth token bundles from `pass`
- checking access-token expiry
- refreshing OAuth bundles when access tokens are stale
- storing rotated refresh tokens back into `pass`
- minting local stand-in tokens
- validating stand-in token audience, scope, expiry, and revocation
- injecting provider credentials only inside the final upstream HTTP hop
- redaction and non-disclosure rules for credential-bearing errors
- custody of a copy of provider browser session cookies (owned by the browser) for read-only usage access when a provider exposes no OAuth/API-key usage endpoint (session-scoped, no refresh; operator-run re-extraction on rotation); Atelier does not custody the provider's own CLI sign-in credentials (owned by that CLI)
- custody and final-hop injection of revocable, non-expiring provider API keys (non-OAuth), distinct from OAuth bundles and session cookies; the key is never returned to clients and rotation is the operator replacing the pass entry (no refresh flow)

Local clients own:

- deciding which upstream request to make
- building upstream HTTP payloads without provider credentials
- sending credential-bearing requests through Atelier
- avoiding provider access-token or refresh-token storage

Atelier does not own:

- model selection
- prompt routing
- LLM payload transformation
- provider account login
- client process launch
- general-purpose secret management


## Operator Isolation

Atelier runs as a dedicated `atelier` system user with its own GPG key and pass
store. This ensures that only the Atelier process can decrypt credentials.

| Identity | Unix user | Can decrypt pass entries? | How it accesses credentials |
|---|---|---|---|
| Atelier service | `atelier` | Yes — owns the GPG key | Direct `pass show` at runtime |
| Client tools | operator | No | HTTP proxy with stand-in tokens |
| Operator | operator | No | Write-only via `atelier load` subcommands |

The operator manages credentials through `atelier load` subcommands that
delegate to a root-owned wrapper script (`/usr/local/bin/atelier-pass-wrapper`)
via a restricted sudoers rule. The wrapper permits only `insert`, `rm`, `mv`,
and `cp` — never `show` or `edit`. The operator can load, delete, move, and
copy credentials but cannot read them back.

See [ADR 0002](decisions/0002-operator-isolated-custody.md) for the full
decision and consequences.

## Stand-In Token Model

Atelier mints local stand-in tokens with:

- `token_id`
- `audience`
- `scope`
- `account`
- `issued_at`
- `expires_at`
- `revocation_state`

The token secret is shown once to the trusted local client. Atelier stores only a hash. The default TTL should stay short.

## Credential Namespace

Use a provider/account namespace for OAuth bundles:

```text
llm/provider/accounts/test-account/auth-json
llm/provider/accounts/account-b/auth-json
```

Each entry stores the provider auth JSON object. The refresh token remains encrypted at rest in `pass` and is never returned to clients.

### Cookie Namespace (session-scoped, non-OAuth)

For providers that expose usage meters only through a browser session cookie (no OAuth/API-key path), Atelier custodies the session-cookie jar alongside OAuth bundles under a sibling path:

```text
llm/cloud-provider-a/accounts/test-account/cookies
```

The entry value is a JSON object `{name: value}` of session cookies. Atelier holds a copy of cookies owned by the browser; the provider's own CLI sign-in credentials (owned by that CLI) are out of scope and never custodied. Cookies are session credentials: encrypted at rest in `pass`, never returned to clients, and rotated by an operator-run re-extract CLI rather than a refresh flow. There is no refresh — when the cookie expires or rotates, the operator re-runs extraction and Atelier reloads the jar on the next request.

### API-Key Namespace (revocable, non-OAuth)

For a provider that issues a revocable, non-expiring API key instead of an OAuth bundle, Atelier custodies the key as a bare single-line string alongside OAuth bundles under a sibling path:

```text
llm/cloud-provider-a/accounts/test-account/cloud-key
```

The entry value is a bare string (not JSON). Atelier is the sole durable custodian; the key is encrypted at rest in `pass`, never returned to clients, and injected only inside the final upstream hop as `Authorization: Bearer <key>`. There is no refresh flow — when the key rotates, the operator replaces the pass entry and Atelier reloads the key on the next request.

The same bare-string custody pattern applies to other providers that issue revocable, non-expiring API keys, each under its own pass path prefix (for example `llm/cloud-provider-b/accounts/<account>/cloud-key`, `llm/cloud-provider-d/accounts/<account>/cloud-key`, and `llm/cloud-provider-c/accounts/<account>/cloud-key`) and selected by a dedicated stand-in token scope (`cloud-provider-b`, `cloud-provider-d`, or `cloud-provider-c`).

## Proxy Flow

1. A local client requests or is provisioned a stand-in token for an account and scope.
2. The client builds the upstream HTTP request without provider credentials.
3. The client sends the request to Atelier with the stand-in token.
4. Atelier validates the stand-in token.
5. Atelier maps the token to an account and reads the OAuth bundle from `pass`.
6. Atelier refreshes and re-stores the OAuth bundle when needed.
7. Atelier injects the provider credential header.
8. Atelier sends the upstream HTTP request and returns the upstream response.
9. The stand-in token's scope selects which custody the proxy uses: an OAuth bundle (`provider-upstream`) or a provider API key (`cloud-provider-a-key`, `cloud-provider-b`, `cloud-provider-d`, or `cloud-provider-c`).

## Usage Endpoint Flow (loopback, read-only)

For a provider whose usage meters are readable only via a stored session cookie:

1. A local client mints a stand-in token scoped for usage reads (not the broader upstream-proxy scope).
2. The client calls the loopback-only usage endpoint with the stand-in token.
3. Atelier validates the stand-in token and rejects non-loopback callers.
4. Atelier loads the session-cookie jar for the token's account from `pass`.
5. Atelier fetches the provider's settings page with the cookies on the final hop only.
6. Atelier parses the usage meters and returns only non-credential numbers — never the cookies.

Cookies never leave Atelier; only parsed `{plan, session, weekly, fetched_at}` is returned. This is a stopgap pending the provider shipping keypair/API-key auth or an official usage endpoint.

## Failure Model

- Missing or malformed pass entries fail closed.
- Invalid, expired, wrong-scope, or revoked stand-in tokens fail before pass lookup.
- Refresh rejection marks the account unavailable until the operator reauthenticates.
- A failed pass write after a refresh is high severity because it can lose a rotated refresh token.
- Provider access tokens must not be returned to clients.
- A missing or unreadable cookie jar fails closed (the endpoint returns 502); the operator re-runs extraction to recover.
- Usage endpoint responses must never include cookies, `Set-Cookie`, or upstream HTML fragments.
