from __future__ import annotations

import sys
from collections.abc import Callable

from safety_monitor.domain.diagnostics import diagnostic_error


def run_cli_safely(
    entrypoint: Callable[[], None],
    *,
    failure_message: str,
    interrupted_message: str,
) -> None:
    """Run a CLI entry point without letting unfiltered exception text reach stderr."""

    try:
        entrypoint()
    except KeyboardInterrupt:
        print(interrupted_message, file=sys.stderr)
        raise SystemExit(130) from None
    except SystemExit:
        raise
    except Exception as exc:
        print(f"{failure_message}: {diagnostic_error(exc)}", file=sys.stderr)
        raise SystemExit(1) from None
