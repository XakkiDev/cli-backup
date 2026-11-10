import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from cli_backup.redis_backup import capture_redis
from cli_backup.rehearsal import rehearse_redis, run, wait_ready


@unittest.skipUnless(os.environ.get('CLI_BACKUP_INTEGRATION_REDIS_IMAGE'), 'set explicit disposable test image')
class RedisIntegration(unittest.TestCase):
    def test_snapshot_and_restore_fixture(self):
        image = os.environ['CLI_BACKUP_INTEGRATION_REDIS_IMAGE']
        name = 'cli-backup-redis-test-' + uuid.uuid4().hex
        started = False
        try:
            run(['docker', 'run', '-d', '--name', name, '-p', '127.0.0.1::6379', image,
                 'keydb-server', '--save', '', '--appendonly', 'no'], stdout=subprocess.DEVNULL)
            started = True
            wait_ready(['docker', 'exec', name, 'redis-cli', 'ping'], dict(os.environ))
            run(['docker', 'exec', name, 'redis-cli', 'SET', 'fixture', 'example'], stdout=subprocess.DEVNULL)
            port = subprocess.check_output(['docker', 'port', name, '6379/tcp'], text=True).strip().rsplit(':', 1)[1]
            with tempfile.TemporaryDirectory(prefix='cli-backup-redis-it-') as tmp:
                artifact = Path(tmp) / 'snapshot.rdb'
                capture_redis(artifact, '127.0.0.1', int(port))
                seen = []
                def recording_run(args, **kwargs):
                    result = run(args, **kwargs)
                    if args[-1] == 'DBSIZE':
                        data = subprocess.check_output(['docker', 'exec', args[2], 'redis-cli', 'GET', 'fixture'], text=True)
                        seen.append(data.strip())
                    return result
                with mock.patch('cli_backup.rehearsal.run', side_effect=recording_run):
                    rehearse_redis(artifact, image, 'keydb-server')
                self.assertEqual(seen, ['example'])
        finally:
            if started:
                run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL)

    def test_wrong_password_does_not_publish_and_correct_password_succeeds(self):
        import contextlib
        import io
        from cli_backup.cli import main
        from cli_backup.storage import LocalStorage
        image = os.environ['CLI_BACKUP_INTEGRATION_REDIS_IMAGE']
        name = 'cli-backup-redis-auth-' + uuid.uuid4().hex
        fixture_password = 'fixture-only-password'
        env = dict(os.environ, REDISCLI_AUTH=fixture_password)
        started = False
        try:
            run(['docker', 'run', '-d', '--name', name, '-p', '127.0.0.1::6379', image,
                 'keydb-server', '--save', '', '--appendonly', 'no', '--requirepass', fixture_password], stdout=subprocess.DEVNULL)
            started = True
            wait_ready(['docker', 'exec', '-e', 'REDISCLI_AUTH', name, 'redis-cli', 'ping'], env)
            port = subprocess.check_output(['docker', 'port', name, '6379/tcp'], text=True).strip().rsplit(':', 1)[1]
            with tempfile.TemporaryDirectory(prefix='cli-backup-auth-it-') as tmp:
                settings = {'CLI_BACKUP_PROJECT': 'fixture', 'CLI_BACKUP_LOCAL_ROOT': tmp,
                            'CLI_BACKUP_REDIS_HOST': '127.0.0.1', 'CLI_BACKUP_REDIS_PORT': port,
                            'CLI_BACKUP_REDIS_PASSWORD': 'wrong-password', 'CLI_BACKUP_REDIS_TIMEOUT': '15'}
                with mock.patch.dict(os.environ, settings, clear=False), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(main(['backup', 'redis']), 0)
                    self.assertEqual(LocalStorage(Path(tmp)).list('fixture', 'redis'), [])
                    os.environ['CLI_BACKUP_REDIS_PASSWORD'] = fixture_password
                    self.assertEqual(main(['backup', 'redis']), 0)
                self.assertEqual(len(LocalStorage(Path(tmp)).list('fixture', 'redis')), 1)
        finally:
            if started:
                run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL)
