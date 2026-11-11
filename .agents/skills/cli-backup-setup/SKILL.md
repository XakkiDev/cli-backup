---
name: cli-backup-setup
description: Configure cli-backup for a project's database, storage and external schedule. Use for MariaDB, MySQL, SQLite, rootless containers or S3 backup integration.
---

# Configure project backups

Read the project's instructions and the cli-backup README first. Identify the actual project, database, environment, storage and approved schedule. Never print or commit credentials, edit another project without authorization, or modify an existing schedule without authorization.

For scheduled MariaDB/MySQL or SQLite, include `share.Makefile` from a checkout and configure the `CLI_BACKUP_*` variables in the project. For a Compose service, set `CLI_BACKUP_COMPOSE_PROJECT_NAME` and `CLI_BACKUP_DB_CONTAINER` to the effective project and database service; container discovery uses labels, not Compose files. For standalone Docker/Podman use `CLI_BACKUP_PROJECT_NAME` and `CLI_BACKUP_DB_CONTAINER`; set `CLI_BACKUP_CONTAINER_RUNTIME=podman` for Podman. For TCP set `CLI_BACKUP_PROJECT_NAME`, `CLI_BACKUP_DB_HOST`, `CLI_BACKUP_DB_NAME` and protected credentials. For SQLite set `CLI_BACKUP_PROJECT_NAME`, `CLI_BACKUP_DB_SERVICE_NAME=sqlite`, and an absolute `CLI_BACKUP_DB_PATH`, without a container or host. Match the project's production/test selectors and do not change the live stack.

Set `CLI_BACKUP_PERIOD_DAILY`, `CLI_BACKUP_PERIOD_WEEKLY`, and `CLI_BACKUP_PERIOD_MONTHLY` per the owner's retention policy. Scheduled defaults are daily and monthly enabled and weekly disabled. Arrange an external scheduler; the tool does not create cron jobs or bucket policies. Set storage credentials in a protected environment. `make cli-backup@config-check` validates settings; a due backup runs with `make cli-backup@backup`. A satisfied period may skip a new dump.

If requested, list or verify copies with the shared Make targets and appropriate `where`, `path`, `db`, and `file`. Backups may be unencrypted; protect all destinations. Restoration is destructive and requires explicit owner authorization. Never run a restore rehearsal without approval; a successful backup alone does not prove recovery. SQLite live restore is manual. Read the README for selection, S3 prefixes, and available source types.
