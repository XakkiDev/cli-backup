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
| `CLI_BACKUP_TEST_CPUS`, `CLI_BACKUP_TEST_MEMORY` | Disposable container limits (default `2` CPUs and `3g` memory) |
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
# Recover an already uploaded object whose manifest was not published:
cli-backup publish-local mysql --file NAME
cli-backup restore-test mysql --storage local
cli-backup restore-test redis --storage s3 --file NAME
cli-backup restore mysql --storage s3 --file NAME --target example
cli-backup restore files --file NAME --destination /srv/restore-new
cli-backup download redis --file NAME --destination /tmp/dump.rdb
```

Patterns use Python `fnmatch` against paths relative to the source directory; `*` can match a slash. Archives use gzip-compressed tar. Symlinks are recorded without following them; hard-linked regular files are archived separately. Symlinks that resolve outside the source tree, symlink cycles, sockets, FIFOs and device files must be excluded; otherwise the backup fails before publication. Extraction requires a new absolute destination and rejects escaping paths, symlink chains that escape the destination or contain cycles, hard links and special files. Permissions are restored without setuid/setgid bits. Ownership is recorded but is not restored; the CLI reports this limitation. POSIX `O_NOFOLLOW` and directory file descriptors are required for safe extraction. Do not restore archives from an untrusted source as a privileged user.

`publish-local` publishes an existing S3 object only after both its local copy and the S3 object match the local manifest checksum; it never replaces an existing manifest. Large S3 downloads and verification use bounded ranges with retries.

A backup is visible only when its manifest is published. Each destination is attempted independently; any failure produces a nonzero exit, and local pruning happens only after every destination succeeds. S3 verification downloads and hashes the complete object. S3 objects use project/source namespaces under an optional prefix; unique names and conditional writes prevent replacing existing objects. Multipart uploads may require S3 multipart permissions. Local backups and temporary files contain sensitive unencrypted data; secure the host filesystem and S3 transport and access policies. The temporary directory must have enough space for a full backup or download; use `CLI_BACKUP_TEMP_ROOT` to choose a different filesystem. Downloads are private files (mode 0600), published atomically. Their destination directory must be owned by the current user or root and must not be writable by other users unless it has the sticky bit (as with `/tmp`).

Directory archives are full scans, not point-in-time snapshots: quiesce writers or use a filesystem snapshot when cross-file consistency is required. MySQL dumps use `--single-transaction`; coordinate DDL and nontransactional tables separately.

For new MySQL backups, the manifest records object counts captured immediately before and after the dump; the backup fails if those counts change during capture. Restore tests compare against the recorded counts, so later schema changes do not invalidate an older snapshot. Legacy manifests lack these counts and are compared with the current source schema instead. Counts alone do not prove row-level equivalence or detect all concurrent DDL.

Database live restore requires an explicit target and a terminal with typed confirmation; stop writers first. Restore tests launch and remove a uniquely named, network-isolated container from the configured image. Redis live restoration is manual: download and verify the RDB, stop the intended Redis instance, install the file with its required permissions, and restart according to its operator documentation. Run backup commands from an external scheduler and alert on nonzero exit; this tool does not install cron jobs.

## Integration tests

`make test-integration` skips image-dependent tests unless explicit `CLI_BACKUP_INTEGRATION_MYSQL_IMAGE`, `CLI_BACKUP_INTEGRATION_REDIS_IMAGE` and `CLI_BACKUP_INTEGRATION_S3_IMAGE` are set. The Redis integration additionally needs `redis-cli` and `redis-check-rdb` on PATH. To run the rootless Compose fixture, select an isolated rootless Docker context or `DOCKER_HOST` and set `CLI_BACKUP_INTEGRATION_ROOTLESS=1` with `CLI_BACKUP_INTEGRATION_MYSQL_IMAGE`. Set `CLI_BACKUP_INTEGRATION_REDIS_IMAGE` and optionally `CLI_BACKUP_INTEGRATION_REDIS_SERVER=keydb-server` to exercise the rootless Redis rehearsal. Tests create uniquely named disposable containers; the Redis and S3 fixtures bind a random loopback-only port. Only the disposable S3 fixture creates a bucket.

## Shared Make targets for scheduled MariaDB/MySQL backups

Include `share.Makefile` in the project Makefile. For a running Compose service,
set `CLI_BACKUP_COMPOSE_PROJECT_NAME` and `CLI_BACKUP_DB_CONTAINER` to the
Compose project and service names. The CLI locates exactly one running container by Compose labels and uses
`docker exec`; no Compose files are read. Set `CLI_BACKUP_CONTAINER_RUNTIME=podman`
for local rootless Podman; Docker is the default. For a
standalone container set `CLI_BACKUP_PROJECT_NAME` and
`CLI_BACKUP_DB_CONTAINER` to its name or ID. For TCP, set
`CLI_BACKUP_PROJECT_NAME`, `CLI_BACKUP_DB_HOST`, optionally `CLI_BACKUP_DB_PORT`,
`CLI_BACKUP_DB_NAME`, `CLI_BACKUP_DB_USER` and `CLI_BACKUP_DB_PASSWORD`.
Use the selected user Docker context or `DOCKER_HOST` for rootless Docker.

`CLI_BACKUP_DB_SERVICE_NAME` defaults to `mariadb`; `mysql` is also supported.
An explicit comma-separated `CLI_BACKUP_DB_NAME` selects multiple databases;
otherwise container mode uses `MARIADB_DATABASE` (or `MYSQL_DATABASE`).
Each database gets a separate gzip dump. Container mode defaults to root and
`MARIADB_ROOT_PASSWORD` (or `MYSQL_ROOT_PASSWORD`) from the container; set
`CLI_BACKUP_DB_USER` and `CLI_BACKUP_DB_PASSWORD` together to override it.
Passwords belong in environment files protected from other users, not arguments.

Backups are stored under `daily/<project>/<service>/<YYYY-MM-DD>.<database>.sql.gz`;
weekly and monthly copies use corresponding top-level prefixes and their own
manifests. Period flags are `CLI_BACKUP_PERIOD_DAILY=true`,
`CLI_BACKUP_PERIOD_MONTHLY=true` and `CLI_BACKUP_PERIOD_WEEKLY=false` by default.
If daily is enabled, starting on the third day
of each month and Wednesday, the latest daily copy from the previous period is
copied to monthly or weekly if missing. Monthly selection has priority when a
Sunday qualifies for both: weekly selects the next earlier daily copy. Originals
remain in daily. When daily is disabled, a new dump is created on or after
Wednesday or the third day of the month if the current week or month has no
backup; one dump is reused if both periods are due. A durable `.state` marker tracks when each database joined the
schedule, so periods before onboarding are not reported missing. Storage rules
must be configured separately before enabling this format.

`CLI_BACKUP_LOCAL_DAYS` limits local copies; daily files are kept at least 14
days if monthly or weekly is enabled to allow period selection; weekly and
monthly copies are kept at least 8 and 35 days respectively so period detection
remains idempotent. S3 retention is
managed by the storage service. The scheduled command preserves successfully
published copies if another destination fails and returns a nonzero exit.
One date identifies one database dump: rerunning verifies the existing copy and
completes any missing destination rather than overwriting it.

Targets: `cli-backup@config-check`, `cli-backup@backup`, `cli-backup@list`,
`cli-backup@verify`, `cli-backup@restore-test`, and `cli-backup@restore`.
Use `where=local|s3`, `path=daily|weekly|monthly`, `file=<name>` and
`db=<name>` as appropriate. Live restore requires an explicit file, a terminal
and confirmation; stop database writers first. Restore-test uses a disposable
isolated container. For multiple databases pass `db=<name>` to select one.
External cron schedules the backup; CLI does not install cron jobs. Full dumps
of multiple databases are separate and do not form one transactional snapshot.

The standalone scheduled CLI also accepts `--project-env <file>` and explicit
options such as `--project-name`, `--db-container`, `--db-host`, `--db-name`
and `--local-root` after its action. Explicit options override project-file
values, which override global environment values. Passwords have no command
line option; supply them through a protected environment or project file.

Backups lower CPU scheduling priority to `nice 10` for the CLI (including gzip)
and, for Docker modes, run the dump client with `nice 10` inside the container.
Set `CLI_BACKUP_CPU_NICE` to an integer from 0 to 19; `0` does not lower the
priority. A more restricted inherited priority is never raised. The database
server itself and restore operations are unchanged. Docker database images must
provide `nice` when this setting is nonzero. This is a scheduling preference,
not a CPU or I/O limit on the database server.

## SQLite snapshots

Set `CLI_BACKUP_PROJECT_NAME`, `CLI_BACKUP_DB_SERVICE_NAME=sqlite`, and
an absolute `CLI_BACKUP_DB_PATH`. SQLite online backup API takes a consistent
snapshot of the running database, including committed WAL transactions, without
stopping the writer. The artifact is `daily/<project>/sqlite/` followed by
`YYYY-MM-DD.<database>.sqlite.gz` and its manifest. The default database name is
the source filename without its final extension; `CLI_BACKUP_DB_NAME` can set
one explicit name. No SQL database username or container is needed. Automatic
live restore is not implemented; retain the verified gzip file for manual
recovery.
