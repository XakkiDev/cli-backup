"""Configuration resolution without executing project files."""
from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PREFIX = "CLI_BACKUP_"


def resolve(name: str, argument: str | None, project: Mapping[str, str], global_env: Mapping[str, str] | None = None) -> str | None:
    key = f"{PREFIX}{name.upper()}"
    env = os.environ if global_env is None else global_env
    return argument if argument is not None else project.get(key, env.get(key))


def validate_project(project: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project):
        raise ValueError("invalid project identifier")
    return project


def read_project_env(path: Path) -> dict[str, str]:
    """Read literal CLI_BACKUP assignments; no expansion or shell execution."""
    result: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not key.startswith(PREFIX):
            continue
        if not sep or not re.fullmatch(r"CLI_BACKUP_[A-Z0-9_]+", key):
            raise ValueError(f"invalid project setting at line {number}")
        try:
            tokens = shlex.split(value, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f"invalid project setting at line {number}") from exc
        if len(tokens) > 1:
            raise ValueError(f"invalid project setting at line {number}")
        if key in result:
            raise ValueError(f"duplicate project setting at line {number}")
        result[key] = tokens[0] if tokens else ""
    return result


@dataclass(frozen=True)
class Config:
    project: str
    local_root: str | None
    s3_bucket: str | None


def load_config(global_env: Mapping[str, str], project_env: Mapping[str, str], args: Mapping[str, str | None]) -> Config:
    project = resolve("project", args.get("project"), project_env, global_env)
    if not project:
        raise ValueError("CLI_BACKUP_PROJECT is required")
    root = resolve("local_root", args.get("local_root"), project_env, global_env) or None
    bucket = resolve("s3_bucket", args.get("s3_bucket"), project_env, global_env) or None
    if not root and not bucket:
        raise ValueError("configure CLI_BACKUP_LOCAL_ROOT or CLI_BACKUP_S3_BUCKET")
    return Config(validate_project(project), root, bucket)
