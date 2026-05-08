# Atelier Architecture

Atelier is a local credential-custody and credential-header proxy boundary for LLM tooling.

```text
pass -> Atelier custody -> stand-in token -> local client -> Atelier final-hop proxy -> provider upstream
```

Diagram source: [architecture.mmd](architecture.mmd)

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

## Proxy Flow

1. A local client requests or is provisioned a stand-in token for an account and scope.
2. The client builds the upstream HTTP request without provider credentials.
3. The client sends the request to Atelier with the stand-in token.
4. Atelier validates the stand-in token.
5. Atelier maps the token to an account and reads the OAuth bundle from `pass`.
6. Atelier refreshes and re-stores the OAuth bundle when needed.
7. Atelier injects the provider credential header.
8. Atelier sends the upstream HTTP request and returns the upstream response.

## Failure Model

- Missing or malformed pass entries fail closed.
- Invalid, expired, wrong-scope, or revoked stand-in tokens fail before pass lookup.
- Refresh rejection marks the account unavailable until the operator reauthenticates.
- A failed pass write after a refresh is high severity because it can lose a rotated refresh token.
- Provider access tokens must not be returned to clients.
