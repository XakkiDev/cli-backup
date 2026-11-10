"""Exercise legacy mysql-bkup artifacts with disposable databases and storage."""
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from cli_backup.rehearsal import wait_ready


@unittest.skipUnless(os.environ.get('CLI_BACKUP_INTEGRATION_LEGACY_IMAGE'), 'set explicit legacy image')
class LegacyIntegration(unittest.TestCase):
    def test_legacy_local_dump_list_and_restore(self):
        db_image = os.environ['CLI_BACKUP_INTEGRATION_MYSQL_IMAGE']
        backup_image = os.environ['CLI_BACKUP_INTEGRATION_LEGACY_IMAGE']
        name = 'cli-backup-legacy-test-' + uuid.uuid4().hex
        env = dict(os.environ, COMPOSE_PROJECT_NAME=name, MYSQL_PWD='fixture-password')
        with tempfile.TemporaryDirectory(prefix='cli-backup-legacy-it-') as tmp:
            path = Path(tmp)
            (path / 'backup').mkdir()
            (path / 'compose.yaml').write_text(
                'services:\n  db:\n    image: ' + db_image + '\n'
                '    environment:\n      MARIADB_ROOT_PASSWORD: fixture-password\n'
                '      MARIADB_DATABASE: fixture\n'
                '  legacy:\n    image: ' + backup_image + '\n    profiles: [backup]\n'
                '    environment:\n      DB_HOST: db\n      DB_PORT: "3306"\n'
                '      DB_USERNAME: root\n      DB_PASSWORD: fixture-password\n      DB_NAME: fixture\n'
                '      STORAGE: local\n      STORAGE_PATH: /backup\n      BACKUP_INDEX: "true"\n'
                '    volumes:\n      - ./backup:/backup\n')
            prefix = ['docker', 'compose']
            def docker(*args, **kwargs):
                return subprocess.run(prefix + list(args), cwd=path, env=env, check=True, timeout=90, **kwargs)
            try:
                docker('up', '-d', 'db', stdout=subprocess.DEVNULL)
                wait_ready(prefix + ['exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-e', 'SELECT 1'],
                           dict(env, COMPOSE_FILE=str(path / 'compose.yaml')))
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', 'fixture', '-e',
                       "CREATE TABLE t (v VARCHAR(10)); INSERT INTO t VALUES ('legacy')")
                common = ('run', '--rm', '--no-deps', '-T', '--entrypoint', '/usr/local/bin/mysql-bkup', 'legacy')
                docker(*common, 'backup', stdout=subprocess.DEVNULL)
                listed = docker(*common, 'list', '--storage', 'local', '--json', capture_output=True, text=True)
                entries = json.loads(listed.stdout)
                self.assertEqual(len(entries), 1)
                filename = entries[0]['file']
                self.assertTrue(filename.endswith('.sql.gz'))
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-e', 'CREATE DATABASE restored')
                docker('run', '--rm', '--no-deps', '-T', '-e', 'DB_NAME=restored',
                       '--entrypoint', '/usr/local/bin/mysql-bkup', 'legacy', 'restore', '--storage', 'local', '-f', filename,
                       stdout=subprocess.DEVNULL)
                result = docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-N', 'restored',
                                '-e', 'SELECT v FROM t', capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), 'legacy')
            finally:
                docker('down', '-v', '--remove-orphans', stdout=subprocess.DEVNULL)
