"""Command-line entry point for backup and recovery."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, read_project_env, resolve
from .cpu_priority import lower_cpu_priority, validate_cpu_nice
from .files import capture_directory, extract_archive
from .mysql import MySQLConfig, capture_mysql, count_mysql_objects, restore_mysql
from .redis_backup import capture_redis
from .rehearsal import rehearse_mysql, rehearse_redis
from .storage import LocalStorage, S3Storage, local_get, make_manifest, prune_local, s3_get, s3_list, write_private_copy


def parser_build() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='cli-backup')
    parser.add_argument('--project-env', type=Path)
    parser.add_argument('--project')
    parser.add_argument('--local-root')
    parser.add_argument('--temp-root')
    parser.add_argument('--s3-bucket')
    parser.add_argument('--s3-endpoint')
    parser.add_argument('--s3-region')
    parser.add_argument('--s3-prefix')
    parser.add_argument('--compose-env-file', action='append', default=[])
    for option in ('db-mode', 'db-name', 'db-user', 'db-host', 'db-port', 'db-service',
                   'db-network', 'db-image', 'db-client', 'compose-dir', 'redis-host',
                   'redis-port', 'redis-timeout', 'local-days', 'cpu-nice', 'test-mysql-image', 'test-redis-image', 'test-redis-server', 'restore-timeout'):
        parser.add_argument('--' + option)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('scheduled', help='scheduled database backups; run cli-backup scheduled --help')
    sub.add_parser('config-check')
    publish = sub.add_parser('publish-local')
    publish.add_argument('source', choices=['files', 'mysql', 'redis'])
    publish.add_argument('--file', required=True)
    backup = sub.add_parser('backup')
    backup.add_argument('source', choices=['files', 'mysql', 'redis'])
    backup.add_argument('--directory', type=Path)
    backup.add_argument('--exclude', action='append', default=[])
    for name in ('list', 'verify', 'restore', 'restore-test', 'download'):
        item = sub.add_parser(name)
        item.add_argument('source', choices=['files', 'mysql', 'redis'])
        item.add_argument('--storage', choices=['local', 's3'], default='local')
        if name != 'list':
            item.add_argument('--file', required=(name != 'restore-test'))
        if name == 'download':
            item.add_argument('--destination', type=Path, required=True)
        if name == 'restore':
            item.add_argument('--destination', type=Path)
            item.add_argument('--target', help='explicit database name')
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'scheduled':
        from .scheduled import main as scheduled_main
        return scheduled_main(argv[1:])
    args = parser_build().parse_args(argv)
    try:
        project_env = read_project_env(args.project_env) if args.project_env else {}
        settings = vars(args)
        config = load_config(os.environ, project_env, settings)
        def setting(name: str, default: str = '') -> str:
            return resolve(name, settings.get(name), project_env, os.environ) or default
        cpu_nice = validate_cpu_nice(setting('cpu_nice', '10'))
        if args.command == 'backup':
            lower_cpu_priority(cpu_nice)
        if args.command == 'config-check':
            print(f'Configuration valid for project {config.project}')
            return 0
        stores = {}
        if config.local_root:
            stores['local'] = LocalStorage(Path(config.local_root).expanduser())
        if config.s3_bucket and (args.command in ('backup', 'publish-local') or args.storage == 's3'):
            def s3_factory():
                import boto3
                from botocore.config import Config
                client = boto3.client('s3', endpoint_url=setting('s3_endpoint') or None,
                    region_name=setting('s3_region') or None, config=Config(s3={'addressing_style': 'path'}),
                    aws_access_key_id=setting('s3_key') or None,
                    aws_secret_access_key=setting('s3_secret') or None)
                return S3Storage(client, config.s3_bucket, setting('s3_prefix'))
            stores['s3'] = s3_factory
        def mysql_config(database: str | None = None) -> MySQLConfig:
            return MySQLConfig(mode=setting('db_mode', 'compose'), database=database or setting('db_name'),
                user=setting('db_user'), password=setting('db_password'), host=setting('db_host', '127.0.0.1'),
                port=int(setting('db_port', '3306')), service=setting('db_service', 'mariadb'),
                network=setting('db_network'), image=setting('db_image'), compose_dir=setting('compose_dir', '.'),
                client=setting('db_client', 'mariadb'), compose_env_files=tuple(args.compose_env_file), cpu_nice=cpu_nice)
        if args.command == 'publish-local':
            if 'local' not in stores or 's3' not in stores:
                raise ValueError('local and S3 storage are required')
            local = stores['local']
            manifest = next((item for item in local.list(config.project, args.source) if item.artifact == args.file), None)
            if manifest is None or not local.verify(manifest):
                raise ValueError('local backup missing or checksum mismatch')
            remote = stores['s3']()
            if not remote.verify(manifest):
                raise ValueError('remote backup checksum mismatch')
            remote.client.put_object(Bucket=remote.bucket, Key=remote.key(manifest) + '.json',
                                     Body=manifest.encode(), IfNoneMatch='*')
            print(f's3: {manifest.artifact}')
            return 0
        temp_root = setting('temp_root')
        with tempfile.TemporaryDirectory(prefix='cli-backup-', dir=str(Path(temp_root).expanduser().absolute()) if temp_root else None) as tmp:
            artifact = Path(tmp) / 'artifact'
            if args.command == 'backup':
                retention = setting('local_days')
                if retention and int(retention) < 1:
                    raise ValueError('local retention days must be positive')
                object_counts = None
                if args.source == 'files':
                    if not args.directory:
                        raise ValueError('--directory is required for file backup')
                    capture_directory(args.directory, args.exclude, artifact)
                elif args.source == 'mysql':
                    database = mysql_config()
                    object_counts = count_mysql_objects(database)
                    with artifact.open('xb') as output:
                        capture_mysql(database, output)
                    if count_mysql_objects(database) != object_counts:
                        raise ValueError('source schema object counts changed during dump')
                else:
                    capture_redis(artifact, setting('redis_host', '127.0.0.1'), int(setting('redis_port', '6379')), setting('redis_password') or None, timeout=int(setting('redis_timeout', '14400')))
                if artifact.stat().st_size == 0:
                    raise ValueError('source produced an empty backup')
                now = datetime.now(timezone.utc)
                name = f'{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.bin'
                manifest = make_manifest(artifact, config.project, args.source, name, now.isoformat(), object_counts)
                failures = []
                for label, store in stores.items():
                    try:
                        destination = store() if label == 's3' else store
                        destination.put(artifact, manifest)
                        print(f'{label}: {name}')
                    except Exception as exc:
                        print(f'{label}: backup failed ({type(exc).__name__})', file=sys.stderr)
                        failures.append(label)
                if failures:
                    return 1
                if retention and 'local' in stores:
                    removed = prune_local(stores['local'], config.project, args.source, int(retention), now)
                    print(f'local: removed {removed} expired backups')
                return 0
            if args.storage not in stores:
                raise ValueError(f'{args.storage} storage is not configured')
            store = stores[args.storage]
            if args.storage == 's3':
                store = store()
            manifests = store.list(config.project, args.source) if args.storage == 'local' else s3_list(store, config.project, args.source)
            if args.command == 'list':
                for item in manifests:
                    print(f'{item.artifact}\t{item.timestamp}\t{item.size}\t{item.sha256}')
                return 0
            manifest = (next((item for item in manifests if item.artifact == args.file), None)
                        if args.file else (manifests[-1] if manifests else None))
            if manifest is None:
                raise ValueError('backup not found')
            with artifact.open('xb') as output:
                (local_get if args.storage == 'local' else s3_get)(store, manifest, output)
            if args.command == 'verify':
                print('SHA-256 verified against downloaded bytes' if args.storage == 's3' else 'SHA-256 verified')
            elif args.command == 'download':
                with artifact.open('rb') as source:
                    write_private_copy(source, args.destination)
            elif args.command == 'restore-test':
                if args.source == 'mysql':
                    restored = rehearse_mysql(artifact, setting('test_mysql_image'), setting('db_client', 'mariadb'), int(setting('restore_timeout', '14400')))
                    expected = manifest.object_counts or count_mysql_objects(mysql_config())
                    if expected != restored:
                        raise ValueError(f'object counts differ: expected={expected}, restored={restored}')
                    print(f'object counts match: {restored}')
                elif args.source == 'redis':
                    rehearse_redis(artifact, setting('test_redis_image'), setting('test_redis_server', 'redis-server'))
                else:
                    raise ValueError('restore-test supports mysql and redis')
            elif args.source == 'files':
                if not args.destination:
                    raise ValueError('--destination is required')
                extract_archive(artifact, args.destination)
                print('Extracted; original ownership is not restored', file=sys.stderr)
            elif args.source == 'mysql':
                if not args.target:
                    raise ValueError('--target is required')
                if not sys.stdin.isatty() or input(f'Restore into database {args.target}? Type yes: ') != 'yes':
                    raise ValueError('restore not confirmed')
                with artifact.open('rb') as source:
                    restore_mysql(mysql_config(args.target), source)
            else:
                raise ValueError('live Redis restore is manual; use a verified RDB snapshot')
            return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'cli-backup: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'cli-backup: operation failed ({type(exc).__name__})', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
