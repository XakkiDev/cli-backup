import io
import unittest
from unittest.mock import patch
from cli_backup.mysql import MySQLConfig, command, capture_mysql


class MysqlTests(unittest.TestCase):
    def test_compose_secret_only_in_env(self):
        cfg = MySQLConfig('compose', 'db', 'user', 's p;$', service='mariadb')
        with patch('cli_backup.mysql.subprocess.run') as run:
            capture_mysql(cfg, io.BytesIO())
        args, kwargs = run.call_args
        self.assertNotIn(cfg.password, args[0])
        self.assertEqual(kwargs['env']['MYSQL_PWD'], cfg.password)
        self.assertEqual(args[0][:4], ['docker', 'compose', 'exec', '-T'])

    def test_compose_env_files_keep_project_selection(self):
        cfg = MySQLConfig('compose', 'db', 'u', 'p', compose_env_files=('.env', '.env.prod'))
        self.assertEqual(command(cfg)[:6], ['docker', 'compose', '--env-file', '.env', '--env-file', '.env.prod'])

    def test_modes_and_invalid_identifiers(self):
        self.assertIn('--host', command(MySQLConfig('tcp', 'db', 'u', 'p')))
        self.assertIn('--network', command(MySQLConfig('container', 'db', 'u', 'p', network='n', image='i')))
        with self.assertRaises(ValueError):
            command(MySQLConfig('compose', '--bad', 'u', 'p'))
