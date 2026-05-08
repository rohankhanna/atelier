# Contributing

Atelier is early-stage software. Contributions should preserve the narrow boundary:

- credential custody and credential-header injection are in scope
- model routing, prompt routing, and client launching are out of scope
- provider access tokens and refresh tokens must never appear in commits, tests, logs, issues, or examples

Before opening a change, run:

```bash
scripts/verify-all
```

Security-sensitive changes should include tests for non-disclosure and failure behavior.
