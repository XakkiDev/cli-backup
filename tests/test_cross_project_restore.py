import contextlib
import gzip
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_backup.mysql import MySQLConfig
from cli_backup.scheduled import main
from cli_backup.storage import LocalStorage, make_manifest


class CrossProjectRestoreTests(unittest.TestCase):
    def test_source_project_and_target_database_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LocalStorage(root / 'daily')
            artifact = root / 'dump.gz'
            with gzip.open(artifact, 'wb') as stream:
                stream.write(b'SELECT 1;')
            filename = '2026-11-11.student.sql.gz'
            store.put(artifact, make_manifest(artifact, 'sa-prod', 'mariadb', filename, '2026-11-11T00:00:00+00:00'))
            cfg = MySQLConfig('exec', 'student_dev', 'root', 'secret', service='container')
            env = {'CLI_BACKUP_PROJECT_NAME': 'sa-dev', 'CLI_BACKUP_DB_CONTAINER': 'mariadb',
                   'CLI_BACKUP_LOCAL_ROOT': str(root)}
            captured = []
            def fake_restore(database, stream):
                captured.append((database.database, stream.read()))
            args = ['restore', '--storage', 's3', '--file', filename,
                    '--source-project-name', 'sa-prod', '--source-database', 'student']
            with patch.dict(os.environ, env, clear=True), \
                 patch('cli_backup.scheduled.connection', return_value=(cfg, ['student_dev'], 'image')), \
                 patch('cli_backup.scheduled.stores', return_value=[('s3', store)]), \
                 patch('cli_backup.scheduled.restore_mysql', side_effect=fake_restore), \
                 patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='yes') as prompt:
                self.assertEqual(main(args), 0)
            self.assertEqual(captured, [('student_dev', b'SELECT 1;')])
            self.assertIn('sa-prod/student into sa-dev/student_dev', prompt.call_args.args[0])

    def test_confirmation_is_required_even_for_cross_project_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = LocalStorage(root / 'daily'); artifact = root / 'dump.gz'
            with gzip.open(artifact, 'wb') as stream: stream.write(b'SELECT 1;')
            filename = '2026-11-11.student.sql.gz'
            store.put(artifact, make_manifest(artifact, 'sa-prod', 'mariadb', filename, '2026-11-11T00:00:00+00:00'))
            cfg = MySQLConfig('exec', 'student_dev', 'root', 'secret', service='container')
            env = {'CLI_BACKUP_PROJECT_NAME': 'sa-dev', 'CLI_BACKUP_DB_CONTAINER': 'mariadb', 'CLI_BACKUP_LOCAL_ROOT': str(root)}
            with patch.dict(os.environ, env, clear=True), patch('cli_backup.scheduled.connection', return_value=(cfg, ['student_dev'], 'image')), \
                 patch('cli_backup.scheduled.stores', return_value=[('s3', store)]), patch('cli_backup.scheduled.restore_mysql') as restore, \
                 patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['restore', '--storage', 's3', '--file', filename, '--source-project-name', 'sa-prod']), 2)
                restore.assert_not_called()

if __name__ == '__main__':
    unittest.main()
