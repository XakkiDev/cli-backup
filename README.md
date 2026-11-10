# cli-backup

A host Python CLI for full MariaDB/MySQL dumps, Redis RDB snapshots and directory archives. Backups can go to local storage, S3-compatible storage, or both. The CLI does not create buckets, set lifecycle policies or configure cron. Python 3.11+ and a working `venv`/`pip` installation are required.

```sh
make venv install
make test lint
.venv/bin/cli-backup --help
# Install as a user command in an existing user-managed environment:
pipx install .
```

On distributions that package `venv`/`ensurepip` separately, install the matching system package before creating `.venv`. The database client binaries used by a connection mode must be installed separately. `docker` and Compose must use the intended Docker context; rootless Docker is supported.

## Configuration

The priority is CLI options, explicit project file, then the process environment. The project file is passed with `--project-env path` and is parsed as literal shell-style assignments; it does not execute code or expand variables. Protect files containing credentials. Set secrets only in environment variables or a protected project file, never in CLI arguments.

| Variable | Meaning |
| --- | --- |
| `CLI_BACKUP_PROJECT` | Required project identifier: letters, digits, `_`, `-` |
| `CLI_BACKUP_LOCAL_ROOT` | Local root; omitted to disable local storage |
| `CLI_BACKUP_TEMP_ROOT` | Optional temporary working directory for dump and restore data; defaults to the system temporary directory |
| `CLI_BACKUP_LOCAL_DAYS` | Optional positive retention days; applied after every required destination succeeds |
| `CLI_BACKUP_S3_BUCKET` | S3 bucket; omitted to disable S3 |
| `CLI_BACKUP_S3_ENDPOINT`, `CLI_BACKUP_S3_REGION`, `CLI_BACKUP_S3_PREFIX` | Endpoint, region and optional key prefix |
| `CLI_BACKUP_S3_KEY`, `CLI_BACKUP_S3_SECRET` | S3 access credentials; otherwise boto3's standard credential chain applies |
| `CLI_BACKUP_DB_MODE` | `compose` (default), `container` or `tcp` |
| `CLI_BACKUP_DB_NAME`, `CLI_BACKUP_DB_USER`, `CLI_BACKUP_DB_PASSWORD` | Database name and credentials |
| `CLI_BACKUP_DB_HOST`, `CLI_BACKUP_DB_PORT` | Database host and port for `tcp`/`container` |
| `CLI_BACKUP_DB_SERVICE`, `CLI_BACKUP_COMPOSE_DIR` | Compose service and project directory |
| `CLI_BACKUP_DB_NETWORK`, `CLI_BACKUP_DB_IMAGE` | Docker network and client image for `container` mode |
| `CLI_BACKUP_DB_CLIENT` | `mariadb` (default) or `mysql`; selects corresponding dump and restore binaries |
| `CLI_BACKUP_REDIS_HOST`, `CLI_BACKUP_REDIS_PORT`, `CLI_BACKUP_REDIS_PASSWORD` | Redis endpoint and optional password |
| `CLI_BACKUP_REDIS_TIMEOUT` | Maximum time in seconds for Redis snapshot transfer and its validation (default 14400 each) |
| `CLI_BACKUP_TEST_MYSQL_IMAGE`, `CLI_BACKUP_TEST_REDIS_IMAGE` | Images explicitly selected for isolated restore tests |
| `CLI_BACKUP_TEST_REDIS_SERVER` | `redis-server` (default) or `keydb-server` |
| `CLI_BACKUP_RESTORE_TIMEOUT` | Maximum MySQL rehearsal import time in seconds (default 14400) |

For multiple Compose env files, pass `--compose-env-file` repeatedly. The order is preserved and the last file wins, as in Compose. `docker compose exec` uses the current Docker context; no socket or rootful daemon is assumed. `container` mode creates a temporary client container in the specified network. TCP mode requires matching `mariadb-dump`/`mariadb` or `mysqldump`/`mysql` executables on the host. Redis capture requires host `redis-cli` and `redis-check-rdb`.

## Project Makefile example

Create a private `.backup.env` file in the project and exclude it from version control:

```dotenv
CLI_BACKUP_PROJECT=example
CLI_BACKUP_DB_MODE=compose
CLI_BACKUP_DB_SERVICE=mariadb
CLI_BACKUP_DB_NAME=example
CLI_BACKUP_DB_USER=backup_user
CLI_BACKUP_DB_PASSWORD=replace-with-project-password
# Omit LOCAL_ROOT and S3 settings here to inherit the host defaults.
```

Restrict the file with `chmod 600 .backup.env`. In the project's Makefile:

```make
BACKUP_CLI ?= cli-backup

backup:
	$(BACKUP_CLI) --project-env .backup.env --compose-env-file .env backup mysql

backup-list:
	$(BACKUP_CLI) --project-env .backup.env list mysql --storage local
```

The project file overrides matching host variables, and explicit CLI arguments
override both. Compose settings are loaded independently using the selected
`--compose-env-file` arguments; pass every required Compose env file in order.
Do not place secrets in Make command arguments. The scheduler can run the Make
target and alert on its exit status.

## Operations

Options common to the CLI precede the command. The examples assume environment variables are set or `--project-env` points to a project file.

```sh
cli-backup config-check
cli-backup backup mysql
cli-backup backup redis
cli-backup backup files --directory /srv/example --exclude '*.tmp' --exclude 'cache/*'
cli-backup list mysql --storage s3
cli-backup verify mysql --storage s3 --file NAME
cli-backup restore-test mysql --storage local
cli-backup restore-test redis --storage s3 --file NAME
cli-backup restore mysql --storage s3 --file NAME --target example
cli-backup restore files --file NAME --destination /srv/restore-new
cli-backup download redis --file NAME --destination /tmp/dump.rdb
```

Patterns use Python `fnmatch` against paths relative to the source directory; `*` can match a slash. Archives use gzip-compressed tar. Symlinks are recorded without following them; hard-linked regular files are archived separately. Symlinks that resolve outside the source tree, symlink cycles, sockets, FIFOs and device files must be excluded; otherwise the backup fails before publication. Extraction requires a new absolute destination and rejects escaping paths, symlink chains that escape the destination or contain cycles, hard links and special files. Permissions are restored without setuid/setgid bits. Ownership is recorded but is not restored; the CLI reports this limitation. POSIX `O_NOFOLLOW` and directory file descriptors are required for safe extraction. Do not restore archives from an untrusted source as a privileged user.

A backup is visible only when its manifest is published. Each destination is attempted independently; any failure produces a nonzero exit, and local pruning happens only after every destination succeeds. S3 verification downloads and hashes the complete object. S3 objects use project/source namespaces under an optional prefix; unique names and conditional writes prevent replacing existing objects. Multipart uploads may require S3 multipart permissions. Local backups and temporary files contain sensitive unencrypted data; secure the host filesystem and S3 transport and access policies. The temporary directory must have enough space for a full backup or download; use `CLI_BACKUP_TEMP_ROOT` to choose a different filesystem. Downloads are private files (mode 0600), published atomically. Their destination directory must be owned by the current user or root and must not be writable by other users unless it has the sticky bit (as with `/tmp`).

Directory archives are full scans, not point-in-time snapshots: quiesce writers or use a filesystem snapshot when cross-file consistency is required. MySQL dumps use `--single-transaction`; coordinate DDL and nontransactional tables separately.

Database live restore requires an explicit target and a terminal with typed confirmation; stop writers first. Restore tests launch and remove a uniquely named, network-isolated container from the configured image. Redis live restoration is manual: download and verify the RDB, stop the intended Redis instance, install the file with its required permissions, and restart according to its operator documentation. Run backup commands from an external scheduler and alert on nonzero exit; this tool does not install cron jobs.

## Integration tests

`make test-integration` skips image-dependent tests unless explicit `CLI_BACKUP_INTEGRATION_MYSQL_IMAGE`, `CLI_BACKUP_INTEGRATION_REDIS_IMAGE` and `CLI_BACKUP_INTEGRATION_S3_IMAGE` are set. The Redis integration additionally needs `redis-cli` and `redis-check-rdb` on PATH. To run the rootless Compose fixture, select an isolated rootless Docker context or `DOCKER_HOST` and set `CLI_BACKUP_INTEGRATION_ROOTLESS=1` with `CLI_BACKUP_INTEGRATION_MYSQL_IMAGE`. Set `CLI_BACKUP_INTEGRATION_REDIS_IMAGE` and optionally `CLI_BACKUP_INTEGRATION_REDIS_SERVER=keydb-server` to exercise the rootless Redis rehearsal. Tests create uniquely named disposable containers; the Redis and S3 fixtures bind a random loopback-only port. Only the disposable S3 fixture creates a bucket.
