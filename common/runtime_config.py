from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "runtime.json"


@dataclass(frozen=True)
class RuntimeConfig:
    list_api_url: str
    detail_api_url: str
    attachment_download_url: str
    portal_login_url: str
    login_wait_ms: int
    post_login_settle_ms: int
    replay_click_delay_ms: int
    replay_step_timeout_ms: int
    request_timeout_seconds: float
    request_retries: int
    retryable_api_codes: tuple[int, ...]
    detail_max_failures: int
    retry_backoff_seconds: float
    attachment_timeout_ms: int
    attachment_retries: int
    attachment_concurrency: int
    attachment_max_failures: int
    page_size: int
    max_pages: int
    stop_on_empty_page: bool
    validate_total: bool
    capture_root: Path
    export_root: Path
    headers_file: Path
    token_source_dir: Path
    attachment_dir_name: str
    login_profile_dir: Path
    login_navigation_file: Path


def _nested(payload: dict[str, Any], section: str, key: str, default: Any) -> Any:
    values = payload.get(section, {})
    return values.get(key, default) if isinstance(values, dict) else default


def _env(name: str, value: Any) -> Any:
    candidate = os.environ.get(name)
    return value if candidate in (None, "") else candidate


def _integer(name: str, value: Any, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer: {value!r}") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}: {parsed}")
    return parsed


def _number(name: str, value: Any, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number: {value!r}") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}: {parsed}")
    return parsed


def _boolean(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false: {value!r}")


def _integer_tuple(name: str, value: Any) -> tuple[int, ...]:
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list of integers: {value!r}")
    try:
        return tuple(int(item) for item in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a list of integers: {value!r}") from exc


def _path(name: str, value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return Path(text)


def _directory_name(name: str, value: Any) -> str:
    text = str(value or "").strip()
    candidate = Path(text)
    if not text or text in {".", ".."} or candidate.name != text or candidate.is_absolute():
        raise ValueError(f"{name} must be a single directory name: {value!r}")
    return text


def load_runtime_config(config_file: Path | str | None = None) -> RuntimeConfig:
    configured_path = config_file or os.environ.get("WORKORDER_CONFIG") or DEFAULT_CONFIG_FILE
    path = Path(configured_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Runtime config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid runtime config JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Runtime config must be a JSON object: {path}")

    list_url = _env(
        "WORKORDER_LIST_API_URL",
        _nested(payload, "api", "list_url", ""),
    )
    detail_url = _env(
        "WORKORDER_DETAIL_API_URL",
        _nested(payload, "api", "detail_url", list_url),
    )
    attachment_url = _env(
        "WORKORDER_ATTACHMENT_URL",
        _nested(payload, "api", "attachment_download_url", ""),
    )
    portal_url = _env(
        "WORKORDER_PORTAL_URL",
        _nested(payload, "portal", "login_url", ""),
    )
    for name, value in {
        "api.list_url": list_url,
        "api.detail_url": detail_url,
        "api.attachment_download_url": attachment_url,
        "portal.login_url": portal_url,
    }.items():
        if not str(value).strip():
            raise ValueError(f"{name} must not be empty")

    return RuntimeConfig(
        list_api_url=str(list_url),
        detail_api_url=str(detail_url).rstrip("/"),
        attachment_download_url=str(attachment_url),
        portal_login_url=str(portal_url),
        login_wait_ms=_integer(
            "portal.login_wait_ms",
            _env("WORKORDER_LOGIN_WAIT_MS", _nested(payload, "portal", "login_wait_ms", 300000)),
        ),
        post_login_settle_ms=_integer(
            "portal.post_login_settle_ms",
            _env(
                "WORKORDER_POST_LOGIN_SETTLE_MS",
                _nested(payload, "portal", "post_login_settle_ms", 5000),
            ),
            minimum=0,
        ),
        replay_click_delay_ms=_integer(
            "portal.replay_click_delay_ms",
            _env(
                "WORKORDER_REPLAY_CLICK_DELAY_MS",
                _nested(payload, "portal", "replay_click_delay_ms", 800),
            ),
            minimum=0,
        ),
        replay_step_timeout_ms=_integer(
            "portal.replay_step_timeout_ms",
            _env(
                "WORKORDER_REPLAY_STEP_TIMEOUT_MS",
                _nested(payload, "portal", "replay_step_timeout_ms", 12000),
            ),
        ),
        request_timeout_seconds=_number(
            "network.request_timeout_seconds",
            _env(
                "WORKORDER_REQUEST_TIMEOUT",
                _nested(payload, "network", "request_timeout_seconds", 60),
            ),
            minimum=0.001,
        ),
        request_retries=_integer(
            "network.request_retries",
            _env(
                "WORKORDER_REQUEST_RETRIES",
                _nested(payload, "network", "request_retries", 3),
            ),
        ),
        retryable_api_codes=_integer_tuple(
            "network.retryable_api_codes",
            _env(
                "WORKORDER_RETRYABLE_API_CODES",
                _nested(
                    payload,
                    "network",
                    "retryable_api_codes",
                    [408, 429, 500, 502, 503, 504],
                ),
            ),
        ),
        detail_max_failures=_integer(
            "network.detail_max_failures",
            _env(
                "WORKORDER_DETAIL_MAX_FAILURES",
                _nested(payload, "network", "detail_max_failures", 0),
            ),
            minimum=0,
        ),
        retry_backoff_seconds=_number(
            "network.retry_backoff_seconds",
            _env(
                "WORKORDER_RETRY_BACKOFF",
                _nested(payload, "network", "retry_backoff_seconds", 1),
            ),
        ),
        attachment_timeout_ms=_integer(
            "network.attachment_timeout_ms",
            _env(
                "WORKORDER_ATTACHMENT_TIMEOUT_MS",
                _nested(payload, "network", "attachment_timeout_ms", 60000),
            ),
        ),
        attachment_retries=_integer(
            "network.attachment_retries",
            _env(
                "WORKORDER_ATTACHMENT_RETRIES",
                _nested(payload, "network", "attachment_retries", 3),
            ),
        ),
        attachment_concurrency=_integer(
            "network.attachment_concurrency",
            _env(
                "WORKORDER_ATTACHMENT_CONCURRENCY",
                _nested(payload, "network", "attachment_concurrency", 3),
            ),
        ),
        attachment_max_failures=_integer(
            "network.attachment_max_failures",
            _env(
                "WORKORDER_ATTACHMENT_MAX_FAILURES",
                _nested(payload, "network", "attachment_max_failures", 0),
            ),
            minimum=0,
        ),
        page_size=_integer(
            "pagination.page_size",
            _env("WORKORDER_PAGE_SIZE", _nested(payload, "pagination", "page_size", 100)),
        ),
        max_pages=_integer(
            "pagination.max_pages",
            _env("WORKORDER_MAX_PAGES", _nested(payload, "pagination", "max_pages", 1000)),
        ),
        stop_on_empty_page=_boolean(
            "pagination.stop_on_empty_page",
            _env(
                "WORKORDER_STOP_ON_EMPTY_PAGE",
                _nested(payload, "pagination", "stop_on_empty_page", True),
            ),
        ),
        validate_total=_boolean(
            "pagination.validate_total",
            _env(
                "WORKORDER_VALIDATE_TOTAL",
                _nested(payload, "pagination", "validate_total", True),
            ),
        ),
        capture_root=_path(
            "paths.capture_root",
            _env(
                "WORKORDER_CAPTURE_ROOT",
                _nested(payload, "paths", "capture_root", "capture-output/daily-management"),
            ),
        ),
        export_root=_path(
            "paths.export_root",
            _env("WORKORDER_EXPORT_ROOT", _nested(payload, "paths", "export_root", "excel-output")),
        ),
        headers_file=_path(
            "paths.headers_file",
            _env(
                "WORKORDER_HEADERS_FILE",
                _nested(
                    payload,
                    "paths",
                    "headers_file",
                    "capture-output/daily-management/daily-management-request.json",
                ),
            ),
        ),
        token_source_dir=_path(
            "paths.token_source_dir",
            _env(
                "WORKORDER_TOKEN_SOURCE_DIR",
                _nested(payload, "paths", "token_source_dir", "capture-output/daily-management"),
            ),
        ),
        attachment_dir_name=_directory_name(
            "paths.attachment_dir_name",
            _env(
                "WORKORDER_ATTACHMENT_DIR_NAME",
                _nested(payload, "paths", "attachment_dir_name", "daily-attachments"),
            ),
        ),
        login_profile_dir=_path(
            "paths.login_profile_dir",
            _env(
                "WORKORDER_LOGIN_PROFILE_DIR",
                _nested(payload, "paths", "login_profile_dir", ".temp/daily-management-login-profile"),
            ),
        ),
        login_navigation_file=_path(
            "paths.login_navigation_file",
            _env(
                "WORKORDER_LOGIN_NAVIGATION_FILE",
                _nested(
                    payload,
                    "paths",
                    "login_navigation_file",
                    "data_acquisition/recordings/portal-navigation.local.json",
                ),
            ),
        ),
    )
