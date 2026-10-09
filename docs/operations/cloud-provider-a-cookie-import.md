# Import Cloud Provider A Session Cookies Into pass

Atelier custodies example.com browser session cookies so a local usage reader can
fetch usage meters through Atelier instead of scraping the browser cookie store
in-process. Cloud Provider A exposes no official usage API; the 5h-session and 7-day
meters are readable only via the `__Secure-session` cookie against
`https://example.com/cloud-provider-a/settings`. This is a stopgap pending cloud-provider-a shipping
keypair/API-key auth or an official usage endpoint.

## One-time / on rotation: re-extract cookies

The re-extract CLI reads the local Firefox cookie store and writes the jar to
`pass` (encrypted via GPG). Run it before first use and whenever cookies expire
or rotate:

```bash
atelier-cloud-provider-a-reextract primary
```

This stores the jar at:

```text
llm/cloud-provider-a/accounts/primary/cookies
```

The CLI prints only a cookie count and the account name — never cookie values.
If `__Secure-session` is missing it warns but still writes what it found; a
fetch with an expired session cookie will redirect to login, so re-run after
re-authenticating in the browser.

## Verify the endpoint

Mint a usage-scoped stand-in token and read the endpoint (Atelier must be
running and loopback-bound):

```bash
TOKEN="$(curl -s -X POST http://127.0.0.1:7342/v1/standin \
  -H 'content-type: application/json' \
  -d '{"account":"primary","scope":"cloud-provider-a-usage","ttl_seconds":300}' | jq -r .token)"

curl -s http://127.0.0.1:7342/v1/cloud-provider-a/usage \
  -H "authorization: Bearer $TOKEN" | jq
```

Expect `{plan, session, weekly, fetched_at}`. Empty meters mean the settings
page could not be parsed (cookie likely expired) — re-run re-extract. A 502
means the jar is missing or the upstream fetch failed. The response never
includes cookies.

## Security notes

- Cookies are session credentials: encrypted at rest in `pass`, never returned
  to clients, never logged with values, never committed.
- The endpoint is loopback-only and requires a usage-scoped stand-in token; it
  does not authorize credential-proxy use.
- Do not commit `pass` exports, decrypted cookie jars, or browser cookie
  databases to this repo.