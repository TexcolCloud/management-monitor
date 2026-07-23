from __future__ import annotations

from common.environment import load_environment
from common.cli import run_cli_safely


load_environment()

from workflows.daily_workflow import main


if __name__ == "__main__":
    run_cli_safely(
        main,
        failure_message="日常管理工作流失败",
        interrupted_message="已停止日常管理工作流",
    )
