import gzip
import io
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from cli_backup.scheduled import Settings, PostgreSQLConfig, backup_database, connection, capture_postgresql


class PostgreSQLBackupTests(unittest.TestCase):
    def settings(self, root):
        return Settings.from_env({'CLI_BACKUP_COMPOSE_PROJECT_NAME': 'a24-service',
            'CLI_BACKUP_DB_CONTAINER': 'postgres', 'CLI_BACKUP_DB_SERVICE_NAME': 'postgresql',
            'CLI_BACKUP_LOCAL_ROOT': str(root), 'CLI_BACKUP_PERIOD_MONTHLY': 'false'})

    def test_container_credentials_are_discovered_without_exposing_password(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            self.assertEqual(settings.port, 5432)
            with patch('cli_backup.scheduled.container_info', return_value=('db123', {
                'POSTGRES_DB': 'a24', 'POSTGRES_USER': 'dbuser', 'POSTGRES_PASSWORD': 'secret'}, 'pg:16')):
                cfg, names, _ = connection(settings)
            self.assertEqual(names, ['a24'])
            fake = unittest.mock.Mock()
            fake.stdout = io.BytesIO(b'-- valid dump\n')
            fake.wait.return_value = 0
            with patch('cli_backup.scheduled.subprocess.Popen', return_value=fake) as popen:
                backup_database(settings, cfg, 'a24', Path(root), datetime.fromisoformat('2026-11-11T03:00:00+00:00'))
            args, kwargs = popen.call_args
            self.assertNotIn('secret', args[0])
            self.assertEqual(kwargs['env']['PGPASSWORD'], 'secret')
            self.assertIn('pg_dump', args[0])
            path = Path(root) / 'daily/a24-service/postgresql/2026-11-11.a24.sql.gz'
            with gzip.open(path, 'rb') as dump:
                self.assertEqual(dump.read(), b'-- valid dump\n')
            self.assertTrue(Path(str(path) + '.json').is_file())

    def test_failed_dump_is_not_accepted(self):
        cfg = PostgreSQLConfig('db', 'user', 'secret', 'localhost', 5432, '', 'docker', 10)
        proc = unittest.mock.Mock()
        proc.stdout = io.BytesIO(b'partial')
        proc.wait.return_value = 1
        with tempfile.TemporaryDirectory() as root, patch('cli_backup.scheduled.subprocess.Popen', return_value=proc):
            with self.assertRaisesRegex(ValueError, 'failed'):
                capture_postgresql(cfg, 'db', Path(root) / 'out.gz')

if __name__ == '__main__':
    unittest.main()
