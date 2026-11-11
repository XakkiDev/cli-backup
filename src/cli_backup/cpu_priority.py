"""Lower backup CPU scheduling priority without raising an inherited value."""
from __future__ import annotations

import os


def validate_cpu_nice(value: str | int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('CLI_BACKUP_CPU_NICE must be an integer from 0 to 19') from exc
    if not 0 <= parsed <= 19:
        raise ValueError('CLI_BACKUP_CPU_NICE must be an integer from 0 to 19')
    return parsed


def lower_cpu_priority(value: int) -> None:
    priority = validate_cpu_nice(value)
    current = os.getpriority(os.PRIO_PROCESS, 0)
    if current < priority:
        os.setpriority(os.PRIO_PROCESS, 0, priority)
