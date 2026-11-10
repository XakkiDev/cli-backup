"""MariaDB/MySQL dump subprocesses."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_$-]{0,63}\Z")


@dataclass(frozen=True)
class MySQLConfig:
    mode: str
    database: str
    user: str
    password: str
    host: str = "127.0.0.1"
    port: int = 3306
    service: str = "mariadb"
    network: str = ""
    image: str = ""
    compose_dir: str = "."
    client: str = "mariadb"
    compose_env_files: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.client not in {"mariadb", "mysql"}:
            raise ValueError("invalid database client")
        if self.mode not in {"compose", "container", "tcp"}:
            raise ValueError("unknown connection mode")
        if not _IDENTIFIER.fullmatch(self.database) or not self.user or self.user.startswith('-'):
            raise ValueError("invalid database or user")
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid port")
        if self.mode != "compose" and (not self.host or self.host.startswith('-') or not re.fullmatch(r"[A-Za-z0-9_.:-]+", self.host)):
            raise ValueError("invalid host")
        if self.mode == "compose" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.service):
            raise ValueError("invalid service")
        if self.mode == "container" and (not self.network or not self.image or self.network.startswith('-') or self.image.startswith('-')):
            raise ValueError("network and image are required")


def command(config: MySQLConfig, restore: bool = False) -> list[str]:
    config.validate()
    executable = config.client if restore else ("mariadb-dump" if config.client == "mariadb" else "mysqldump")
    if config.mode == "compose":
        prefix = ["docker", "compose"]
        for env_file in config.compose_env_files:
            if not env_file or env_file.startswith('-'):
                raise ValueError("invalid Compose env file")
            prefix.extend(["--env-file", env_file])
        prefix.extend(["exec", "-T", "-e", "MYSQL_PWD", config.service])
    elif config.mode == "container":
        prefix = ["docker", "run", "--rm", "-i", "--network", config.network, "-e", "MYSQL_PWD", config.image]
    else:
        prefix = []
    args = [executable, "--user", config.user]
    if config.mode != "compose":
        args.extend(["--host", config.host, "--port", str(config.port), "--protocol", "tcp"])
    if not restore:
        args.extend(["--single-transaction", "--routines", "--triggers", "--events", "--hex-blob"])
    return prefix + args + [config.database]


def capture_mysql(config: MySQLConfig, output: BinaryIO) -> None:
    env = dict(os.environ, MYSQL_PWD=config.password)
    subprocess.run(command(config), cwd=config.compose_dir if config.mode == 'compose' else None,
                   env=env, stdin=subprocess.DEVNULL, stdout=output, check=True)


def restore_mysql(config: MySQLConfig, source: BinaryIO) -> None:
    env = dict(os.environ, MYSQL_PWD=config.password)
    subprocess.run(command(config, restore=True), cwd=config.compose_dir if config.mode == 'compose' else None,
                   env=env, stdin=source, check=True)


OBJECT_COUNTS_SQL = (
    "SELECT (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'), "
    "(SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=DATABASE()), "
    "(SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE()), "
    "(SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()), "
    "(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE())"
)


def count_mysql_objects(config: MySQLConfig) -> str:
    env = dict(os.environ, MYSQL_PWD=config.password)
    result = subprocess.run(command(config, restore=True) + ['-N', '-B', '-e', OBJECT_COUNTS_SQL],
                            cwd=config.compose_dir if config.mode == 'compose' else None,
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
    count = result.stdout.strip()
    if not re.fullmatch(r'\d+(?:\s+\d+){4}', count):
        raise ValueError('invalid object counts from source database')
    return count
