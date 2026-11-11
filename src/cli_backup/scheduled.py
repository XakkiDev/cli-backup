"""Validated daily SQL backups and idempotent period copies."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sqlite3
import stat
from contextlib import closing
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import read_project_env, validate_project
from .cpu_priority import lower_cpu_priority, validate_cpu_nice
from .mysql import MySQLConfig, count_mysql_objects, restore_mysql
from .rehearsal import rehearse_mysql
from .storage import LocalStorage, S3Storage, local_get, make_manifest, prune_local, s3_get, s3_list

NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,63}\Z')


def flag(value: str, name: str) -> bool:
    if value.lower() not in ('true', 'false'):
        raise ValueError(f'{name} must be true or false')
    return value.lower() == 'true'


def databases(value: str) -> list[str]:
    names = value.split(',')
    if not names or any(not NAME.fullmatch(n) for n in names) or len(set(names)) != len(names):
        raise ValueError('CLI_BACKUP_DB_NAME must be a comma-separated list of distinct database names')
    return names


@dataclass(frozen=True)
class Settings:
    project: str
    compose: str
    container: str
    service: str
    host: str
    port: int
    names: str
    user: str
    password: str
    daily: bool
    weekly: bool
    monthly: bool
    local_root: str
    bucket: str
    s3_endpoint: str
    s3_region: str
    s3_key: str
    s3_secret: str
    local_days: str
    temp_root: str
    test_image: str
    cpu_nice: int
    container_runtime: str
    db_path: str
    s3_prefix: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Settings:
        compose = env.get('CLI_BACKUP_COMPOSE_PROJECT_NAME', '')
        project = env.get('CLI_BACKUP_PROJECT_NAME') or compose
        if not project:
            raise ValueError('CLI_BACKUP_PROJECT_NAME is required unless CLI_BACKUP_COMPOSE_PROJECT_NAME is set')
        validate_project(project)
        if compose:
            validate_project(compose)
        container = env.get('CLI_BACKUP_DB_CONTAINER', '')
        host = env.get('CLI_BACKUP_DB_HOST', '')
        db_path = env.get('CLI_BACKUP_DB_PATH', '')
        runtime = env.get('CLI_BACKUP_CONTAINER_RUNTIME', 'docker')
        if runtime not in ('docker', 'podman'):
            raise ValueError('CLI_BACKUP_CONTAINER_RUNTIME must be docker or podman')
        service = env.get('CLI_BACKUP_DB_SERVICE_NAME', 'mariadb').lower()
        if service not in ('mariadb', 'mysql', 'sqlite', 'postgresql'):
            raise ValueError('unsupported CLI_BACKUP_DB_SERVICE_NAME; supported: mariadb, mysql, sqlite, postgresql')
        if service == 'sqlite':
            if not db_path or not Path(db_path).is_absolute() or container or host or compose:
                raise ValueError('SQLite requires an absolute CLI_BACKUP_DB_PATH and no container or host')
        else:
            if db_path or bool(container) == bool(host):
                raise ValueError('set exactly one of CLI_BACKUP_DB_CONTAINER or CLI_BACKUP_DB_HOST')
            if compose and not container:
                raise ValueError('Compose project requires CLI_BACKUP_DB_CONTAINER')
            if container and not NAME.fullmatch(container):
                raise ValueError('invalid CLI_BACKUP_DB_CONTAINER')
            if host and (host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.:-]+', host)):
                raise ValueError('invalid CLI_BACKUP_DB_HOST')
        port = int(env.get('CLI_BACKUP_DB_PORT', '5432' if service == 'postgresql' else '3306'))
        if not 1 <= port <= 65535:
            raise ValueError('invalid CLI_BACKUP_DB_PORT')
        names = env.get('CLI_BACKUP_DB_NAME', '')
        if names:
            parsed_names = databases(names)
            if service == 'sqlite' and len(parsed_names) != 1:
                raise ValueError('SQLite accepts exactly one database name')
        if host and not names:
            raise ValueError('CLI_BACKUP_DB_NAME is required for TCP')
        user = env.get('CLI_BACKUP_DB_USER', '')
        password = env.get('CLI_BACKUP_DB_PASSWORD', '')
        if bool(user) != bool(password):
            raise ValueError('CLI_BACKUP_DB_USER and CLI_BACKUP_DB_PASSWORD must be supplied together')
        if host and not user:
            raise ValueError('TCP requires CLI_BACKUP_DB_USER and CLI_BACKUP_DB_PASSWORD')
        root, bucket = env.get('CLI_BACKUP_LOCAL_ROOT', ''), env.get('CLI_BACKUP_S3_BUCKET', '')
        if not root and not bucket:
            raise ValueError('configure CLI_BACKUP_LOCAL_ROOT or CLI_BACKUP_S3_BUCKET')
        if bucket and any(not env.get('CLI_BACKUP_S3_' + key) for key in ('KEY', 'SECRET', 'ENDPOINT', 'REGION')):
            raise ValueError('S3 configuration is incomplete')
        if env.get('CLI_BACKUP_LOCAL_DAYS') and int(env['CLI_BACKUP_LOCAL_DAYS']) < 1:
            raise ValueError('CLI_BACKUP_LOCAL_DAYS must be positive')
        daily = flag(env.get('CLI_BACKUP_PERIOD_DAILY', 'true'), 'CLI_BACKUP_PERIOD_DAILY')
        weekly = flag(env.get('CLI_BACKUP_PERIOD_WEEKLY', 'false'), 'CLI_BACKUP_PERIOD_WEEKLY')
        monthly = flag(env.get('CLI_BACKUP_PERIOD_MONTHLY', 'true'), 'CLI_BACKUP_PERIOD_MONTHLY')
        if not any((daily, weekly, monthly)):
            raise ValueError('at least one period must be enabled')
        return cls(project, compose, container, service, host, port, names, user, password,
                   daily, weekly, monthly, root, bucket, env.get("CLI_BACKUP_S3_ENDPOINT", ""),
                   env.get("CLI_BACKUP_S3_REGION", ""), env.get("CLI_BACKUP_S3_KEY", ""),
                   env.get("CLI_BACKUP_S3_SECRET", ""), env.get("CLI_BACKUP_LOCAL_DAYS", ""),
                   env.get("CLI_BACKUP_TEMP_ROOT", ""), env.get("CLI_BACKUP_TEST_MYSQL_IMAGE", ""),
                   validate_cpu_nice(env.get("CLI_BACKUP_CPU_NICE", "10")), runtime, db_path,
                   env.get("CLI_BACKUP_S3_PREFIX", "").strip("/"))


def container_info(settings: Settings) -> tuple[str, dict[str, str], str]:
    if settings.compose:
        result = subprocess.run([settings.container_runtime, 'ps', '--filter', 'label=com.docker.compose.project=' + settings.compose,
                                 '--filter', 'label=com.docker.compose.service=' + settings.container,
                                 '--quiet', '--no-trunc'],
                                check=True, capture_output=True, text=True, timeout=30)
        ids = result.stdout.split()
        if len(ids) != 1:
            raise ValueError('expected exactly one running Compose database container')
        target = ids[0]
    else:
        target = settings.container
    result = subprocess.run([settings.container_runtime, 'inspect', target], check=True, capture_output=True, text=True, timeout=30)
    info = json.loads(result.stdout)
    if len(info) != 1 or not info[0].get('State', {}).get('Running'):
        raise ValueError('database container is not running')
    if settings.compose and (info[0].get('Config', {}).get('Labels') or {}).get('com.docker.compose.oneoff', '').lower() == 'true':
        raise ValueError('one-off containers are not backup targets')
    environment = {}
    for item in info[0].get('Config', {}).get('Env') or []:
        key, sep, value = item.partition('=')
        if sep:
            environment[key] = value
    return info[0]['Id'], environment, info[0]['Config']['Image']


@dataclass(frozen=True)
class PostgreSQLConfig:
    database: str
    user: str
    password: str
    host: str
    port: int
    target: str
    runtime: str
    cpu_nice: int


def connection(settings: Settings) -> tuple[MySQLConfig | PostgreSQLConfig | None, list[str], str]:
    if settings.service == 'sqlite':
        names = databases(settings.names or Path(settings.db_path).stem)
        return None, names, ''
    target, env, image = container_info(settings) if settings.container else ('', {}, '')
    if settings.service == 'postgresql':
        names = databases(settings.names or env.get('POSTGRES_DB') or env.get('POSTGRES_USER') or '')
        user = settings.user or env.get('POSTGRES_USER') or 'postgres'
        password = settings.password or env.get('POSTGRES_PASSWORD') or ''
        if not user or user.startswith('-'):
            raise ValueError('invalid PostgreSQL user')
        if not password:
            raise ValueError('database password is missing; set CLI_BACKUP_DB_USER and CLI_BACKUP_DB_PASSWORD')
        return PostgreSQLConfig(names[0], user, password, settings.host or '127.0.0.1',
                                settings.port, target, settings.container_runtime, settings.cpu_nice), names, image
    names = databases(settings.names or env.get('MARIADB_DATABASE') or env.get('MYSQL_DATABASE') or '')
    user = settings.user or 'root'
    password = settings.password or env.get('MARIADB_ROOT_PASSWORD') or env.get('MYSQL_ROOT_PASSWORD') or ''
    if not password:
        raise ValueError('database password is missing; set CLI_BACKUP_DB_USER and CLI_BACKUP_DB_PASSWORD')
    return (MySQLConfig('exec' if target else 'tcp', names[0], user, password,
                        host=settings.host or '127.0.0.1', port=settings.port,
                        service=target, client=settings.service, cpu_nice=settings.cpu_nice,
                        container_runtime=settings.container_runtime), names, image)


def stores(settings: Settings, period: str) -> list[tuple[str, object]]:
    result: list[tuple[str, object]] = []
    if settings.local_root:
        result.append(('local', LocalStorage(Path(settings.local_root).expanduser() / period)))
    if settings.bucket:
        import boto3
        from botocore.config import Config
        client = boto3.client('s3', endpoint_url=settings.s3_endpoint,
                              region_name=settings.s3_region,
                              aws_access_key_id=settings.s3_key,
                              aws_secret_access_key=settings.s3_secret,
                              config=Config(s3={'addressing_style': 'path'}))
        result.append(('s3', S3Storage(client, settings.bucket, '/'.join(filter(None, (settings.s3_prefix, period))))))
    return result


def list_store(store, project: str, source: str):
    return store.list(project, source) if isinstance(store, LocalStorage) else s3_list(store, project, source)


def get(store, manifest, path: Path) -> None:
    with path.open('xb') as target:
        (local_get if isinstance(store, LocalStorage) else s3_get)(store, manifest, target)


def capture_gzip(config: MySQLConfig, path: Path) -> None:
    from .mysql import command
    process = subprocess.Popen(command(config), env=dict(os.environ, MYSQL_PWD=config.password), stdout=subprocess.PIPE)
    assert process.stdout is not None
    try:
        size = 0
        with gzip.open(path, 'xb') as out:
            while chunk := process.stdout.read(1024 * 1024):
                out.write(chunk)
                size += len(chunk)
        process.stdout.close()
        if process.wait() != 0 or not size:
            raise ValueError('database dump failed or is empty')
    except BaseException:
        process.kill()
        process.wait()
        raise


def capture_postgresql(config: PostgreSQLConfig, name: str, path: Path) -> None:
    prefix = [config.runtime, 'exec', '-i', '-e', 'PGPASSWORD', config.target] if config.target else []
    nice = ['nice', '-n', str(config.cpu_nice)] if config.target and config.cpu_nice else []
    command = prefix + nice + ['pg_dump', '--no-password', '--host', config.host,
                                '--port', str(config.port), '--username', config.user, '--dbname', name]
    process = subprocess.Popen(command, env=dict(os.environ, PGPASSWORD=config.password),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    assert process.stdout is not None
    try:
        size = 0
        with gzip.open(path, 'xb') as output:
            while chunk := process.stdout.read(1024 * 1024):
                output.write(chunk)
                size += len(chunk)
        process.stdout.close()
        if process.wait() != 0 or not size:
            raise ValueError('PostgreSQL dump failed or is empty')
    except BaseException:
        process.kill()
        process.wait()
        raise


def capture_source(settings: Settings, cfg: MySQLConfig | PostgreSQLConfig | None, name: str, artifact: Path, tmp: Path) -> str | None:
    if settings.service == 'postgresql':
        if not isinstance(cfg, PostgreSQLConfig):
            raise ValueError('missing PostgreSQL connection')
        capture_postgresql(cfg, name, artifact)
        return None
    if settings.service != 'sqlite':
        if cfg is None:
            raise ValueError('missing database connection')
        database = MySQLConfig(**{**vars(cfg), 'database': name})
        before = count_mysql_objects(database)
        capture_gzip(database, artifact)
        if count_mysql_objects(database) != before:
            raise ValueError('schema object counts changed during dump')
        return before
    source = Path(settings.db_path)
    if source.resolve(strict=True) != source or not stat.S_ISREG(source.lstat().st_mode):
        raise ValueError('SQLite source must be an existing canonical regular file')
    output = tmp / (name + '.sqlite')
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader:
        with closing(sqlite3.connect(output)) as writer:
            reader.backup(writer)
            writer.execute('PRAGMA journal_mode=DELETE')
    with output.open('rb') as source_file, gzip.open(artifact, 'xb') as target:
        shutil.copyfileobj(source_file, target, 1024 * 1024)
    return None


def backup_suffix(settings: Settings, name: str) -> str:
    extension = 'sqlite.gz' if settings.service == 'sqlite' else 'sql.gz'
    return '.' + name + '.' + extension


def period_start(store, project: str, source: str, name: str, available) -> date:
    """Persist when this database joined the schedule, to avoid inventing pre-adoption gaps."""
    key = f'.cli-backup-start-{name}'
    if isinstance(store, LocalStorage):
        folder = store.root.parent / '.state' / project / source
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / key
        try:
            value = path.read_text(encoding='ascii')
        except FileNotFoundError:
            if not available:
                raise ValueError('no daily backup from which to initialize period tracking')
            value = min(m.timestamp[:10] for m in available)
            try:
                with path.open('x', encoding='ascii') as file:
                    os.chmod(path, 0o600)
                    file.write(value)
                    file.flush()
                    os.fsync(file.fileno())
            except FileExistsError:
                value = path.read_text(encoding='ascii')
    else:
        from botocore.exceptions import ClientError
        object_key = '/'.join(filter(None, (store.prefix.rsplit('/', 1)[0] if '/' in store.prefix else '', '.state', project, source, key)))
        try:
            body = store.client.get_object(Bucket=store.bucket, Key=object_key)['Body']
            try:
                value = body.read(32).decode('ascii')
            finally:
                body.close()
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
                raise
            if not available:
                raise ValueError('no daily backup from which to initialize period tracking')
            value = min(m.timestamp[:10] for m in available)
            try:
                store.client.put_object(Bucket=store.bucket, Key=object_key, Body=value.encode('ascii'), IfNoneMatch='*')
            except ClientError as race:
                if race.response.get('Error', {}).get('Code') not in ('409', '412', 'PreconditionFailed', 'ConditionalRequestConflict'):
                    raise
                body = store.client.get_object(Bucket=store.bucket, Key=object_key)['Body']
                try:
                    value = body.read(32).decode('ascii')
                finally:
                    body.close()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('invalid period tracking date')
    return date.fromisoformat(value)


def period_copies(settings: Settings, name: str, today: date, tmp: Path) -> None:
    previous_month_last = today.replace(day=1) - timedelta(days=1)
    previous_monday = today - timedelta(days=today.weekday() + 7)
    previous_sunday = previous_monday + timedelta(days=6)
    failures = []
    for label, daily in stores(settings, 'daily'):
        try:
            sources = [m for m in list_store(daily, settings.project, settings.service)
                       if m.artifact.endswith(backup_suffix(settings, name))]
            start = period_start(daily, settings.project, settings.service, name, sources)
            monthly = dict(stores(settings, 'monthly')).get(label)
            weekly = dict(stores(settings, 'weekly')).get(label)
            monthly_selected = []
            if settings.monthly:
                monthly_selected = [m for m in list_store(monthly, settings.project, settings.service)
                                    if m.artifact.endswith(backup_suffix(settings, name))
                                    and m.timestamp[:7] == previous_month_last.isoformat()[:7]]
            if settings.monthly and previous_month_last >= start and today.day >= 3 and not monthly_selected:
                candidates = [m for m in sources if m.timestamp[:7] == previous_month_last.isoformat()[:7]]
                if not candidates:
                    raise ValueError(f'no daily backup for previous month: {name}')
                chosen = candidates[-1]
                file = tmp / (label + '-monthly-' + chosen.artifact)
                get(daily, chosen, file)
                put_with_recovery(monthly, file, chosen)
                monthly_selected = [chosen]
                print(f'{label}: monthly/{chosen.artifact}')
            if settings.weekly and previous_sunday >= start and today.weekday() >= 2:
                existing = [m for m in list_store(weekly, settings.project, settings.service)
                            if m.artifact.endswith(backup_suffix(settings, name))
                            and previous_monday <= date.fromisoformat(m.timestamp[:10]) <= previous_sunday]
                if not existing:
                    reserved = {m.artifact for m in monthly_selected}
                    candidates = [m for m in sources if previous_monday <= date.fromisoformat(m.timestamp[:10]) <= previous_sunday
                                  and m.artifact not in reserved]
                    if not candidates:
                        raise ValueError(f'no daily backup for previous week: {name}')
                    chosen = candidates[-1]
                    file = tmp / (label + '-weekly-' + chosen.artifact)
                    get(daily, chosen, file)
                    put_with_recovery(weekly, file, chosen)
                    print(f'{label}: weekly/{chosen.artifact}')
        except Exception as exc:
            print(f'{label}: period copy failed ({type(exc).__name__}: {exc})', file=sys.stderr)
            failures.append(label)
    if failures:
        raise ValueError('period copy incomplete')


def put_with_recovery(store, artifact: Path, manifest) -> None:
    """Complete a previously interrupted publication only when bytes match exactly."""
    try:
        store.put(artifact, manifest)
        return
    except Exception as original:
        if isinstance(store, LocalStorage):
            metadata = store.directory(manifest) / (manifest.artifact + '.json')
            if metadata.exists():
                raise original
            try:
                if not store.verify(manifest):
                    raise original
                fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=metadata.parent)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(manifest.encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary, metadata)
                finally:
                    os.unlink(temporary)
                return
            except (FileNotFoundError, FileExistsError):
                raise original
        try:
            if not store.verify(manifest):
                raise original
            store.client.put_object(Bucket=store.bucket, Key=store.key(manifest) + '.json',
                                    Body=manifest.encode(), IfNoneMatch='*')
            return
        except Exception:
            raise original


def backup_database(settings: Settings, cfg: MySQLConfig, name: str, tmp: Path, now: datetime) -> None:
    destinations = stores(settings, 'daily')
    artifact_name = f'{now:%Y-%m-%d}' + backup_suffix(settings, name)
    existing = []
    missing = []
    failed = []
    for label, store in destinations:
        try:
            found = next((m for m in list_store(store, settings.project, settings.service)
                          if m.artifact == artifact_name), None)
            if found:
                existing.append((label, store, found))
            else:
                missing.append((label, store))
        except Exception as exc:
            print(f'{label}: daily listing failed ({type(exc).__name__})', file=sys.stderr)
            failed.append(label)
    if not existing and not missing:
        raise ValueError('daily backup incomplete')
    artifact = tmp / (name + '.sql.gz')
    if existing:
        manifest = existing[0][2]
        if any(item[2] != manifest for item in existing):
            raise ValueError('daily destinations disagree about the existing backup')
        for label, store, current in existing:
            checked = tmp / (label + '-existing-' + name + '.sql.gz')
            get(store, current, checked)
            if label == existing[0][0]:
                artifact = checked
    else:
        before = capture_source(settings, cfg, name, artifact, tmp)
        manifest = make_manifest(artifact, settings.project, settings.service, artifact_name, now.isoformat(), before)
    for label, store in missing:
        try:
            put_with_recovery(store, artifact, manifest)
            print(f'{label}: daily/{manifest.artifact}')
        except Exception as exc:
            print(f'{label}: backup failed ({type(exc).__name__})', file=sys.stderr)
            failed.append(label)
    if failed:
        raise ValueError('daily backup incomplete')
    period_copies(settings, name, now.date(), tmp)


def direct_period_backups(settings: Settings, cfg: MySQLConfig, name: str, tmp: Path, now: datetime) -> None:
    """Without daily, create one new dump for any due current-period copies."""
    periods = []
    if settings.monthly and now.day >= 3:
        periods.append('monthly')
    if settings.weekly and now.weekday() >= 2:
        periods.append('weekly')
    week_start = now.date() - timedelta(days=now.weekday())
    new_artifact = None
    new_manifest = None
    failures = []
    for period in periods:
        destinations = stores(settings, period)
        existing = []
        missing = []
        for label, store in destinations:
            try:
                candidates = [m for m in list_store(store, settings.project, settings.service)
                              if m.artifact.endswith(backup_suffix(settings, name)) and
                              (m.timestamp[:7] == now.strftime('%Y-%m') if period == 'monthly'
                               else week_start <= date.fromisoformat(m.timestamp[:10]) <= now.date())]
                if candidates:
                    existing.append((label, store, candidates[-1]))
                else:
                    missing.append((label, store))
            except Exception as exc:
                print(f'{label}: {period} listing failed ({type(exc).__name__})', file=sys.stderr)
                failures.append((period, label))
        if not existing and not missing:
            continue
        if existing:
            manifest = existing[0][2]
            if any(current != manifest for _, _, current in existing):
                raise ValueError(f'{period} destinations disagree about the existing backup')
            artifact = tmp / (period + '-existing-' + name + '.sql.gz')
            get(existing[0][1], manifest, artifact)
            for label, store, current in existing[1:]:
                check = tmp / (period + '-' + label + '-' + name + '.sql.gz')
                get(store, current, check)
        else:
            if new_artifact is None:
                new_artifact = tmp / ('direct-' + name + '.gz')
                before = capture_source(settings, cfg, name, new_artifact, tmp)
                new_manifest = make_manifest(new_artifact, settings.project, settings.service,
                                             f'{now:%Y-%m-%d}' + backup_suffix(settings, name), now.isoformat(), before)
            artifact, manifest = new_artifact, new_manifest
        for label, store in missing:
            try:
                put_with_recovery(store, artifact, manifest)
                print(f'{label}: {period}/{manifest.artifact}')
            except Exception as exc:
                print(f'{label}: {period} backup failed ({type(exc).__name__})', file=sys.stderr)
                failures.append((period, label))
    if failures:
        raise ValueError('period backup incomplete')


def prune_local_periods(settings: Settings, now: datetime) -> None:
    retention = settings.local_days
    if not retention or not settings.local_root:
        return
    days = int(retention)
    if days < 1:
        raise ValueError('CLI_BACKUP_LOCAL_DAYS must be positive')
    for period in ('daily', 'weekly', 'monthly'):
        minimum = (14 if settings.weekly or settings.monthly else 0) if period == 'daily' else (8 if period == 'weekly' else 35)
        period_days = max(days, minimum)
        folder = Path(settings.local_root).expanduser() / period / settings.project / settings.service
        if folder.is_dir():
            prune_local(LocalStorage(Path(settings.local_root).expanduser() / period), settings.project, settings.service, period_days, now)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='cli-backup scheduled')
    parser.add_argument('action', choices=('config-check', 'backup', 'list', 'verify', 'restore-test', 'restore'))
    parser.add_argument('--period', choices=('daily', 'weekly', 'monthly'), default='daily')
    parser.add_argument('--storage', choices=('local', 's3'), default='local')
    parser.add_argument('--file')
    parser.add_argument('--database')
    parser.add_argument('--source-project-name')
    parser.add_argument('--source-database')
    parser.add_argument('--project-env', type=Path)
    for option in ('project-name', 'compose-project-name', 'db-container', 'db-service-name',
                   'db-host', 'db-port', 'db-name', 'db-user', 'db-path', 'local-root', 's3-bucket', 'cpu-nice', 'container-runtime'):
        parser.add_argument('--' + option)
    args = parser.parse_args(argv)
    try:
        environment = dict(os.environ)
        if args.project_env:
            environment.update(read_project_env(args.project_env))
        for option in ('project_name', 'compose_project_name', 'db_container', 'db_service_name',
                       'db_host', 'db_port', 'db_name', 'db_user', 'db_path', 'local_root', 's3_bucket', 'cpu_nice', 'container_runtime'):
            value = getattr(args, option)
            if value is not None:
                environment['CLI_BACKUP_' + option.upper()] = value
        settings = Settings.from_env(environment)
        if (args.source_project_name or args.source_database) and args.action != 'restore':
            raise ValueError('source project and source database are only supported for restore')
        source_project = validate_project(args.source_project_name) if args.source_project_name else settings.project
        if args.source_database and len(databases(args.source_database)) != 1:
            raise ValueError('source database must be a single database name')
        if args.action == 'config-check':
            print(f'Configuration valid for project {settings.project}')
            return 0
        if args.action == 'backup':
            lower_cpu_priority(settings.cpu_nice)
        if args.action in ('verify', 'restore') and not args.file:
            raise ValueError('--file is required')
        cfg, names, image = (connection(settings) if args.action in ('backup', 'restore')
                             else (None, databases(settings.names) if settings.names else [], ''))
        if args.database and names and args.database not in names:
            raise ValueError('selected database is not configured')
        if args.database:
            databases(args.database)
        with tempfile.TemporaryDirectory(prefix='cli-backup-', dir=settings.temp_root or None) as directory:
            tmp = Path(directory)
            if args.action == 'backup':
                failures = []
                for name in names:
                    try:
                        now = datetime.now().astimezone()
                        if settings.daily:
                            backup_database(settings, cfg, name, tmp, now)
                        else:
                            direct_period_backups(settings, cfg, name, tmp, now)
                    except Exception as exc:
                        print(f'{name}: backup failed ({type(exc).__name__}: {exc})', file=sys.stderr)
                        failures.append(name)
                if not failures:
                    prune_local_periods(settings, datetime.now().astimezone())
                return 1 if failures else 0
            chosen = dict(stores(settings, args.period)).get(args.storage)
            if chosen is None:
                raise ValueError('storage is not configured')
            items = list_store(chosen, source_project, settings.service)
            source_database = args.source_database or (args.database if source_project == settings.project else None)
            if source_database:
                items = [m for m in items if m.artifact.endswith(backup_suffix(settings, source_database))]
            if args.action == 'list':
                for item in items:
                    print(f'{item.artifact}\t{item.timestamp}\t{item.size}\t{item.sha256}')
                return 0
            if args.file:
                manifest = next((m for m in items if m.artifact == args.file), None)
            else:
                if not args.database:
                    suffix = '.sqlite.gz' if settings.service == 'sqlite' else '.sql.gz'
                    available_names = {match.group(1) for item in items
                                       if (match := re.fullmatch(r'\d{4}-\d{2}-\d{2}\.([A-Za-z0-9_][A-Za-z0-9_-]{0,63})' + re.escape(suffix), item.artifact))}
                    if len(names) > 1 or (not names and len(available_names) > 1):
                        raise ValueError('--database is required when multiple databases are available')
                    if len(names) == 1:
                        items = [item for item in items if item.artifact.endswith(backup_suffix(settings, names[0]))]
                manifest = items[-1] if items else None
            if manifest is None:
                raise ValueError('backup not found')
            suffix = '.sqlite.gz' if settings.service == 'sqlite' else '.sql.gz'
            match = re.fullmatch(r'\d{4}-\d{2}-\d{2}\.([A-Za-z0-9_][A-Za-z0-9_-]{0,63})' + re.escape(suffix), manifest.artifact)
            if not match:
                raise ValueError('invalid scheduled backup filename')
            selected = match.group(1)
            if source_project == settings.project and not args.source_database and names and selected not in names:
                raise ValueError('selected database is not configured')
            if args.source_database and selected != args.source_database:
                raise ValueError('backup database does not match the selected source database')
            artifact = tmp / manifest.artifact
            get(chosen, manifest, artifact)
            if args.action == 'verify':
                print('SHA-256 verified')
                return 0
            if settings.service in ('sqlite', 'postgresql'):
                raise ValueError(f'{settings.service} restoration is manual; use the verified backup file')
            plain = tmp / 'restore.sql'
            with gzip.open(artifact, 'rb') as source, plain.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            if args.action == 'restore-test':
                if not settings.test_image and settings.container:
                    _, _, image = container_info(settings)
                restored = rehearse_mysql(plain, settings.test_image or image, settings.service,
                                         runtime=settings.container_runtime)
                if restored != manifest.object_counts:
                    raise ValueError('restored object counts do not match manifest')
                print(f'object counts match: {restored}')
            else:
                if not args.file:
                    raise ValueError('--file is required for live restore')
                target = args.database or (names[0] if len(names) == 1 else selected)
                if target not in names:
                    raise ValueError('restore target database is not configured')
                if target != selected:
                    raise ValueError('restoring to a different database name is not supported safely')
                prompt = f'Restore {source_project}/{selected} into {settings.project}/{target}? Type yes: '
                if not sys.stdin.isatty() or input(prompt) != 'yes':
                    raise ValueError('restore not confirmed')
                database = MySQLConfig(**{**vars(cfg), 'database': target})
                with plain.open('rb') as source:
                    restore_mysql(database, source)
            return 0
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        print(f'cli-backup: {exc}', file=sys.stderr)
        return 2
