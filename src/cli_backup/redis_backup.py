"""Redis RDB capture via the official redis-cli replication client."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def capture_redis(output: Path, host: str, port: int, password: str | None = None, *, timeout: int = 14400) -> None:
    if not host or host.startswith('-') or not 1 <= port <= 65535:
        raise ValueError('invalid Redis address')
    if timeout < 1:
        raise ValueError('Redis timeout must be positive')
    env = os.environ.copy()
    if password is not None:
        env['REDISCLI_AUTH'] = password
    else:
        env.pop('REDISCLI_AUTH', None)
    subprocess.run(['redis-cli', '-h', host, '-p', str(port), '--rdb', str(output)], env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True, timeout=timeout)
    if not output.is_file() or output.stat().st_size == 0:
        raise ValueError('Redis produced an empty snapshot')
    subprocess.run(['redis-check-rdb', str(output)], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, check=True, timeout=timeout)
