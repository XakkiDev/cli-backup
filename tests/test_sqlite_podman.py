import gzip
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from cli_backup.mysql import command
from cli_backup.scheduled import Settings, backup_database, connection
from cli_backup.storage import LocalStorage


class SqliteAndPodmanTests(unittest.TestCase):
    def test_podman_compose_labels_and_command(self):
        env = {'CLI_BACKUP_PROJECT_NAME': 'tg', 'CLI_BACKUP_COMPOSE_PROJECT_NAME': 'tg',
               'CLI_BACKUP_DB_CONTAINER': 'mariadb', 'CLI_BACKUP_CONTAINER_RUNTIME': 'podman',
               'CLI_BACKUP_LOCAL_ROOT': '/tmp/backups'}
        cfg = Settings.from_env(env)
        ps = subprocess.CompletedProcess([], 0, stdout='container-id\n')
        inspect = subprocess.CompletedProcess([], 0, stdout=json.dumps([{'Id': 'container-id',
            'State': {'Running': True}, 'Config': {'Image': 'db:1', 'Labels': {}, 'Env':
            ['MARIADB_DATABASE=db', 'MARIADB_ROOT_PASSWORD=secret']}}]))
        with patch('cli_backup.scheduled.subprocess.run', side_effect=[ps, inspect]) as run:
            mysql, names, _ = connection(cfg)
        self.assertEqual(names, ['db'])
        self.assertEqual(run.call_args_list[0].args[0][0], 'podman')
        self.assertEqual(command(mysql)[:2], ['podman', 'exec'])
        self.assertNotIn('secret', command(mysql))

    def test_oneoff_container_is_rejected(self):
        env = {'CLI_BACKUP_COMPOSE_PROJECT_NAME': 'tg', 'CLI_BACKUP_DB_CONTAINER': 'db',
               'CLI_BACKUP_CONTAINER_RUNTIME': 'podman', 'CLI_BACKUP_LOCAL_ROOT': '/tmp/backups'}
        ps = subprocess.CompletedProcess([], 0, stdout='container-id\n')
        inspect = subprocess.CompletedProcess([], 0, stdout=json.dumps([{'Id': 'container-id',
            'State': {'Running': True}, 'Config': {'Labels': {'com.docker.compose.oneoff': 'True'}}}]))
        with patch('cli_backup.scheduled.subprocess.run', side_effect=[ps, inspect]):
            with self.assertRaisesRegex(ValueError, 'one-off'):
                connection(Settings.from_env(env))

    def test_sqlite_snapshot_uses_wal_and_separate_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'sample.sqlite3'
            db = sqlite3.connect(source)
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE sample (n INTEGER)')
                db.execute('INSERT INTO sample VALUES (42)')
                db.commit()
                env = {'CLI_BACKUP_PROJECT_NAME': 'bp', 'CLI_BACKUP_DB_SERVICE_NAME': 'sqlite',
                       'CLI_BACKUP_DB_PATH': str(source), 'CLI_BACKUP_LOCAL_ROOT': str(root / 'out'),
                       'CLI_BACKUP_PERIOD_MONTHLY': 'false'}
                settings = Settings.from_env(env)
                cfg, names, _ = connection(settings)
                backup_database(settings, cfg, names[0], root, datetime.fromisoformat('2026-11-11T03:00:00+00:00'))
                backup = LocalStorage(root / 'out/daily').list('bp', 'sqlite')
                self.assertEqual([x.artifact for x in backup], ['2026-11-11.sample.sqlite.gz'])
                with gzip.open(root / 'out/daily/bp/sqlite' / backup[0].artifact, 'rb') as f:
                    self.assertEqual(f.read(16), b'SQLite format 3\x00')
            finally:
                db.close()

    def test_sqlite_configuration_rejects_multiple_databases_and_container(self):
        env = {'CLI_BACKUP_PROJECT_NAME': 'bp', 'CLI_BACKUP_DB_SERVICE_NAME': 'sqlite',
               'CLI_BACKUP_DB_PATH': '/tmp/example.sqlite3', 'CLI_BACKUP_LOCAL_ROOT': '/tmp/backups'}
        with self.assertRaises(ValueError):
            Settings.from_env({**env, 'CLI_BACKUP_DB_NAME': 'one,two'})
        with self.assertRaises(ValueError):
            Settings.from_env({**env, 'CLI_BACKUP_DB_CONTAINER': 'db'})


if __name__ == '__main__':
    unittest.main()
