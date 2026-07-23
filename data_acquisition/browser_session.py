from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from common.runtime_config import RuntimeConfig
from common.workflow_paths import PROJECT_ROOT, ensure_inside_project, project_path
from data_acquisition.acquisition_steps import resolve_node_executable


def _browser_environment() -> dict[str, str]:
    blocked_names = {"WORKORDER_TOKEN", "WORKORDER_BROWSER_BRIDGE_TOKEN"}
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PG", "FEISHU_"))
        and key.upper() not in blocked_names
    }


class BrowserSession:
    """Own a non-persistent browser and its authenticated loopback request bridge."""

    def __init__(
        self,
        args: argparse.Namespace,
        runtime: RuntimeConfig,
        state_path: Path,
        logger: logging.Logger,
    ) -> None:
        self.args = args
        self.runtime = runtime
        self.state_path = state_path
        self.logger = logger
        self.process: subprocess.Popen | None = None
        self.ready_file: Path | None = None
        self.url = ""
        self.token = ""

    def start(self) -> str:
        self.stop()
        navigation_file = ensure_inside_project(project_path(self.args.login_navigation_file))
        self.ready_file = self.state_path.parent / f"browser-session-{uuid.uuid4().hex}.json"
        command = [
            resolve_node_executable(),
            "data_acquisition/record-portal-clicks.js",
            f"--data-dir={ensure_inside_project(project_path(self.args.token_source))}",
            f"--filter-file={ensure_inside_project(project_path(self.args.headers_file))}",
            f"--auto-wait-ms={self.args.login_wait_ms}",
            "--require-token",
            "--require-request",
            "--stop-on-request",
            "--wait-for-login",
            "--keep-session",
            "--bridge-port=0",
            f"--bridge-ready-file={self.ready_file}",
        ]
        if not self.args.no_login_replay:
            command.append(f"--replay-file={navigation_file}")
        self.logger.info("Starting in-memory browser session for portal access")
        child_environment = _browser_environment()
        config_file = getattr(self.args, "config", None)
        if config_file:
            config_path = Path(config_file)
            if not config_path.is_absolute():
                config_path = PROJECT_ROOT / config_path
            child_environment["WORKORDER_CONFIG"] = str(config_path.resolve())
        self.process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=child_environment)
        while True:
            if self.ready_file.exists():
                try:
                    payload = json.loads(self.ready_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = None
                if payload is not None:
                    self._accept_ready_payload(payload)
                    self.ready_file.unlink(missing_ok=True)
                    self.logger.info("Browser request bridge ready: %s", self.url)
                    return self.url
            if self.process.poll() is not None:
                return_code = self.process.returncode
                self.stop()
                raise RuntimeError(
                    f"Browser session exited before becoming ready: {return_code}"
                )
            time.sleep(0.25)

    def _accept_ready_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self.stop()
            raise RuntimeError("Browser session ready file is invalid")
        url = str(payload.get("url") or "").rstrip("/")
        token = str(payload.get("token") or "")
        parsed = urlparse(url)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not port
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or len(token) < 32
        ):
            self.stop()
            raise RuntimeError("Browser session returned an invalid local bridge endpoint")
        self.url = url
        self.token = token

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None
        if self.ready_file:
            self.ready_file.unlink(missing_ok=True)
        self.ready_file = None
        self.url = ""
        self.token = ""
