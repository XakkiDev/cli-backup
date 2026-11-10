import os
import contextlib
import io
from unittest import mock
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from cli_backup.storage import S3Storage, make_manifest, s3_list, s3_get


@unittest.skipUnless(os.environ.get('CLI_BACKUP_INTEGRATION_S3_IMAGE'), 'set disposable S3 image')
class S3Integration(unittest.TestCase):
    def test_upload_list_download_and_tamper(self):
        import boto3
        image = os.environ['CLI_BACKUP_INTEGRATION_S3_IMAGE']
        name = 'cli-backup-s3-test-' + uuid.uuid4().hex
        run = lambda *args, **kwargs: subprocess.run(list(args), check=True, timeout=90, **kwargs)
        started = False
        try:
            run('docker', 'run', '-d', '--name', name, '-p', '127.0.0.1::9000', '--tmpfs', '/data:size=128m',
                '-e', 'AWS_ACCESS_KEY_ID=disposable-test-access', '-e', 'AWS_SECRET_ACCESS_KEY=disposable-test-secret-change-me',
                image, 'mini', '-dir=/data', '-s3.port=9000', '-master.volumeSizeLimitMB=8', '-volume.max=16', stdout=subprocess.DEVNULL)
            started = True
            port = subprocess.check_output(['docker', 'port', name, '9000/tcp'], text=True).strip().rsplit(':', 1)[1]
            client = boto3.client('s3', endpoint_url='http://127.0.0.1:' + port, region_name='us-east-1',
                                  aws_access_key_id='disposable-test-access', aws_secret_access_key='disposable-test-secret-change-me')
            for attempt in range(30):
                try:
                    client.list_buckets()
                    break
                except Exception:
                    if attempt == 29:
                        raise
                    time.sleep(1)
            client.create_bucket(Bucket='disposable-backup-test')
            with tempfile.TemporaryDirectory(prefix='cli-backup-s3-it-') as tmp:
                artifact = Path(tmp) / 'data'
                artifact.write_bytes(b's3 fixture')
                manifest = make_manifest(artifact, 'project', 'mysql', 'fixture.bin', '2026-11-10T00:00:00Z')
                from cli_backup.cli import main
                source_dir = Path(tmp) / 'source'
                source_dir.mkdir()
                temporary = Path(tmp) / 'temp'
                temporary.mkdir()
                (source_dir / 'file').write_text('fixture')
                cli_env = {'CLI_BACKUP_PROJECT': 'cli-project', 'CLI_BACKUP_LOCAL_ROOT': str(Path(tmp) / 'local'),
                           'CLI_BACKUP_TEMP_ROOT': str(temporary),
                           'CLI_BACKUP_S3_BUCKET': 'disposable-backup-test', 'CLI_BACKUP_S3_ENDPOINT': 'http://127.0.0.1:' + port,
                           'CLI_BACKUP_S3_REGION': 'us-east-1', 'CLI_BACKUP_S3_KEY': 'disposable-test-access',
                           'CLI_BACKUP_S3_SECRET': 'disposable-test-secret-change-me', 'NO_PROXY': '127.0.0.1'}
                with mock.patch.dict(os.environ, cli_env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['backup', 'files', '--directory', str(source_dir)]), 0)
                    self.assertEqual(main(['list', 'files', '--storage', 's3']), 0)
                self.assertTrue(list((Path(tmp) / 'local/cli-project/files').glob('*.json')))
                self.assertEqual(list(temporary.iterdir()), [])
                store = S3Storage(client, 'disposable-backup-test')
                multipart = Path(tmp) / 'multipart'
                multipart.write_bytes(b'x' * (6 * 1024**2))
                multi_manifest = make_manifest(multipart, 'project', 'mysql', 'multipart.bin', '2026-11-10T00:00:00Z')
                with mock.patch('cli_backup.storage.MULTIPART_THRESHOLD', 5 * 1024**2), mock.patch('cli_backup.storage.MULTIPART_PART_SIZE', 5 * 1024**2):
                    store.put(multipart, multi_manifest)
                    self.assertTrue(store.verify(multi_manifest))
                    with self.assertRaises(Exception):
                        store.put(multipart, multi_manifest)
                self.assertTrue(store.verify(multi_manifest))
                store.put(artifact, manifest)
                self.assertCountEqual(s3_list(store, 'project', 'mysql'), [manifest, multi_manifest])
                with self.assertRaises(Exception):
                    store.put(artifact, manifest)
                self.assertTrue(store.verify(manifest))
                with (Path(tmp) / 'download').open('wb') as out:
                    s3_get(store, manifest, out)
                self.assertEqual((Path(tmp) / 'download').read_bytes(), b's3 fixture')
                client.put_object(Bucket='disposable-backup-test', Key=store.key(manifest), Body=b'tampered')
                self.assertFalse(store.verify(manifest))
                self.assertEqual(s3_list(store, 'project', 'mysql'), [multi_manifest])
        finally:
            if started:
                run('docker', 'rm', '-f', name, stdout=subprocess.DEVNULL)
