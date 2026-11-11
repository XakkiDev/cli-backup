import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from cli_backup.cpu_priority import lower_cpu_priority, validate_cpu_nice
from cli_backup.mysql import MySQLConfig, command
from cli_backup.scheduled import Settings


class CpuPriorityTests(unittest.TestCase):
    def test_validation_and_inherited_priority(self):
        for value in ('-1', '20', 'secret'):
            with self.assertRaises(ValueError):
                validate_cpu_nice(value)
        with patch('cli_backup.cpu_priority.os.getpriority', return_value=0), patch('cli_backup.cpu_priority.os.setpriority') as setpriority:
            lower_cpu_priority(10)
            setpriority.assert_called_once_with(os.PRIO_PROCESS, 0, 10)
        with patch('cli_backup.cpu_priority.os.getpriority', return_value=15), patch('cli_backup.cpu_priority.os.setpriority') as setpriority:
            lower_cpu_priority(10)
            setpriority.assert_not_called()

    def test_actual_child_priority(self):
        result = subprocess.run([sys.executable, '-c',
            'import os; from cli_backup.cpu_priority import lower_cpu_priority; '
            'lower_cpu_priority(10); print(os.getpriority(os.PRIO_PROCESS, 0))'],
            check=True, capture_output=True, text=True)
        self.assertGreaterEqual(int(result.stdout), 10)

    def test_container_dump_is_nice_but_count_and_restore_are_not(self):
        for mode in ('exec', 'compose', 'container'):
            cfg = MySQLConfig(mode, 'db', 'root', 'password', service='mariadb', network='net', image='db:1', cpu_nice=7)
            dump = command(cfg)
            self.assertEqual(dump[dump.index('nice'):][:4], ['nice', '-n', '7', 'mariadb-dump'])
            self.assertNotIn('nice', command(cfg, restore=True))
            self.assertNotIn('password', dump)
        self.assertNotIn('nice', command(MySQLConfig('tcp', 'db', 'root', 'password')))
        self.assertNotIn('nice', command(MySQLConfig('exec', 'db', 'root', 'password', cpu_nice=0)))

    def test_scheduled_configuration_defaults_and_overrides(self):
        env = {'CLI_BACKUP_PROJECT_NAME': 'app', 'CLI_BACKUP_DB_CONTAINER': 'db', 'CLI_BACKUP_LOCAL_ROOT': '/tmp/backups'}
        self.assertEqual(Settings.from_env(env).cpu_nice, 10)
        self.assertEqual(Settings.from_env({**env, 'CLI_BACKUP_CPU_NICE': '4'}).cpu_nice, 4)
        with self.assertRaises(ValueError):
            Settings.from_env({**env, 'CLI_BACKUP_CPU_NICE': '20'})


if __name__ == '__main__':
    unittest.main()
