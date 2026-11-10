"""Opt-in end-to-end checks against an already isolated rootless daemon."""
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from cli_backup.mysql import MySQLConfig, capture_mysql, count_mysql_objects, restore_mysql
from cli_backup.rehearsal import rehearse_mysql, wait_ready


@unittest.skipUnless(os.environ.get('CLI_BACKUP_INTEGRATION_ROOTLESS') == '1', 'requires an isolated rootless daemon')
class RootlessIntegration(unittest.TestCase):
    def test_compose_dump_restore_and_rehearsal(self):
        image = os.environ['CLI_BACKUP_INTEGRATION_MYSQL_IMAGE']
        info = subprocess.check_output(['docker', 'info', '--format', '{{json .SecurityOptions}}'], text=True)
        self.assertIn('name=rootless', json.loads(info))
        name = 'cli-backup-rootless-test-' + uuid.uuid4().hex
        env = dict(os.environ, COMPOSE_PROJECT_NAME=name, MYSQL_PWD='test-password')
        with tempfile.TemporaryDirectory(prefix='cli-backup-rootless-it-') as tmp:
            path = Path(tmp)
            (path / 'compose.yaml').write_text(
                'services:\n  db:\n    image: ' + image + '\n    network_mode: none\n'
                '    environment:\n      MYSQL_ROOT_PASSWORD: test-password\n      MYSQL_DATABASE: fixture\n')
            def docker(*args, **kwargs):
                return subprocess.run(['docker', 'compose', *args], cwd=path, env=env, check=True, timeout=120, **kwargs)
            try:
                docker('up', '-d', stdout=subprocess.DEVNULL)
                wait_ready(['docker', 'compose', 'exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-e', 'SELECT 1'],
                           dict(env, COMPOSE_FILE=str(path / 'compose.yaml')))
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', 'fixture', '-e',
                       "CREATE TABLE t (v VARCHAR(20)); INSERT INTO t VALUES ('rootless')")
                dump = path / 'dump.sql'
                config = MySQLConfig('compose', 'fixture', 'root', 'test-password', service='db', compose_dir=str(path))
                os.environ['COMPOSE_PROJECT_NAME'] = name
                with dump.open('wb') as output:
                    capture_mysql(config, output)
                self.assertEqual(count_mysql_objects(config), '1\t0\t0\t0\t0')
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-e', 'CREATE DATABASE restored')
                with dump.open('rb') as source:
                    restore_mysql(MySQLConfig('compose', 'restored', 'root', 'test-password', service='db', compose_dir=str(path)), source)
                result = docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-N', 'restored', '-e', 'SELECT v FROM t', capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), 'rootless')
                self.assertEqual(rehearse_mysql(dump, image), '1\t0\t0\t0\t0')
            finally:
                os.environ.pop('COMPOSE_PROJECT_NAME', None)
                docker('down', '-v', '--remove-orphans', stdout=subprocess.DEVNULL)

    @unittest.skipUnless(os.environ.get('CLI_BACKUP_INTEGRATION_REDIS_IMAGE'), 'set disposable Redis image')
    def test_redis_snapshot_rehearsal(self):
        from unittest import mock
        from cli_backup.rehearsal import rehearse_redis, run
        image = os.environ['CLI_BACKUP_INTEGRATION_REDIS_IMAGE']
        server = os.environ.get('CLI_BACKUP_INTEGRATION_REDIS_SERVER', 'redis-server')
        name = 'cli-backup-rootless-redis-' + uuid.uuid4().hex
        started = False
        try:
            run(['docker', 'run', '-d', '--name', name, '--network', 'none', image,
                 server, '--save', '', '--appendonly', 'no'], stdout=subprocess.DEVNULL)
            started = True
            wait_ready(['docker', 'exec', name, 'redis-cli', 'ping'], dict(os.environ))
            run(['docker', 'exec', name, 'redis-cli', 'SET', 'fixture', 'rootless-value'], stdout=subprocess.DEVNULL)
            run(['docker', 'exec', name, 'redis-cli', 'SAVE'], stdout=subprocess.DEVNULL)
            with tempfile.TemporaryDirectory(prefix='cli-backup-rootless-rdb-') as tmp:
                artifact = Path(tmp) / 'dump.rdb'
                run(['docker', 'cp', name + ':/data/dump.rdb', str(artifact)])
                seen = []
                def recording(args, **kwargs):
                    result = run(args, **kwargs)
                    if args[-1] == 'DBSIZE':
                        value = subprocess.check_output(['docker', 'exec', args[2], 'redis-cli', 'GET', 'fixture'], text=True)
                        seen.append(value.strip())
                    return result
                with mock.patch('cli_backup.rehearsal.run', side_effect=recording):
                    rehearse_redis(artifact, image, server)
                self.assertEqual(seen, ['rootless-value'])
        finally:
            if started:
                run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL)
