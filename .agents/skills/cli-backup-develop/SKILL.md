---
name: cli-backup-develop
description: Develop, review or test the cli-backup Python CLI and its storage and scheduling behavior.
---

# Develop cli-backup

Read the README and project instructions, then inspect the relevant code and tests. Use a project-local `.venv`; run `make test` and `make lint`. CI runs unit tests, lint, packaging and installed-command smoke tests on supported Python versions. Integration tests are opt-in and create disposable containers; some perform database recovery, so do not run them without the owner's authorization.

Maintain the separation between the generic CLI and `scheduled`: configuration names, supported source types, and artifact layouts differ. Scheduled database recovery inspection must work when the original database container is unavailable. Preserve local/S3 destination isolation, retries, per-period idempotency, prefix scoping (including `.state`), manifest and full-object checksums, and nonzero failure reporting. Never publish a manifest for an incomplete dump. Review destructive restore safeguards and temporary-file permissions when touching recovery or storage.

When changing public options, format, supported runtimes or behavior, update README, relevant skills and meaningful regression tests. Never use production data or real credentials in tests. Avoid real restores, remote bucket mutations and cron changes without explicit authorization.
