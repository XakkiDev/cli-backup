# cli-backup

A Python command-line tool for full database backups and directory archives, stored locally, in S3-compatible object storage, or both. Use it directly or include the shared Makefile in multiple projects on the same host.

[![CI](https://github.com/XakkiDev/cli-backup/actions/workflows/ci.yml/badge.svg)](https://github.com/XakkiDev/cli-backup/actions/workflows/ci.yml) · [MIT license](LICENSE)

## Capabilities

| Interface | Sources | Features |
| --- | --- | --- |
| `scheduled` and `share.Makefile` | MariaDB, MySQL, PostgreSQL, SQLite | Date-based daily, weekly and monthly snapshots; Docker or Podman containers, TCP, or SQLite online backup |
| Generic commands | MariaDB/MySQL, Redis, directories | Full dumps, Redis RDB snapshots, gzip tar archives and exclusion patterns |

The CLI runs on the host, supports rootless Docker and Podman for scheduled database backups, and does not require a separate backup service in a project's Compose file. Scheduled Compose targets are discovered from container labels. MongoDB and incremental backups are not implemented.

**Protect backup data and credentials.** Backups are not encrypted by this tool. Use private local paths, protected environment files and encrypted transport. Backups are not a substitute for an authorized recovery exercise. This CLI does not create buckets, change lifecycle policies, or schedule jobs.

## Installation

Python 3.11+ on a POSIX host is required. Client binaries are required for direct database or Redis connections. The selected Docker/Podman runtime is required for container sources.

```sh
git clone https://github.com/XakkiDev/cli-backup.git
cd cli-backup
make venv install
.venv/bin/cli-backup --help
.venv/bin/cli-backup scheduled --help
```

For a user-wide executable, `pipx install .` installs from the checkout. `share.Makefile` is supplied in the repository checkout rather than the Python wheel: keep or download a trusted checkout to use the shared Make targets. On systems without `ensurepip`, install the matching Python venv package first.

The Composer package `xakki/cli-backup` distributes the source tree and agent skills for Composer-based projects. Composer does not install Python dependencies or provide a ready-to-run CLI: after `composer require xakki/cli-backup`, install the Python project from `vendor/xakki/cli-backup` with `pip` or `pipx` and include `vendor/xakki/cli-backup/share.Makefile` when needed. The `extra.skills.source` metadata identifies the agent skills for compatible Composer skill managers.


## Scheduled database backups

Include `share.Makefile` from your checkout. For a Compose service:

```make
CLI_BACKUP_COMPOSE_PROJECT_NAME := example
CLI_BACKUP_DB_CONTAINER := mariadb
include /path/to/cli-backup/share.Makefile
export CLI_BACKUP_COMPOSE_PROJECT_NAME CLI_BACKUP_DB_CONTAINER
```

Set `CLI_BACKUP_PROJECT_NAME` if the storage namespace should differ from the Compose project. For a standalone container, set `CLI_BACKUP_PROJECT_NAME` and `CLI_BACKUP_DB_CONTAINER` to its name or ID. Set `CLI_BACKUP_CONTAINER_RUNTIME=podman` to use local rootless Podman; Docker is the default and uses the selected Docker context. For TCP use `CLI_BACKUP_PROJECT_NAME`, `CLI_BACKUP_DB_HOST`, optionally `CLI_BACKUP_DB_PORT`, `CLI_BACKUP_DB_NAME`, `CLI_BACKUP_DB_USER` and `CLI_BACKUP_DB_PASSWORD`.

Container mode defaults to root, using `MARIADB_ROOT_PASSWORD` and `MARIADB_DATABASE` from the container; MySQL equivalents are also supported. Set a user and password together to override root. `CLI_BACKUP_DB_NAME` accepts comma-separated database names; each database produces a separate file. Credentials must be supplied in the environment or a protected project file, never as command-line arguments.

For PostgreSQL, set `CLI_BACKUP_DB_SERVICE_NAME=postgresql`. Container mode defaults to `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` from the database container. The container must accept TCP connections on `127.0.0.1:5432` (or the configured port). Host mode requires `pg_dump` and explicit connection settings. Dumps include one database and use PostgreSQL's consistent snapshot; cluster-wide roles and tablespaces are not included. Restore and restore rehearsal are not implemented for PostgreSQL; verify the checksum and perform recovery manually only with authorization.

For SQLite, set `CLI_BACKUP_PROJECT_NAME`, `CLI_BACKUP_DB_SERVICE_NAME=sqlite`, and an absolute `CLI_BACKUP_DB_PATH`. Do not set container, host or Compose variables. The SQLite online backup API includes committed WAL transactions without stopping the writer. Automatic SQLite restoration is not implemented.

```sh
make cli-backup@config-check
make cli-backup@backup
make cli-backup@list path=daily where=local
make cli-backup@verify path=daily where=local file=2026-01-01.example.sql.gz
```

Additional targets `cli-backup@restore-test` and `cli-backup@restore` exist for MariaDB/MySQL. A live restore is destructive, requires an explicit file and interactive confirmation, and must target the correct stopped-writer environment. A restore rehearsal creates a disposable isolated container and should run only with appropriate authorization. For an unavailable source container, set `CLI_BACKUP_TEST_MYSQL_IMAGE` to an appropriate disposable test image before a rehearsal. Use `db=<name>` to filter when needed. Source-independent `list` and `verify` work with a stopped database container.

To restore a backup from another project into this project's selected MariaDB/MySQL database, explicitly specify the source project and database. Stop writers and run interactively only with authorization; the confirmation names both source and destination. The destination database must already exist. This example reads from S3 without a persistent intermediary dump file:

```sh
make cli-backup@restore where=s3 path=daily file=2026-01-01.production.sql.gz source_project=production source_db=production db=development
```

### Storage and schedule

Set `CLI_BACKUP_LOCAL_ROOT` for local storage and/or the S3 variables `CLI_BACKUP_S3_BUCKET`, `CLI_BACKUP_S3_ENDPOINT`, `CLI_BACKUP_S3_REGION`, `CLI_BACKUP_S3_KEY`, `CLI_BACKUP_S3_SECRET`, and optionally `CLI_BACKUP_S3_PREFIX`. Project environment values override global environment values; explicit scheduled CLI options override project values. `--project-env` accepts literal assignments and does not execute shell code. Period keys are relative to the optional S3 prefix:

```text
daily/<project>/<source>/YYYY-MM-DD.<database>.sql.gz
weekly/<project>/<source>/YYYY-MM-DD.<database>.sql.gz
monthly/<project>/<source>/YYYY-MM-DD.<database>.sql.gz
.state/<project>/<source>/...  # schedule metadata
```

SQLite uses `.sqlite.gz`. `CLI_BACKUP_PERIOD_DAILY` and `CLI_BACKUP_PERIOD_MONTHLY` default to `true`; `CLI_BACKUP_PERIOD_WEEKLY` defaults to `false`. With daily enabled, beginning on Wednesday or the third day of the month, the latest eligible daily copy from the preceding week or month is copied to the relevant period if missing. When both select the same Sunday, weekly chooses an earlier snapshot. With daily disabled, a new weekly or monthly snapshot is created on or after the threshold if the current period is missing. Run the command from an external scheduler, normally daily, so missed attempts can be retried. A completed period may not create a new snapshot on a repeated invocation.

`CLI_BACKUP_LOCAL_DAYS` sets local retention, with minimum retention for period selection and idempotency. The object storage service controls S3 retention. Required destinations are attempted independently, and errors cause a nonzero exit. S3 publishing reads and verifies the complete uploaded object before publishing its manifest. The default CPU scheduling priority is `nice 10` for the CLI and container dump client; configure `CLI_BACKUP_CPU_NICE` from 0 to 19. This does not limit the database server's CPU or I/O.

## Generic CLI

Run `cli-backup --help` for global options and commands. Generic CLI options precede the subcommand. Its configuration uses `CLI_BACKUP_PROJECT` and `CLI_BACKUP_DB_MODE` (`compose`, `container`, or `tcp`), which are distinct from the scheduled interface's project and connection variables.

```sh
cli-backup backup mysql
cli-backup backup redis
cli-backup backup files --directory /srv/example --exclude '*.tmp' --exclude 'cache/*'
cli-backup list files --storage local
cli-backup verify files --storage local --file NAME
```

Directory archives are full scans, not point-in-time filesystem snapshots. Exclusion patterns use Python `fnmatch` relative to the source; `*` can match `/`. Symlinks are archived without following them, hard links are archived separately, and unsafe escaping links or special files must be excluded. Extraction requires a new absolute destination and rejects unsafe paths; permissions are restored without setuid/setgid bits, and ownership is not restored. See `cli-backup restore --help` before recovery.

MariaDB/MySQL dumps use `--single-transaction`; coordinate schema changes and nontransactional tables. Object-count comparisons do not prove row-level equivalence. The local and S3 files are sensitive; ensure sufficient free temporary space and protect the destination.

## Development and AI agent skills

```sh
make venv install
make test lint
```

CI runs unit tests on Python 3.11–3.13, lint, build and installed-wheel smoke tests. `make test-integration` requires explicitly selected disposable images and may perform database restoration; run it only when authorized. Project-portable skills for agents are in [`.agents/skills/`](.agents/skills/): `cli-backup-setup` covers integration and `cli-backup-develop` covers implementation and testing. They do not authorize storage administration, schedule changes, or restoration.

## License

[MIT](LICENSE)
