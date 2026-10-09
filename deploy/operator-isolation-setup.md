# Operator Isolation Setup — Task List

This file tracks every step to move credential custody from the `<operator-username>`
user's pass store into a dedicated `atelier` service user's pass store, so that
only the Atelier process can decrypt credentials.

Legend: [ ] not done  [x] done  [!] blocked/snag

---

## Phase 1: Create the service user and its pass store

- [x] 1.1  Create the `atelier` system user
           Command: `sudo useradd --system --create-home --shell /bin/bash atelier`
           Command: `sudo passwd -l atelier`

- [x] 1.2  Generate a passphrase-less RSA 4096 GPG key for the service user
           (Ed25519 was rejected — it is signing-only, pass needs encryption)
           Done via `sudo -u atelier gpg --batch --gen-key` with RSA

- [x] 1.3  Initialize the service user's pass store
           Command: `sudo -u atelier pass init <fingerprint>`

- [x] 1.4  Create the restricted pass wrapper script
           Location: `/usr/local/bin/atelier-pass-wrapper`
           Owner: `root:root`, permissions: `755`
           Only allows: insert, rm, mv, cp (never show, edit, grep, find)
           Done

- [x] 1.5  Install the sudoers rule
           The first attempt used wildcards in arguments, which modern sudo
           rejects. The old broken file is still on disk and must be OVERWRITTEN.
           NEXT COMMAND TO RUN:
             echo '<operator-username> ALL=(atelier) NOPASSWD: /usr/local/bin/atelier-pass-wrapper' | sudo tee /etc/sudoers.d/atelier-write-only
             sudo visudo -c
           Expected: visudo reports "parsed OK"

- [x] 1.6  Test the wrapper — write a dummy entry
           Command:
             echo "test-value" | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/test/dummy
           Expected: succeeds silently

- [x] 1.7  Test the wrapper — verify show is rejected
           Command:
             sudo -u atelier /usr/local/bin/atelier-pass-wrapper show llm/test/dummy
           Expected: "atelier-pass-wrapper: only insert, rm, mv, cp are permitted"

- [x] 1.8  Test the wrapper — clean up the dummy entry
           Command:
             sudo -u atelier /usr/local/bin/atelier-pass-wrapper rm -f llm/test/dummy
           Expected: entry removed

---

## Phase 2: Migrate existing credentials into the new store

Run these as yourself (the operator). The plaintext flows through a pipe and
is never displayed or captured.

- [x] 2.1  Check whether a cloud-provider-d entry exists
           Command:
             pass show llm/cloud-provider-d/accounts/default 2>&1
           If it succeeds, add it to the migration list below.
           If it errors with "not in the password store", skip it.

- [x] 2.2  Migrate: cloud-provider-c cloud-key
           Command:
             pass show llm/cloud-provider-c/accounts/default/cloud-key | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/cloud-provider-c/accounts/default/cloud-key

- [x] 2.3  Migrate: cloud-provider-a cloud-key
           Command:
             pass show llm/cloud-provider-a/accounts/primary/cloud-key | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/cloud-provider-a/accounts/primary/cloud-key

- [x] 2.4  Migrate: cloud-provider-a cookies
           Command:
             pass show llm/cloud-provider-a/accounts/primary/cookies | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/cloud-provider-a/accounts/primary/cookies

- [x] 2.5  Migrate: oauth-provider test-account-1 auth-json
           Command:
             pass show llm/oauth-provider/accounts/test-account-1/auth-json | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/oauth-provider/accounts/test-account-1/auth-json

- [x] 2.6  Migrate: oauth-provider test-account-2 auth-json
           Command:
             pass show llm/oauth-provider/accounts/test-account-2/auth-json | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/oauth-provider/accounts/test-account-2/auth-json

- [x] 2.7  Migrate: cloud-provider-b cloud-key
           Command:
             pass show llm/cloud-provider-b/accounts/default/cloud-key | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/cloud-provider-b/accounts/default/cloud-key

- [x] 2.8  (Conditional) Migrate: cloud-provider-d entry
           Only if step 2.1 showed an entry exists.
           Command:
             pass show llm/cloud-provider-d/accounts/default | sudo -u atelier /usr/local/bin/atelier-pass-wrapper insert -m -f llm/cloud-provider-d/accounts/default

---

## Phase 3: Verify the migration

- [x] 3.1  List all entries in the new store (paths only, no contents)
           Command:
             sudo -u atelier find /home/atelier/.password-store -name "*.gpg" -type f | sed "s|/home/atelier/.password-store/||;s|.gpg$//"
           Expected: all migrated paths appear

- [x] 3.2  Confirm you cannot read entries back
           Command:
             sudo -u atelier /usr/local/bin/atelier-pass-wrapper show llm/cloud-provider-a/accounts/primary/cloud-key
           Expected: "atelier-pass-wrapper: only insert, rm, mv, cp are permitted"

- [x] 3.3  Confirm the old sudoers wildcard file is gone
           Command:
             cat /etc/sudoers.d/atelier-write-only
           Expected: single line with /usr/local/bin/atelier-pass-wrapper, no wildcards

---

## Phase 4: Codebase changes (agent implements, not operator)

These are done by the agent in the Atelier repository after Phases 1-3 pass.

- [x] 4.1  Add `atelier load` subcommands (put, rm, mv, cp) that delegate to
           `sudo -u atelier /usr/local/bin/atelier-pass-wrapper`
- [x] 4.2  Convert the systemd unit from `--user` to a system service with
           `User=atelier`, `Group=atelier`, `RuntimeDirectory=atelier`
- [x] 4.3  Update the install script for system-level installation
- [x] 4.4  Update pass_store.py if needed (GNUPGHOME, agent path)
- [x] 4.5  Write ADR 0002: operator-isolated credential custody
- [x] 4.6  Update architecture doc and integration contract
- [x] 4.7  Add tests for the load subcommands
- [x] 4.8  Run full verification: scripts/verify-all

---

## Phase 5: Start Atelier as the service user

- [x] 5.1  Install the new system-level systemd unit
           Command (to be provided after Phase 4):
             sudo systemctl daemon-reload
             sudo systemctl enable --now atelier.service

- [x] 5.2  Verify Atelier is running and healthy
           Command:
             curl -fsS http://127.0.0.1:7342/health
           Expected: healthy response

- [x] 5.3  Functional test — make a real upstream request through the proxy
           using a stand-in token. If the upstream call succeeds, the
           credentials migrated correctly.

- [x] 5.4  Stop and disable the old `--user` service if it is still running
           Command:
             systemctl --user disable --now atelier.service 2>/dev/null || true

---

## Phase 6: Cleanup (only after Phase 5 passes)

- [x] 6.1  Remove old entries from your personal pass store
           Commands:
             pass rm -f llm/cloud-provider-c/accounts/default/cloud-key
             pass rm -f llm/cloud-provider-a/accounts/primary/cloud-key
             pass rm -f llm/cloud-provider-a/accounts/primary/cookies
             pass rm -f llm/oauth-provider/accounts/test-account-1/auth-json
             pass rm -f llm/oauth-provider/accounts/test-account-2/auth-json
             pass rm -f llm/cloud-provider-b/accounts/default/cloud-key

- [x] 6.2  Verify old entries are gone
           Command:
             pass ls
           Expected: no llm/ entries

- [x] 6.3  Final security check — confirm you cannot read any credential
           Command:
             pass show llm/cloud-provider-a/accounts/primary/cloud-key 2>&1
           Expected: "not in the password store" (old store is empty)
           Command:
             sudo -u atelier /usr/local/bin/atelier-pass-wrapper show llm/cloud-provider-a/accounts/primary/cloud-key
           Expected: rejected by wrapper
