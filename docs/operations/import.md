# Import OAuth Bundles Into pass

Atelier reads durable OAuth bundles from `pass`. For an OAuth provider account
named `test-account`, insert the existing auth JSON into:

```text
llm/oauth-provider/accounts/test-account/auth-json
```

Run:

```bash
pass insert -m llm/oauth-provider/accounts/test-account/auth-json
```

Paste the full JSON content, then press `Ctrl-D`.

After import, delete plaintext copies of the auth JSON from shell
history buffers, editor swap files, temporary directories, and any
legacy local vault path. Do not commit password-store exports or
decrypted auth JSON to this repo.
