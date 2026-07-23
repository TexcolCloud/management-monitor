from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from common.redaction import redact_text
from common.workflow_paths import PROJECT_ROOT


def run_command(
    command: list[str],
    label: str,
    logger: logging.Logger,
    cwd: Path = PROJECT_ROOT,
    env_overrides: Mapping[str, str] | None = None,
) -> None:
    display_items = [
        "--token=<redacted>" if item.startswith("--token=") else item
        for item in command
    ]
    printable = redact_text(
        " ".join(f'"{item}"' if " " in item else item for item in display_items)
    )
    logger.info("[%s] %s", label, printable)
    child_environment = os.environ.copy()
    if env_overrides:
        child_environment.update(env_overrides)
    try:
        completed = subprocess.run(command, cwd=cwd, env=child_environment)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label}无法启动，系统找不到命令：{command[0]}") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{label}失败，退出码：{completed.returncode}")
