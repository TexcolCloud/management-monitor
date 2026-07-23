from __future__ import annotations

import csv
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from common.spreadsheet_safety import spreadsheet_safe_text
from safety_monitor.application.acquire_snapshot import (
    AcquisitionService,
    row_datetime as domain_row_datetime,
)
from safety_monitor.domain.time_window import parse_datetime
from safety_monitor.domain.diagnostics import redact_text
from safety_monitor.domain.work_order import deduplicate_work_orders
from safety_monitor.ports.acquisition import CaptureResult, CaptureStatistics


DEFAULT_BODY = {
    "deptId": "",
    "newDeptId": "",
    "pageType": "1",
}

BLOCKED_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
}


def range_slug(start: datetime, end: datetime) -> str:
    return f"range-{start:%Y%m%d_%H%M%S}-{end:%Y%m%d_%H%M%S}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_request_config(path: Path | None) -> tuple[dict[str, Any], dict[str, str]]:
    if not path:
        return {}, {}
    payload = read_json(path.resolve())
    body = payload.get("body", payload) if isinstance(payload, dict) else {}
    headers: Any = {}
    if isinstance(payload, dict):
        headers = payload.get("headers") or payload.get("requestHeaders") or {}
    if not isinstance(body, dict) or isinstance(body, list):
        raise ValueError(f"invalid body file: {path}")
    if not isinstance(headers, dict):
        headers = {}
    return body, {str(key): str(value) for key, value in headers.items()}


def read_captured_headers(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = read_json(path.resolve())
    except (OSError, json.JSONDecodeError):
        return {}
    headers = payload.get("headers", {}) if isinstance(payload, dict) else {}
    if not isinstance(headers, dict):
        return {}
    return {str(key): str(value) for key, value in headers.items()}


def replay_headers(captured_headers: dict[str, str]) -> dict[str, str]:
    headers = {}
    for key, value in captured_headers.items():
        lower_key = key.lower()
        if lower_key in BLOCKED_HEADERS or lower_key.startswith("sec-ch-"):
            continue
        headers[key] = value
    return headers


def captured_bearer_token(headers: dict[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() != "authorization":
            continue
        match = re.match(r"^Bearer\s+(.+)$", value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def build_headers(captured_headers: dict[str, str], token: str) -> dict[str, str]:
    headers = replay_headers(captured_headers)
    if token:
        auth_key = next((key for key in headers if key.lower() == "authorization"), "")
        headers[auth_key or "Authorization"] = f"Bearer {token}"
    headers.setdefault("Content-Type", "application/json;charset=utf-8")
    return headers


class TokenProvider:
    def __init__(
        self,
        source_dir: Path,
        output_dir: Path,
        captured_headers: dict[str, str],
        explicit_token: str = "",
        environment_token: str = "",
        admin_host: str = "api.example.invalid",
    ) -> None:
        self.source_dir = source_dir
        self.output_dir = output_dir
        self.captured_headers = captured_headers
        self.explicit_token = explicit_token
        self.environment_token = environment_token
        self.admin_host = admin_host

    def _storage_state_token(self, source_dir: Path) -> str:
        storage_file = source_dir / "portal-storage-state.json"
        if not storage_file.exists():
            return ""
        try:
            storage = read_json(storage_file)
        except (OSError, json.JSONDecodeError):
            return ""

        for origin in storage.get("origins", []):
            if self.admin_host not in str(origin.get("origin", "")):
                continue
            for item in origin.get("localStorage", []):
                if item.get("name") in {"Admin-Token", "token", "Token"} and item.get("value"):
                    return str(item["value"])
        for cookie in storage.get("cookies", []):
            if self.admin_host not in str(cookie.get("domain", "")):
                continue
            if cookie.get("name") in {"Admin-Token", "token", "Token"} and cookie.get("value"):
                return str(cookie["value"])
        return ""

    def _saved_token(self, source_dir: Path) -> str:
        token_file = source_dir / "auth-token.json"
        if token_file.exists():
            try:
                token = str(read_json(token_file).get("token", ""))
                if token:
                    return token
            except (OSError, json.JSONDecodeError):
                pass
        return self._storage_state_token(source_dir)

    @staticmethod
    def _latest_login_token(source_dir: Path) -> str:
        response_dir = source_dir / "responses"
        if not response_dir.exists():
            return ""
        candidates: list[tuple[float, str]] = []
        for path in response_dir.glob("*login*.json"):
            try:
                payload = read_json(path)
                token = payload.get("data", {}).get("token")
            except (OSError, json.JSONDecodeError, AttributeError):
                token = ""
            if token:
                candidates.append((path.stat().st_mtime, str(token)))
        return sorted(candidates, reverse=True)[0][1] if candidates else ""

    def resolve(self) -> str:
        token = (
            self.explicit_token
            or self.environment_token
            or self._saved_token(self.source_dir)
            or self._saved_token(self.output_dir)
            or self._latest_login_token(self.source_dir)
            or captured_bearer_token(self.captured_headers)
        )
        if not token:
            raise RuntimeError("No login token found. Run npm run record:portal-clicks first.")
        return token


class BrowserBridgeError(RuntimeError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class ApiClient:
    def __init__(
        self,
        list_url: str,
        detail_url: str,
        token: str,
        captured_headers: dict[str, str],
        timeout_seconds: float = 60,
        retries: int = 3,
        retry_backoff_seconds: float = 1,
        retryable_api_codes: tuple[int, ...] = (408, 429, 500, 502, 503, 504),
        browser_bridge_url: str = "",
        browser_bridge_token: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self.list_url = list_url
        self.detail_url = detail_url.rstrip("/")
        self.headers = build_headers(captured_headers, token)
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.retryable_api_codes = {str(code) for code in retryable_api_codes}
        self.browser_bridge_url = browser_bridge_url.rstrip("/")
        self.browser_bridge_token = browser_bridge_token
        if self.browser_bridge_url:
            self.headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"authorization", "cookie", "proxy-authorization"}
            }
        self.logger = logger or logging.getLogger(__name__)

    def _browser_request(self, url: str, method: str, body: dict[str, Any] | None) -> str:
        request = urllib.request.Request(
            f"{self.browser_bridge_url}/request",
            data=json.dumps(
                {"url": url, "method": method, "headers": self.headers, "body": body},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.browser_bridge_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            bridge_payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(bridge_payload, dict):
            raise BrowserBridgeError(None, "Browser request bridge returned an invalid response")
        status = int(bridge_payload.get("status", 0))
        content = str(bridge_payload.get("text", ""))
        if status < 200 or status >= 300:
            raise BrowserBridgeError(status, f"Browser request bridge returned HTTP {status}")
        return content

    def request_json(
        self,
        url: str,
        method: str,
        body: dict[str, Any] | None = None,
        api_context: str | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                if self.browser_bridge_url:
                    content = self._browser_request(url, method, body)
                else:
                    request = urllib.request.Request(url, data=data, headers=self.headers, method=method)
                    with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                        content = response.read().decode("utf-8")
                payload = json.loads(content)
                if not isinstance(payload, dict):
                    raise RuntimeError(
                        f"Unexpected response type from {url}: {type(payload).__name__}"
                    )
                if api_context and str(payload.get("code")) != "200":
                    error = RuntimeError(
                        f"{api_context}: API code {payload.get('code')}: "
                        f"{payload.get('msg') or payload.get('message')}"
                    )
                    if str(payload.get("code")) not in self.retryable_api_codes:
                        raise error
                    last_error = error
                else:
                    return payload
            except urllib.error.HTTPError as exc:
                exc.read()
                last_error = RuntimeError(f"HTTP {exc.code}")
                if exc.code < 500 and exc.code not in {408, 429}:
                    raise last_error from exc
            except BrowserBridgeError as exc:
                last_error = exc
                if exc.status is not None and str(exc.status) not in self.retryable_api_codes:
                    raise exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries:
                delay = min(self.retry_backoff_seconds * (2 ** (attempt - 1)), 30)
                self.logger.warning(
                    "Request attempt %s/%s failed; retrying in %.1fs: %s",
                    attempt,
                    self.retries,
                    delay,
                    last_error,
                )
                time.sleep(delay)
        raise RuntimeError(
            f"Request failed after {self.retries} attempts: {url}: {last_error}"
        ) from last_error

    def request_page(
        self,
        page_no: int,
        page_size: int,
        base_body: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            **DEFAULT_BODY,
            **base_body,
            "pageNo": page_no,
            "pageSize": page_size,
            "pageType": str(base_body.get("pageType", DEFAULT_BODY["pageType"])),
        }
        result = self.request_json(self.list_url, "POST", body, api_context=f"page {page_no}")
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"page {page_no}: API data must be an object")
        page_info = data.get("pageInfo", {})
        page_rows = data.get("pageData", [])
        if not isinstance(page_info, dict) or not isinstance(page_rows, list):
            raise RuntimeError(f"page {page_no}: API pagination structure changed")
        if not all(isinstance(row, dict) for row in page_rows):
            raise RuntimeError(f"page {page_no}: pageData contains non-object rows")
        return {"pageInfo": page_info, "rows": page_rows, "requestBody": body}

    def request_detail(self, record_id: str) -> dict[str, Any]:
        result = self.request_json(
            f"{self.detail_url}/{record_id}",
            "GET",
            api_context=f"detail {record_id}",
        )
        data = result.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeError(f"detail {record_id}: API data must be an object")
        return data


def row_datetime(row: dict[str, Any], field: str) -> datetime | None:
    return domain_row_datetime(row, field)


def filter_rows(
    rows: list[dict[str, Any]],
    field: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if (value := row_datetime(row, field)) is not None and start <= value <= end
    ]


def deduplicate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return deduplicate_work_orders(rows)


class CaptureService:
    def __init__(
        self,
        api_client: ApiClient,
        max_pages: int = 1000,
        stop_on_empty_page: bool = True,
        validate_total: bool = True,
        allow_partial: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.api_client = api_client
        self.max_pages = max_pages
        self.stop_on_empty_page = stop_on_empty_page
        self.validate_total = validate_total
        self.allow_partial = allow_partial
        self.logger = logger or logging.getLogger(__name__)

    def capture(
        self,
        start: datetime,
        end: datetime,
        date_field: str,
        page_size: int,
        base_body: dict[str, Any],
        include_details: bool,
    ) -> CaptureResult:
        def progress(event: str, context: dict[str, Any]) -> None:
            if event == "page":
                self.logger.info(
                    "Page %s/%s: %s rows",
                    context["page"],
                    context["totalPages"],
                    context["rows"],
                )
            elif event == "detail":
                self.logger.info(
                    "Detail %s/%s: %s",
                    context["index"],
                    context["total"],
                    context["recordId"],
                )

        result = AcquisitionService(
            self.api_client,
            max_pages=self.max_pages,
            stop_on_empty_page=self.stop_on_empty_page,
            validate_total=self.validate_total,
            progress=progress,
        ).capture(
            start=start,
            end=end,
            date_field=date_field,
            page_size=page_size,
            base_body=base_body,
            include_details=include_details,
        )
        for reason in result.statistics.incomplete_reasons:
            self.logger.warning("Capture incomplete: %s", reason)
        return result


def csv_escape_row_value(value: Any, spreadsheet_safe: bool = False) -> Any:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return spreadsheet_safe_text(value) if spreadsheet_safe else value


class OutputWriter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def write_json(path: Path, payload: Any) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def write_csv(
        path: Path,
        rows: list[dict[str, Any]],
        spreadsheet_safe: bool = False,
    ) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            if not rows:
                return
            fieldnames: list[str] = []
            seen: set[str] = set()
            for row in rows:
                for key in row:
                    if key not in seen:
                        fieldnames.append(key)
                        seen.add(key)
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: csv_escape_row_value(row.get(key), spreadsheet_safe)
                        for key in fieldnames
                    }
                )

    def write(
        self,
        result: CaptureResult,
        start: datetime,
        end: datetime,
        date_field: str,
        list_url: str,
        detail_url: str,
        headers_file: Path,
        body_file: Path | None,
        page_size: int,
        captured_headers_used: bool,
        include_details: bool,
    ) -> dict[str, Path]:
        timestamp = datetime.now().isoformat(timespec="seconds")
        filter_info = {
            "mode": "manual_start_end",
            "dateField": date_field,
            "start": start.isoformat(sep=" "),
            "end": end.isoformat(sep=" "),
        }
        list_json = self.output_dir / "management-api-all.json"
        list_csv = self.output_dir / "management-api-all.csv"
        list_excel_safe_csv = self.output_dir / "management-api-all-excel-safe.csv"
        self.write_json(
            list_json,
            {
                "exportedAt": timestamp,
                "apiUrl": redact_text(list_url),
                "headersFile": str(headers_file),
                "bodyFile": str(body_file) if body_file else "",
                "requestBody": result.request_body,
                "capturedHeadersUsed": captured_headers_used,
                "pageSize": page_size,
                "totalNumber": len(result.rows),
                "sourceTotalNumber": result.statistics.source_total_reported,
                "totalPage": result.statistics.pages_reported,
                "filter": filter_info,
                "statistics": result.statistics.to_dict(),
                "rows": result.rows,
            },
        )
        self.write_csv(list_csv, result.rows)
        self.write_csv(list_excel_safe_csv, result.rows, spreadsheet_safe=True)
        paths = {
            "list_json": list_json,
            "list_csv": list_csv,
            "list_excel_safe_csv": list_excel_safe_csv,
        }

        if include_details:
            details_json = self.output_dir / "management-api-details.json"
            details_csv = self.output_dir / "management-api-details.csv"
            details_excel_safe_csv = (
                self.output_dir / "management-api-details-excel-safe.csv"
            )
            self.write_json(
                details_json,
                {
                    "exportedAt": timestamp,
                    "apiUrl": redact_text(detail_url),
                    "totalNumber": len(result.details),
                    "filter": filter_info,
                    "statistics": result.statistics.to_dict(),
                    "rows": result.details,
                },
            )
            self.write_csv(details_csv, result.details)
            self.write_csv(details_excel_safe_csv, result.details, spreadsheet_safe=True)
            failures_json = self.output_dir / "management-api-detail-failures.json"
            failures_csv = self.output_dir / "management-api-detail-failures.csv"
            failures_excel_safe_csv = (
                self.output_dir / "management-api-detail-failures-excel-safe.csv"
            )
            self.write_json(
                failures_json,
                {
                    "exportedAt": timestamp,
                    "apiUrl": redact_text(detail_url),
                    "totalNumber": len(result.failed_details),
                    "filter": filter_info,
                    "statistics": result.statistics.to_dict(),
                    "rows": result.failed_details,
                },
            )
            self.write_csv(failures_csv, result.failed_details)
            self.write_csv(
                failures_excel_safe_csv,
                result.failed_details,
                spreadsheet_safe=True,
            )
            paths.update(
                {
                    "details_json": details_json,
                    "details_csv": details_csv,
                    "details_excel_safe_csv": details_excel_safe_csv,
                    "failures_json": failures_json,
                    "failures_csv": failures_csv,
                    "failures_excel_safe_csv": failures_excel_safe_csv,
                }
            )
        return paths
