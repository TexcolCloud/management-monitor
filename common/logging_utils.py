from __future__ import annotations

import logging
import os
import sys
import traceback
import uuid
from pathlib import Path

from common.redaction import redact_text


class SensitiveDataFilter(logging.Filter):
    """Redact credentials and user identifiers before any handler emits them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
        return True


def setup_logging(log_file: Path | None = None, verbose: bool = False) -> logging.Logger:
    """Configure a consistent console/file logger for the workflow."""
    logger = logging.getLogger("workorder_daily_manage")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    run_id = os.environ.setdefault("WORKORDER_RUN_ID", uuid.uuid4().hex[:12])
    formatter = logging.Formatter(
        f"%(asctime)s | %(levelname)s | run={run_id} | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(SensitiveDataFilter())
    logger.addHandler(console)
    logger.propagate = False

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(SensitiveDataFilter())
        logger.addHandler(file_handler)
    return logger
