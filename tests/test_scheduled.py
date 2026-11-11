import gzip
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from cli_backup.mysql import MySQLConfig, command
from cli_backup.scheduled import Settings, backup_database, databases, period_copies
from cli_backup.storage import LocalStorage, make_manifest


class ScheduledTests(unittest.TestCase):
    def settings(self, root, **more):
        env = {'CLI_BACKUP_PROJECT_NAME': 'project', 'CLI_BACKUP_DB_CONTAINER': 'mariadb',
               'CLI_BACKUP_LOCAL_ROOT': str(root), 'CLI_BACKUP_DB_NAME': 'one,two',
               'CLI_BACKUP_PERIOD_WEEKLY': 'true'}
        env.update(more)
        return Settings.from_env(env)

    def test_config_rejects_ambiguity_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'CLI_BACKUP_PROJECT_NAME'):
                self.settings(root, CLI_BACKUP_PROJECT_NAME='')
            for extra in ({'CLI_BACKUP_DB_HOST': 'localhost'}, {'CLI_BACKUP_DB_NAME': 'one,one'},
                          {'CLI_BACKUP_DB_NAME': 'one,'}, {'CLI_BACKUP_PERIOD_WEEKLY': 'yes'},
                          {'CLI_BACKUP_DB_USER': 'user'}, {'CLI_BACKUP_DB_SERVICE_NAME': 'sqlite'}):
                with self.assertRaises(ValueError):
                    self.settings(root, **extra)
            self.assertEqual(databases('one,two'), ['one', 'two'])
            self.assertEqual(self.settings(root).project, 'project')
            settings = self.settings(root, CLI_BACKUP_PROJECT_NAME='', CLI_BACKUP_COMPOSE_PROJECT_NAME='compose')
            self.assertEqual(settings.project, 'compose')

    def test_container_resolution_uses_project_labels_and_root_credentials(self):
        import json
        import subprocess
        from cli_backup.scheduled import connection
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root, CLI_BACKUP_COMPOSE_PROJECT_NAME='isolated', CLI_BACKUP_DB_NAME='')
            ps = subprocess.CompletedProcess([], 0, stdout='container-id\n')
            inspect = subprocess.CompletedProcess([], 0, stdout=json.dumps([{'Id': 'container-id',
                'State': {'Running': True}, 'Config': {'Image': 'db:1', 'Env':
                ['MARIADB_DATABASE=testdb', 'MARIADB_ROOT_PASSWORD=secret']}}]))
            with patch('cli_backup.scheduled.subprocess.run', side_effect=[ps, inspect]) as run:
                cfg, names, image = connection(settings)
            self.assertEqual((names, cfg.user, cfg.password, cfg.service, image),
                             (['testdb'], 'root', 'secret', 'container-id', 'db:1'))
            self.assertIn('label=com.docker.compose.project=isolated', run.call_args_list[0].args[0])
            self.assertNotIn('secret', repr(run.call_args_list))
            with patch('cli_backup.scheduled.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout='one\ntwo\n')):
                with self.assertRaises(ValueError):
                    connection(settings)

    def test_project_file_and_cli_override_global_settings(self):
        import contextlib
        import io
        import os
        from cli_backup.scheduled import main
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.env'
            path.write_text('CLI_BACKUP_PROJECT_NAME=from_file\nCLI_BACKUP_DB_CONTAINER=mariadb\nCLI_BACKUP_LOCAL_ROOT=/tmp/backups\n')
            output = io.StringIO()
            with patch.dict(os.environ, {'CLI_BACKUP_PROJECT_NAME': 'from_global'}, clear=True), contextlib.redirect_stdout(output):
                self.assertEqual(main(['config-check', '--project-env', str(path), '--project-name', 'from_argument']), 0)
            self.assertIn('from_argument', output.getvalue())
            self.assertNotIn('from_global', output.getvalue())

    def test_exec_uses_container_without_secret_in_arguments(self):
        cmd = command(MySQLConfig('exec', 'one', 'root', 'secret', service='abc123'))
        self.assertEqual(cmd[:5], ['docker', 'exec', '-i', '-e', 'MYSQL_PWD'])
        self.assertNotIn('secret', cmd)
        self.assertNotIn('--host', cmd)

    def test_copy_month_first_and_prior_week_falls_back_to_saturday(self):
        # Sunday 2026-05-31 is both month-end and end of week.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(root)
            daily = LocalStorage(root / 'daily')
            for day in ('2026-05-30', '2026-05-31'):
                artifact = root / day
                artifact.write_bytes(day.encode())
                manifest = make_manifest(artifact, 'project', 'mariadb', day + '.one.sql.gz', day + 'T03:00:00+00:00')
                daily.put(artifact, manifest)
            period_copies(settings, 'one', date(2026, 6, 3), root)
            monthly = LocalStorage(root / 'monthly').list('project', 'mariadb')
            weekly = LocalStorage(root / 'weekly').list('project', 'mariadb')
            self.assertEqual(monthly[0].artifact, '2026-05-31.one.sql.gz')
            self.assertEqual(weekly[0].artifact, '2026-05-30.one.sql.gz')
            self.assertEqual(len(daily.list('project', 'mariadb')), 2)
            period_copies(settings, 'one', date(2026, 6, 4), root)
            self.assertEqual(len(LocalStorage(root / 'monthly').list('project', 'mariadb')), 1)
            self.assertEqual(len(LocalStorage(root / 'weekly').list('project', 'mariadb')), 1)

    def test_daily_idempotent_and_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(root, CLI_BACKUP_PERIOD_MONTHLY='false', CLI_BACKUP_PERIOD_WEEKLY='false')
            cfg = MySQLConfig('tcp', 'one', 'u', 'p')
            now = datetime.fromisoformat('2026-06-03T03:00:00+00:00')
            def fake_capture(_cfg, path):
                with gzip.open(path, 'wb') as f:
                    f.write(b'CREATE TABLE demo (id int);')
            with patch('cli_backup.scheduled.capture_gzip', side_effect=fake_capture) as capture, patch('cli_backup.scheduled.count_mysql_objects', return_value='1 0 0 0 0'):
                backup_database(settings, cfg, 'one', root, now)
                backup_database(settings, cfg, 'one', root, now)
                self.assertEqual(capture.call_count, 1)
            path = root / 'daily/project/mariadb/2026-06-03.one.sql.gz'
            with gzip.open(path, 'rb') as f:
                self.assertIn(b'CREATE TABLE', f.read())

    def test_without_daily_creates_due_periods_once_without_daily(self):
        from cli_backup.scheduled import direct_period_backups
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(root, CLI_BACKUP_PERIOD_DAILY='false')
            cfg = MySQLConfig('tcp', 'one', 'u', 'p')
            def fake_capture(_cfg, path):
                with gzip.open(path, 'wb') as f:
                    f.write(b'dump')
            with patch('cli_backup.scheduled.capture_gzip', side_effect=fake_capture) as capture, patch('cli_backup.scheduled.count_mysql_objects', return_value='1 0 0 0 0'):
                direct_period_backups(settings, cfg, 'one', root, datetime.fromisoformat('2026-06-01T03:00:00+00:00'))
                self.assertEqual(capture.call_count, 0)
                direct_period_backups(settings, cfg, 'one', root, datetime.fromisoformat('2026-06-03T03:00:00+00:00'))
                self.assertEqual(capture.call_count, 1)
                direct_period_backups(settings, cfg, 'one', root, datetime.fromisoformat('2026-06-04T03:00:00+00:00'))
                self.assertEqual(capture.call_count, 1)
            self.assertFalse((root / 'daily').exists())
            for period in ('monthly', 'weekly'):
                items = LocalStorage(root / period).list('project', 'mariadb')
                self.assertEqual([m.artifact for m in items], ['2026-06-03.one.sql.gz'])

    def test_local_retention_preserves_current_period_markers(self):
        from cli_backup.scheduled import prune_local_periods
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(root)
            for period, day in [('daily', '2026-06-24'), ('weekly', '2026-06-28'), ('monthly', '2026-06-01')]:
                artifact = root / period
                artifact.write_bytes(b'dump')
                manifest = make_manifest(artifact, 'project', 'mariadb', day + '.one.sql.gz', day + 'T03:00:00+00:00')
                LocalStorage(root / (period + '-store')).put(artifact, manifest)
                (root / period).unlink()
                (root / (period + '-store')).rename(root / period)
            from dataclasses import replace
            settings = replace(settings, local_days='3')
            prune_local_periods(settings, datetime.fromisoformat('2026-07-04T03:00:00+00:00'))
            for period in ('daily', 'weekly', 'monthly'):
                self.assertEqual(len(LocalStorage(root / period).list('project', 'mariadb')), 1)

    def test_s3_period_copies_are_verified_and_idempotent(self):
        import io
        from botocore.exceptions import ClientError
        from cli_backup.storage import S3Storage
        class FakeS3:
            def __init__(self): self.data = {}
            def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
                if Key in self.data:
                    raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
                self.data[Key] = Body.read() if hasattr(Body, 'read') else Body
            def get_object(self, *, Bucket, Key):
                if Key not in self.data:
                    raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
                return {'Body': io.BytesIO(self.data[Key])}
            def head_object(self, *, Bucket, Key):
                if Key not in self.data:
                    raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
                return {'ContentLength': len(self.data[Key])}
            def get_paginator(self, _name): return self
            def paginate(self, *, Bucket, Prefix):
                return [{'Contents': [{'Key': k} for k in self.data if k.startswith(Prefix)]}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(root)
            client = FakeS3()
            for day in ('2026-05-30', '2026-05-31'):
                artifact = root / day
                artifact.write_bytes(day.encode())
                manifest = make_manifest(artifact, 'project', 'mariadb', day + '.one.sql.gz', day + 'T03:00:00+00:00')
                S3Storage(client, 'b', 'daily').put(artifact, manifest)
            def fake_stores(_settings, period): return [('s3', S3Storage(client, 'b', period))]
            with patch('cli_backup.scheduled.stores', side_effect=fake_stores):
                period_copies(settings, 'one', date(2026, 6, 3), root)
                period_copies(settings, 'one', date(2026, 6, 4), root)
            self.assertIn('monthly/project/mariadb/2026-05-31.one.sql.gz', client.data)
            self.assertIn('weekly/project/mariadb/2026-05-30.one.sql.gz', client.data)
            self.assertIn('daily/project/mariadb/2026-05-31.one.sql.gz', client.data)

    def test_no_previous_month_is_reported_without_deleting_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp, CLI_BACKUP_PERIOD_WEEKLY='false')
            with self.assertRaises(ValueError):
                period_copies(settings, 'one', date(2026, 6, 3), Path(tmp))


if __name__ == '__main__':
    unittest.main()
