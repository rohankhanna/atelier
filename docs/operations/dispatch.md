# Dispatch Operations

Status: N/A: no long-running automation

Atelier is a credential-custody and credential-header proxy that runs as a system service. It does not run long-lived jobs, background pipelines, cron-like automation, training or embedding runs, discovery pipelines, or workloads with restart or recovery needs beyond the systemd service lifecycle.

The service itself is managed by systemd, which handles start, stop, restart, and automatic recovery. No Dispatch orchestration is required.
