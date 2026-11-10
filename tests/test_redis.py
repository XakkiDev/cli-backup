import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cli_backup.redis_backup import capture_redis


class RedisTests(unittest.TestCase):
    def test_failed_capture_does_not_run_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dump.rdb'
            with patch('cli_backup.redis_backup.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'redis-cli')) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    capture_redis(path, 'localhost', 6379, 'secret ;')
            self.assertEqual(run.call_count, 1)
            self.assertNotIn('secret ;', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['REDISCLI_AUTH'], 'secret ;')

    def test_snapshot_has_a_configurable_positive_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dump.rdb'
            with patch('cli_backup.redis_backup.subprocess.run') as run:
                def fake_run(argv, **kwargs):
                    if argv[0] == 'redis-cli': path.write_bytes(b'fixture')
                run.side_effect = fake_run
                capture_redis(path, '127.0.0.1', 6379, timeout=17)
                self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [17, 17])
            with self.assertRaises(ValueError):
                capture_redis(path, '127.0.0.1', 6379, timeout=0)
