# ADR 0002: Operator-Isolated Credential Custody

Date: 2026-10-09

## Status

Accepted

## Context

ADR 0001 adopted `pass` as the encrypted durable storage backend and
defined Atelier as the custody and proxy boundary on top. The original
design assumed a single-user model: the operator, client tools, and
Atelier all ran as the same Unix user, sharing the same GPG private key.

Under that model the stand-in token boundary protects **clients** from
seeing provider credentials over the HTTP API, but it does not protect
credentials from the **operator**. Any process running as the operator
can run `pass show` directly and decrypt every entry, because the
operator owns the GPG private key. This was demonstrated in practice:
the operator's terminal session could read the Cloud Provider A cloud key, the
OAuth provider bundles, and every other entry without authenticating to
Atelier or presenting a stand-in token.

The intended outcome is stronger: only the Atelier process should be
able to decrypt credentials. No other process — including the
operator's own shell and client tools running under the operator's
account — should be able to read them.

## Decision

Introduce OS-level user separation with a dedicated `atelier` service
user that owns the GPG private key and the pass store. The operator can
write credentials into the service user's pass store through a
write-only wrapper but cannot read them back.

### Three identities

| Identity | Unix user | Can decrypt pass entries? | How it accesses credentials |
|---|---|---|---|
| Atelier service | `atelier` | Yes — owns the GPG key | Direct `pass show` at runtime |
| Client tools | `<operator-username>` | No | HTTP proxy with stand-in tokens |
| Operator | `<operator-username>` | No | Write-only via restricted sudo wrapper |

### Mechanism

1. **Service user**: a dedicated `atelier` system user with its own home
   directory, GPG key pair (passphrase-less RSA 4096), and pass store.

2. **Write-only wrapper**: `/usr/local/bin/atelier-pass-wrapper` is a
   root-owned script that only permits `pass insert`, `pass rm`,
   `pass mv`, and `pass cp` — never `pass show`, `pass edit`, or any
   command that decrypts. A sudoers rule allows the operator to run
   this wrapper as the `atelier` user and nothing else.

3. **`atelier load` subcommands**: the operator manages credentials
   through `atelier load put`, `atelier load rm`, `atelier load mv`,
   and `atelier load cp`, which delegate to the wrapper via sudo.

4. **System service**: Atelier runs as a system-level systemd unit
   (`atelier.service`) with `User=atelier`, not as a `--user` service.
   The service user's `gpg-agent` holds the private key and auto-loads
   it (passphrase-less key) on first use.

5. **Migration**: existing credentials are piped from the operator's
   pass store into the service user's pass store
   (`pass show <path> | sudo -u atelier atelier-pass-wrapper insert -m -f <path>`).
   The plaintext flows through a pipe and is never displayed, written
   to a file, or captured by tool output.

## Consequences

- The operator cannot read credentials after loading them. Rotation
  replaces the entry; it does not require reading the old value.
- The operator can still delete, move, and copy entries (encrypted
  file operations that do not decrypt).
- Client tools running as the operator cannot bypass Atelier by running
  `pass show` directly, because the pass store and GPG key belong to a
  different user.
- Root can still read everything (`su - atelier`). The threat model is
  operator-isolation, not root-isolation.
- Atelier cannot auto-restart unattended if the GPG key has a
  passphrase. A passphrase-less key is used so the service can start
  without human intervention. The private key is protected by file
  ownership (only the `atelier` user can read it), not by a passphrase.
- The `atelier-cloud-provider-a-reextract` command must also write through the
  wrapper, since it loads cookie jars that belong in the service user's
  pass store.
- ADR 0001 remains valid for its encryption and proxy design. This ADR
  extends the trust model by adding OS-level identity separation.
