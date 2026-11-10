import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cli_backup.rehearsal import rehearse_mysql, rehearse_redis


class RehearsalTests(unittest.TestCase):
    def test_mysql_runs_in_isolated_container_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / 'dump'
            artifact.write_bytes(b'SELECT 1;')
            with patch('cli_backup.rehearsal.run') as run, patch('cli_backup.rehearsal.wait_ready'):
                run.return_value.stdout = '1\t0\t0\t0\t0\n'
                self.assertEqual(rehearse_mysql(artifact, 'test/image:1'), '1\t0\t0\t0\t0')
            calls = [args.args[0] for args in run.call_args_list]
            self.assertIn('--network', calls[0])
            self.assertEqual(calls[0][calls[0].index('--network') + 1], 'none')
            self.assertEqual(calls[-1][:4], ['docker', 'rm', '-f', '-v'])
            self.assertEqual(calls[0][calls[0].index('--name') + 1], calls[-1][-1])

    def test_redis_cleans_up_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / 'dump.rdb'
            artifact.write_bytes(b'RDB')
            with patch('cli_backup.rehearsal.run') as run, patch('cli_backup.rehearsal.subprocess.run') as info, patch('cli_backup.rehearsal.wait_ready', side_effect=TimeoutError):
                info.return_value.stdout = '[]'
                with self.assertRaises(TimeoutError):
                    rehearse_redis(artifact, 'test/redis:1')
            self.assertEqual(run.call_args_list[-1].args[0][:4], ['docker', 'rm', '-f', '-v'])

    def test_rootless_redis_uses_mapped_root_for_private_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / 'dump.rdb'
            artifact.write_bytes(b'RDB')
            with patch('cli_backup.rehearsal.run') as run, patch('cli_backup.rehearsal.subprocess.run') as info, patch('cli_backup.rehearsal.wait_ready'):
                info.return_value.stdout = '["name=rootless"]'
                rehearse_redis(artifact, 'test/redis:1')
            create = run.call_args_list[0].args[0]
            self.assertEqual(create[create.index('--user') + 1], '0:0')
            self.assertEqual(create[create.index('--entrypoint') + 1], 'redis-server')
