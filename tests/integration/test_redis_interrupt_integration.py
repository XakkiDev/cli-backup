"""Exercise a truncated real redis-cli replication transfer on loopback."""
import contextlib
import io
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_backup.cli import main
from cli_backup.storage import LocalStorage


@unittest.skipUnless(shutil.which('redis-cli') and shutil.which('redis-check-rdb'), 'requires Redis client tools')
class InterruptedRedisIntegration(unittest.TestCase):
    def test_truncated_snapshot_is_never_published(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        server.settimeout(10)
        port = server.getsockname()[1]
        commands = []
        errors = []
        def serve():
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(10)
                    reader = connection.makefile('rb')
                    while True:
                        header = reader.readline()
                        if not header: break
                        parts = []
                        if header.startswith(b'*'):
                            for _ in range(int(header[1:])):
                                length = reader.readline()
                                if not length.startswith(b'$'): raise ValueError('invalid RESP string')
                                size = int(length[1:])
                                parts.append(reader.read(size))
                                if reader.read(2) != b'\r\n': raise ValueError('invalid RESP ending')
                        else:
                            parts = header.strip().split()
                        command = parts[0].upper()
                        commands.append(command)
                        if command == b'PING': connection.sendall(b'+PONG\r\n')
                        elif command == b'REPLCONF': connection.sendall(b'+OK\r\n')
                        elif command in (b'SYNC', b'PSYNC'):
                            connection.sendall(b'$1024\r\nREDIS0009partial')
                            break
                        else: raise ValueError(f'unexpected Redis command: {command!r}')
            except Exception as exc:
                errors.append(exc)
            finally:
                server.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory(prefix='cli-backup-interrupted-') as tmp:
            env = {'CLI_BACKUP_PROJECT': 'fixture', 'CLI_BACKUP_LOCAL_ROOT': tmp,
                   'CLI_BACKUP_REDIS_HOST': '127.0.0.1', 'CLI_BACKUP_REDIS_PORT': str(port),
                   'CLI_BACKUP_REDIS_TIMEOUT': '10'}
            with patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(main(['backup', 'redis']), 0)
            self.assertEqual(LocalStorage(Path(tmp)).list('fixture', 'redis'), [])
        thread.join(timeout=12)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(any(command in (b'SYNC', b'PSYNC') for command in commands))
