# Credential Proxy Integration Contract

Atelier defines a local OAuth custody and credential-header proxy boundary for tools that need upstream provider access. Durable OAuth bundles live encrypted in `pass`; downstream clients receive local stand-in tokens only.

## pass Paths

Use provider/account entries for OAuth bundles:

```text
llm/provider/accounts/test-account/auth-json
llm/provider/accounts/account-b/auth-json
```

The entry value is the provider auth JSON object. Do not store decrypted exports, local auth databases, password-store clones, or GPG key material in this repo.

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

Atelier must not return:

- provider `access_token`
- `refresh_token`
- full auth JSON
- identity tokens unless a later client contract proves they are required
- decrypted pass entry contents

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

## Reauthentication

If a refresh token is rejected, the operator must log in again with the provider's normal tool or web flow, import the resulting auth bundle into the matching pass entry, and remove plaintext copies.
