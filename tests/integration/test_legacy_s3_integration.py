"""Exercise legacy S3 backup compatibility on disposable resources only."""
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from cli_backup.rehearsal import wait_ready


@unittest.skipUnless(all(os.environ.get(k) for k in ('CLI_BACKUP_INTEGRATION_LEGACY_IMAGE',
                     'CLI_BACKUP_INTEGRATION_MYSQL_IMAGE', 'CLI_BACKUP_INTEGRATION_S3_IMAGE')),
                     'set disposable legacy, database and S3 images')
class LegacyS3Integration(unittest.TestCase):
    def test_old_s3_backup_list_verify_and_restore(self):
        import boto3
        name = 'cli-backup-legacy-s3-' + uuid.uuid4().hex
        env = dict(os.environ, COMPOSE_PROJECT_NAME=name, MYSQL_PWD='fixture-password')
        db = os.environ['CLI_BACKUP_INTEGRATION_MYSQL_IMAGE']
        legacy = os.environ['CLI_BACKUP_INTEGRATION_LEGACY_IMAGE']
        s3 = os.environ['CLI_BACKUP_INTEGRATION_S3_IMAGE']
        with tempfile.TemporaryDirectory(prefix='cli-backup-legacy-s3-it-') as tmp:
            path = Path(tmp)
            (path / 'compose.yaml').write_text(
                'services:\n  db:\n    image: ' + db + '\n'
                '    environment:\n      MYSQL_ROOT_PASSWORD: fixture-password\n      MYSQL_DATABASE: fixture\n'
                '  s3:\n    image: ' + s3 + '\n'
                '    command: ["mini", "-dir=/data", "-s3.port=9000", "-master.volumeSizeLimitMB=8", "-volume.max=16"]\n'
                '    ports: ["127.0.0.1::9000"]\n    tmpfs: [/data:size=128m]\n'
                '    environment:\n      AWS_ACCESS_KEY_ID: fixture-access\n      AWS_SECRET_ACCESS_KEY: fixture-secret\n'
                '  legacy:\n    image: ' + legacy + '\n    profiles: [backup]\n'
                '    environment:\n      DB_HOST: db\n      DB_PORT: "3306"\n'
                '      DB_USERNAME: root\n      DB_PASSWORD: fixture-password\n      DB_NAME: fixture\n'
                '      STORAGE: s3\n      AWS_S3_ENDPOINT: http://s3:9000\n'
                '      AWS_S3_BUCKET_NAME: disposable-legacy-backup\n      AWS_ACCESS_KEY: fixture-access\n'
                '      AWS_SECRET_KEY: fixture-secret\n      AWS_REGION: us-east-1\n'
                '      AWS_FORCE_PATH_STYLE: "true"\n      BACKUP_INDEX: "true"\n')
            def docker(*args, **kwargs):
                return subprocess.run(['docker', 'compose', *args], cwd=path, env=env,
                                      check=True, timeout=120, **kwargs)
            try:
                docker('up', '-d', 'db', 's3', stdout=subprocess.DEVNULL)
                wait_ready(['docker', 'compose', 'exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-e', 'SELECT 1'],
                           dict(env, COMPOSE_FILE=str(path / 'compose.yaml')))
                port = docker('port', 's3', '9000', capture_output=True, text=True).stdout.strip().rsplit(':', 1)[1]
                client = boto3.client('s3', endpoint_url='http://127.0.0.1:' + port, region_name='us-east-1',
                                      aws_access_key_id='fixture-access', aws_secret_access_key='fixture-secret')
                for attempt in range(30):
                    try:
                        client.list_buckets()
                        break
                    except Exception:
                        if attempt == 29: raise
                        time.sleep(1)
                client.create_bucket(Bucket='disposable-legacy-backup')
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', 'fixture', '-e',
                       "CREATE TABLE t (v VARCHAR(10)); INSERT INTO t VALUES ('old-s3')")
                common = ('run', '--rm', '--no-deps', '-T', '--entrypoint', '/usr/local/bin/mysql-bkup', 'legacy')
                docker(*common, 'backup', '--storage', 's3', '--path', 'mariadb/daily', stdout=subprocess.DEVNULL)
                listed = docker(*common, 'list', '--storage', 's3', '--path', 'mariadb/daily', '--json', capture_output=True, text=True)
                entries = json.loads(listed.stdout)
                self.assertEqual(len(entries), 1)
                filename = entries[0]['file']
                docker(*common, 'verify', '--storage', 's3', '--path', 'mariadb/daily', '-f', filename, stdout=subprocess.DEVNULL)
                docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-e', 'CREATE DATABASE restored')
                docker('run', '--rm', '--no-deps', '-T', '-e', 'DB_NAME=restored', '--entrypoint', '/usr/local/bin/mysql-bkup',
                       'legacy', 'restore', '--storage', 's3', '--path', 'mariadb/daily', '-f', filename, stdout=subprocess.DEVNULL)
                result = docker('exec', '-T', '-e', 'MYSQL_PWD', 'db', 'mariadb', '-u', 'root', '-N', 'restored',
                                '-e', 'SELECT v FROM t', capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), 'old-s3')
            finally:
                docker('down', '-v', '--remove-orphans', stdout=subprocess.DEVNULL)
