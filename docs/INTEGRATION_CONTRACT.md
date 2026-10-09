# Credential Proxy Integration Contract

Atelier defines a local OAuth custody and credential-header proxy boundary for tools that need upstream provider access. Durable OAuth bundles live encrypted in `pass`; downstream clients receive local stand-in tokens only.

## pass Paths

Use provider/account entries for OAuth bundles:

```text
llm/provider/accounts/test-account/auth-json
llm/provider/accounts/account-b/auth-json
```

The entry value is the provider auth JSON object. Do not store decrypted exports, local auth databases, password-store clones, or GPG key material in this repo.

### Session-cookie jars (non-OAuth)

For providers that expose usage meters only through a browser session cookie, store the cookie jar under a sibling path:

```text
llm/cloud-provider-a/accounts/test-account/cookies
```

The entry value is a JSON object `{name: value}` of session cookies. Atelier holds a copy of cookies owned by the browser; the provider's own CLI sign-in credentials (owned by that CLI) are out of scope and never custodied. Cookies are session credentials: never return them to clients, never log them, and never commit them. There is no refresh flow; rotation is operator-run re-extraction (`atelier-cloud-provider-a-reextract <account>`), which reads the local browser cookie store and writes the jar into `pass`.

### Provider API keys (non-OAuth)

For a provider that issues a revocable, non-expiring API key (not an OAuth bundle), store the key as a BARE STRING (single line, not JSON) at:

```text
llm/cloud-provider-a/accounts/test-account/cloud-key
```

Atelier is the sole durable custodian of this key. The key is never returned to clients and is injected only inside the final upstream hop as `Authorization: Bearer <key>`. There is no refresh flow; rotation is the operator replacing the pass entry, and Atelier reloads the key on the next request.

The same bare-string pattern applies to other providers that issue revocable, non-expiring API keys, each under its own pass path prefix:

```text
llm/cloud-provider-b/accounts/test-account/cloud-key
llm/cloud-provider-d/accounts/test-account/cloud-key
llm/cloud-provider-c/accounts/test-account/cloud-key
```

Each prefix is served by its own `CloudKeyCustody` instance and selected by the stand-in token scope (`cloud-provider-b`, `cloud-provider-d`, or `cloud-provider-c`).

## Stand-In Token Shape

Atelier returns local stand-in tokens to trusted local clients:

```json
{
  "token": "ati_redacted",
  "token_id": "redacted",
  "audience": "local-client",
  "scope": "provider-upstream",
  "account": "test-account",
  "expires_at": 1770000000
}
```

The stand-in token is secret, but it is local-only, scoped, short-lived, and revocable. It is not a provider-valid access token.

Scopes are least-privilege and distinct:

- `provider-upstream` — authorizes the credential proxy to inject provider credentials into an upstream request.
- `cloud-provider-a-key` — authorizes the credential proxy to inject a provider API key into an upstream request; does not authorize OAuth-upstream proxying or usage-read.
- `cloud-provider-b` — authorizes the credential proxy to inject an Cloud Provider B API key into an upstream request; does not authorize OAuth-upstream proxying or usage-read.
- `cloud-provider-d` — authorizes the credential proxy to inject a Cloud Provider D API key into an upstream request; does not authorize OAuth-upstream proxying or usage-read.
- `cloud-provider-c` — authorizes the credential proxy to inject an Cloud Provider C API key into an upstream request; does not authorize OAuth-upstream proxying or usage-read.
- `cloud-provider-a-usage` — authorizes only the loopback usage-read endpoint; does not authorize credential-proxy use.

A client that only reads usage meters must not hold `provider-upstream`, and a proxy client must not implicitly gain usage-read.

Atelier must not return:

- provider `access_token`
- provider API keys (revocable, non-expiring)
- `refresh_token`
- full auth JSON
- identity tokens unless a later client contract proves they are required
- decrypted pass entry contents

## Stand-In Token Info Endpoint

`GET /v1/standin/info` returns non-secret metadata for all stand-in tokens.

- Auth: none required (loopback-only, returns no credentials).
- Response (200): a JSON array of token metadata objects:

```json
[
  {
    "token_id": "redacted",
    "audience": "local-client",
    "scope": "cloud-provider-b",
    "account": "default",
    "issued_at": 1770000000,
    "expires_at": 1772592000,
    "revoked": false
  }
]
```

The response never includes `token_hash`, raw token secrets, or any provider
credential. Expired and revoked tokens are included so callers can display full
lifecycle state. This endpoint is intended for read-only operational surfaces
(such as a menubar status display) that need to show token expiry without
holding token secrets.

## Custody Status Endpoint

`GET /v1/custody/status` returns a unified, non-secret view of all credential
custody state, combining stand-in token metadata with the underlying provider
credential status for each active (non-revoked) stand-in token.

- Auth: none required (loopback-only, returns no credentials).
- Response (200):

```json
{
  "accounts": [
    {
      "standin": {
        "token_id": "redacted",
        "scope": "provider-upstream",
        "account": "test-account",
        "expires_at": 1772592000,
        "issued_at": 1770000000
      },
      "credential": {
        "account_id": "org-...",
        "access_token_expires_at": 1770000300,
        "needs_refresh": false,
        "last_refresh": "2026-10-06T05:00:00Z",
        "available": true
      }
    },
    {
      "standin": {
        "token_id": "redacted",
        "scope": "cloud-provider-b",
        "account": "default",
        "expires_at": 1772592000,
        "issued_at": 1770000000
      },
      "credential": {
        "key_present": true,
        "expiry": "non-expiring",
        "available": true
      }
    }
  ]
}
```

The `credential` object shape depends on the stand-in token's scope:

- `provider-upstream` (OAuth): returns `account_id`, `access_token_expires_at`
  (JWT expiry of the current access token), `needs_refresh` (whether the token
  is within the refresh safety window), `last_refresh` timestamp, and
  `available`. Does not trigger a refresh. Never returns `access_token`,
  `refresh_token`, or `id_token`.
- `cloud-provider-b`, `cloud-provider-c`, `cloud-provider-d`, `cloud-provider-a-key` (cloud keys): returns
  `key_present`, `expiry` (always `"non-expiring"`), and `available`. Never
  returns the key value.
- `cloud-provider-a-usage` (session cookies): returns `available` and
  `expiry` (`"session-scoped"`). Never returns cookie values.

Revoked stand-in tokens are excluded. When multiple stand-in tokens share the
same (scope, account), the one with the furthest expiry is shown (the active
one). The endpoint is intended for read-only operational surfaces that need a
single-call view of both stand-in token expiry and underlying credential
health.

## Client Contract

Expected client behavior:

- request or receive a stand-in token for a specific audience and scope
- build upstream HTTP requests without provider credentials
- call Atelier's credential proxy endpoint for the final upstream hop
- retry through Atelier when upstream auth is rejected and the stand-in token remains valid
- never persist provider access tokens or refresh tokens
- never log stand-in tokens, provider tokens, full auth bundles, or Authorization headers

## Atelier Contract

Expected Atelier behavior:

- mint stand-in tokens with audience, scope, account, expiry, and revocation state
- store only stand-in token hashes
- map account ids to configured pass paths
- reject account ids containing path separators
- parse OAuth bundles from pass
- refresh near-expiry access tokens with the configured OAuth endpoint
- store rotated refresh tokens back into pass before proxying with refreshed access material
- inject provider credentials only into the final upstream request
- fail closed if pass storage fails after refresh

## Lifecycle Contract

The local process manager owns lifecycle:

- start Atelier before clients that require credential proxying
- stop dependent clients before Atelier
- keep pass/GPG unlock behavior explicit to the operator

## Usage Endpoint Contract (loopback, read-only)

`GET /v1/cloud-provider-a/usage` returns parsed, non-credential usage meters for the account bound to the stand-in token.

- Auth: `Authorization: Bearer <stand-in token>` with scope `cloud-provider-a-usage`.
- Loopback only: the server binds `127.0.0.1` and rejects non-loopback callers with 403.
- The endpoint fetches the provider settings page with the stored session cookies on the final hop only and parses the meters server-side.
- Response (200):

```json
{
  "plan": "plus",
  "session": {"percent": 3.7, "resets_at": "2026-06-30T19:00:00Z"},
  "weekly": {"percent": 42.1, "resets_at": "2026-07-03T00:00:00Z"},
  "fetched_at": "2026-08-05T12:00:00Z"
}
```

`plan`, `session`, and `weekly` may be `null`/empty when the page cannot be parsed. The response never includes cookies, `Set-Cookie`, or upstream HTML.

Failure codes: 401 (missing/invalid/wrong-scope stand-in), 403 (non-loopback), 502 (cookie jar missing/unreadable or upstream fetch failed), 503 (usage endpoint not configured). All error bodies are non-disclosing.


## Operator Credential Loading

Credentials are loaded into the atelier service user's pass store through the
`atelier load` subcommands, which delegate to the atelier-pass-wrapper script
via a restricted sudoers rule:

```bash
echo "sk-..." | atelier load put   llm/provider/accounts/default/cloud-key
atelier load rm    llm/provider/accounts/default/cloud-key
atelier load mv    llm/old/path llm/new/path
atelier load cp    llm/source llm/dest
```

The operator can write, delete, move, and copy credentials but cannot read
them back. Only the Atelier service process (running as the `atelier` system
user) can decrypt entries. See [ADR 0002](decisions/0002-operator-isolated-custody.md)
for the full operator-isolation model.

## Service Deployment

Atelier runs as a system-level systemd service (`atelier.service`) under the
dedicated `atelier` system user, not as a `--user` service. Install with:

```bash
sudo deploy/systemd/install.sh
sudo systemctl enable --now atelier.service
```

The service binds `127.0.0.1:7342` and uses `/var/lib/atelier` for state
persistence. See [deploy/systemd/README.md](../deploy/systemd/README.md) for
details.

## Reauthentication

If a refresh token is rejected, the operator must log in again with the provider's normal tool or web flow, import the resulting auth bundle into the matching pass entry, and remove plaintext copies.
