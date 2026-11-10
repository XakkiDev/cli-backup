"""Disposable database restore rehearsals; no published ports or shared networks."""
from __future__ import annotations

import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from .mysql import OBJECT_COUNTS_SQL
import re


def run(args: list[str], *, timeout: int = 30, **kwargs):
    return subprocess.run(args, check=True, timeout=timeout, **kwargs)


def wait_ready(command: list[str], env: dict[str, str], timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except subprocess.CalledProcessError:
            time.sleep(1)
    raise TimeoutError('disposable database did not become ready')


def rehearse_mysql(artifact: Path, image: str, client: str = 'mariadb', import_timeout: int = 14400) -> str:
    if import_timeout < 1 or not image or image.startswith('-') or client not in ('mysql', 'mariadb'):
        raise ValueError('test image and client are required')
    name = 'cli-backup-rehearsal-' + uuid.uuid4().hex
    password = secrets.token_hex(24)
    env = dict(os.environ, MYSQL_ROOT_PASSWORD=password, MYSQL_DATABASE='backup_rehearsal', MYSQL_PWD=password)
    started = False
    try:
        run(['docker', 'create', '--name', name, '--network', 'none',
             '-e', 'MYSQL_ROOT_PASSWORD', '-e', 'MYSQL_DATABASE', image], env=env, stdout=subprocess.DEVNULL)
        started = True
        run(['docker', 'start', name], env=env, stdout=subprocess.DEVNULL)
        wait_ready(['docker', 'exec', '-e', 'MYSQL_PWD', name, client, '-u', 'root', '-e', 'SELECT 1'], env)
        with artifact.open('rb') as source:
            run(['docker', 'exec', '-i', '-e', 'MYSQL_PWD', name, client, '-u', 'root', 'backup_rehearsal'],
                env=env, stdin=source, timeout=import_timeout)
        result = run(['docker', 'exec', '-e', 'MYSQL_PWD', name, client, '-u', 'root', 'backup_rehearsal',
                      '-N', '-B', '-e', OBJECT_COUNTS_SQL], env=env, capture_output=True, text=True)
        counts = result.stdout.strip()
        if not re.fullmatch(r'\d+(?:\s+\d+){4}', counts):
            raise ValueError('invalid object counts from restored database')
        return counts
    finally:
        if started:
            run(['docker', 'rm', '-f', '-v', name], env=env, stdout=subprocess.DEVNULL)


def rehearse_redis(artifact: Path, image: str, server: str = 'redis-server') -> None:
    if server not in ('redis-server', 'keydb-server'):
        raise ValueError('unsupported Redis server')
    if not image or image.startswith('-'):
        raise ValueError('test image is required')
    name = 'cli-backup-rehearsal-' + uuid.uuid4().hex
    started = False
    info = subprocess.run(['docker', 'info', '--format', '{{json .SecurityOptions}}'], check=True, capture_output=True, text=True, timeout=30).stdout
    user = '0:0' if 'name=rootless' in info else f'{os.getuid()}:{os.getgid()}'
    try:
        run(['docker', 'create', '--name', name, '--network', 'none',
             '--user', user,
             '--mount', f'type=bind,src={artifact.resolve()},dst=/data/dump.rdb,readonly',
             '--entrypoint', server, image, '--dir', '/data', '--dbfilename', 'dump.rdb',
             '--save', '', '--appendonly', 'no'], stdout=subprocess.DEVNULL)
        started = True
        run(['docker', 'start', name], stdout=subprocess.DEVNULL)
        wait_ready(['docker', 'exec', name, 'redis-cli', 'ping'], dict(os.environ))
        run(['docker', 'exec', name, 'redis-cli', 'DBSIZE'])
    finally:
        if started:
            run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL)
