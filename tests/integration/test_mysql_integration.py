"""Disposable container integration; enabled only with an explicit image."""
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from cli_backup.mysql import MySQLConfig, capture_mysql, count_mysql_objects, restore_mysql
from cli_backup.rehearsal import rehearse_mysql


@unittest.skipUnless(os.environ.get('CLI_BACKUP_INTEGRATION_MYSQL_IMAGE'), 'set explicit disposable test image')
class MysqlIntegration(unittest.TestCase):
    def test_dump_and_restore_disposable_database(self):
        image = os.environ['CLI_BACKUP_INTEGRATION_MYSQL_IMAGE']
        client = os.environ.get('CLI_BACKUP_INTEGRATION_CLIENT', 'mariadb')
        name = 'cli-backup-test-' + uuid.uuid4().hex
        env = dict(os.environ, COMPOSE_PROJECT_NAME=name, MYSQL_PWD='test-password')
        with tempfile.TemporaryDirectory(prefix='cli-backup-it-') as tmp:
            path = Path(tmp)
            (path / 'compose.yaml').write_text(
                'services:\n  db:\n    image: ' + image + '\n    ports: ["127.0.0.1::3306"]\n'
                '    environment:\n      MYSQL_ROOT_PASSWORD: test-password\n'
                '      MYSQL_DATABASE: fixture\n')
            (path / 'env.fixture').write_text('COMPOSE_PROJECT_NAME=' + name + '\nCOMPOSE_FILE=compose.yaml\n')
            prefix = ['docker', 'compose']
            def docker(*args, **kwargs):
                return subprocess.run(prefix + list(args), cwd=path, env=env, check=True, timeout=90, **kwargs)
            try:
                docker('up', '-d', stdout=subprocess.DEVNULL)
                from cli_backup.rehearsal import wait_ready
                wait_ready(prefix + ['exec', '-T', '-e', 'MYSQL_PWD', 'db', client, '-u', 'root', '-e', 'SELECT 1'], dict(env, COMPOSE_FILE=str(path / 'compose.yaml')))
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', client, '-u', 'root', 'fixture', '-e',
                       "CREATE TABLE t (v VARCHAR(10)); INSERT INTO t VALUES ('data'); "
                       "CREATE VIEW view_t AS SELECT v FROM t; "
                       "CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW SET NEW.v=NEW.v; "
                       "CREATE PROCEDURE proc_test() SELECT 1; "
                       "CREATE EVENT event_test ON SCHEDULE EVERY 1 DAY DO SET @fixture=1")
                artifact = path / 'dump.sql'
                cfg = MySQLConfig('compose', 'fixture', 'root', 'test-password', service='db', compose_dir=str(path), compose_env_files=('env.fixture',), client=client)
                with artifact.open('wb') as output:
                    with mock.patch.dict(os.environ, {'COMPOSE_PROJECT_NAME': name}):
                        capture_mysql(cfg, output)
                self.assertIn(b'INSERT INTO', artifact.read_bytes())
                container_dump = path / 'container.sql'
                remote = MySQLConfig('container', 'fixture', 'root', 'test-password', host='db',
                                     network=name + '_default', image=image, client=client)
                with container_dump.open('wb') as output:
                    capture_mysql(remote, output)
                self.assertIn(b'INSERT INTO', container_dump.read_bytes())
                if os.environ.get('CLI_BACKUP_INTEGRATION_TCP_CLIENT'):
                    binding = docker('port', 'db', '3306', capture_output=True, text=True).stdout.strip()
                    tcp = MySQLConfig('tcp', 'fixture', 'root', 'test-password', host='127.0.0.1',
                                      port=int(binding.rsplit(':', 1)[1]), client=client)
                    direct_dump = path / 'direct.sql'
                    with direct_dump.open('wb') as output:
                        capture_mysql(tcp, output)
                    self.assertIn(b'INSERT INTO', direct_dump.read_bytes())
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', client, '-u', 'root', '-e', 'CREATE DATABASE restored')
                target = MySQLConfig('compose', 'restored', 'root', 'test-password', service='db', compose_dir=str(path), compose_env_files=('env.fixture',), client=client)
                with artifact.open('rb') as source, mock.patch.dict(os.environ, {'COMPOSE_PROJECT_NAME': name}):
                    restore_mysql(target, source)
                result = docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', client, '-u', 'root', '-N', 'restored', '-e', 'SELECT v FROM t', capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), 'data')
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', client, '-u', 'root', 'fixture', '-e', "UPDATE t SET v='changed'")
                view = docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', client, '-u', 'root', '-N', 'restored', '-e', 'SELECT v FROM view_t', capture_output=True, text=True)
                self.assertEqual(view.stdout.strip(), 'data')
                with mock.patch.dict(os.environ, {'COMPOSE_PROJECT_NAME': name}):
                    self.assertEqual(count_mysql_objects(cfg), '1\t1\t1\t1\t1')
                self.assertEqual(rehearse_mysql(artifact, image, client), '1\t1\t1\t1\t1')
            finally:
                docker('down', '-v', '--remove-orphans', stdout=subprocess.DEVNULL)
