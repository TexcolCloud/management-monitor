from __future__ import annotations

import argparse
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from common.logging_utils import setup_logging
from common.cli import run_cli_safely
from common.runtime_config import RuntimeConfig, load_runtime_config
from data_acquisition.capture_components import (
    ApiClient,
    CaptureService,
    OutputWriter,
    TokenProvider,
    deduplicate_rows,
    filter_rows,
    parse_datetime,
    range_slug,
    read_captured_headers,
    read_request_config,
    row_datetime,
)
from safety_monitor.domain.time_window import portal_datetime_text
from safety_monitor.domain.diagnostics import redact_text


def parse_args() -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path, default=None)
    config_args, _ = config_parser.parse_known_args()
    runtime = load_runtime_config(config_args.config)

    parser = argparse.ArgumentParser(
        description=(
            "Capture daily-management list/detail data for a manually selected "
            "start/end time range. The default filter field is createTime."
        ),
        parents=[config_parser],
    )
    parser.add_argument("--start", required=True, help="Start time, e.g. 2026-07-01")
    parser.add_argument("--end", required=True, help="End time, e.g. 2026-07-20")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--headers-file", type=Path, default=runtime.headers_file)
    parser.add_argument("--body-file", type=Path, default=None)
    parser.add_argument("--token-source", type=Path, default=runtime.token_source_dir)
    parser.add_argument("--page-size", type=int, default=runtime.page_size)
    parser.add_argument("--max-pages", type=int, default=runtime.max_pages)
    parser.add_argument("--date-field", default="createTime")
    parser.add_argument("--request-start-field", default="startTime")
    parser.add_argument("--request-end-field", default="endTime")
    parser.add_argument("--request-timeout", type=float, default=runtime.request_timeout_seconds)
    parser.add_argument("--retries", type=int, default=runtime.request_retries)
    parser.add_argument("--max-detail-failures", type=int, default=runtime.detail_max_failures)
    parser.add_argument("--retry-backoff", type=float, default=runtime.retry_backoff_seconds)
    parser.add_argument(
        "--browser-bridge-url",
        default="",
        help="Execute portal requests in an existing logged-in browser session.",
    )
    parser.add_argument(
        "--browser-bridge-token",
        default=os.environ.get("WORKORDER_BROWSER_BRIDGE_TOKEN", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-empty-page-stop",
        action="store_true",
        help="Continue through reported pages even after an empty page.",
    )
    parser.add_argument(
        "--no-total-validation",
        action="store_true",
        help="Disable API total consistency enforcement.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow a batch to continue when fetched rows differ from the API total.",
    )
    parser.add_argument("--details", action="store_true", help="Also fetch detail records.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    args.runtime_config = runtime
    return args


def _validate_positive(name: str, value: int | float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0: {value}")


def enforce_detail_failure_threshold(
    result,
    max_failures: int,
    output_dir: Path,
    start: datetime,
    end: datetime,
    date_field: str,
    detail_url: str,
    logger,
) -> bool:
    if result.statistics.details_failed <= max_failures:
        return True

    attempt_dir = output_dir / "failed-attempts" / (
        f"details-{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}"
    )
    writer = OutputWriter(attempt_dir)
    payload = {
        "exportedAt": datetime.now().isoformat(timespec="seconds"),
        "apiUrl": redact_text(detail_url),
        "totalNumber": len(result.failed_details),
        "filter": {
            "mode": "manual_start_end",
            "dateField": date_field,
            "start": start.isoformat(sep=" "),
            "end": end.isoformat(sep=" "),
        },
        "statistics": result.statistics.to_dict(),
        "rows": result.failed_details,
    }
    writer.write_json(attempt_dir / "management-api-detail-failures.json", payload)
    writer.write_csv(attempt_dir / "management-api-detail-failures.csv", result.failed_details)
    writer.write_csv(
        attempt_dir / "management-api-detail-failures-excel-safe.csv",
        result.failed_details,
        spreadsheet_safe=True,
    )
    logger.error("详情失败批次已隔离: %s", attempt_dir)
    logger.error(
        "Detail failure threshold exceeded: failed=%s allowed=%s; batch remains diagnostic only",
        result.statistics.details_failed,
        max_failures,
    )
    raise RuntimeError(
        "Detail failure threshold exceeded: "
        f"failed={result.statistics.details_failed} allowed={max_failures}"
    )


def _close_file_handlers(logger: logging.Logger) -> None:
    handlers = getattr(logger, "handlers", ())
    if not isinstance(handlers, (list, tuple)):
        return
    for handler in list(handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()


def run_capture(args: argparse.Namespace, runtime: RuntimeConfig) -> dict[str, Path]:
    start = parse_datetime(args.start, end_of_day=False)
    end = parse_datetime(args.end, end_of_day=True)
    if start > end:
        raise ValueError(f"start must be <= end: {args.start} > {args.end}")
    for name in ("page_size", "max_pages", "request_timeout", "retries"):
        _validate_positive(name, getattr(args, name))
    if args.retry_backoff < 0:
        raise ValueError(f"retry_backoff must be >= 0: {args.retry_backoff}")
    if args.max_detail_failures < 0:
        raise ValueError("max_detail_failures must be >= 0")
    request_start_field = getattr(args, "request_start_field", "startTime")
    request_end_field = getattr(args, "request_end_field", "endTime")
    if bool(request_start_field) != bool(request_end_field):
        raise ValueError("request_start_field and request_end_field must be specified together")

    output_dir = (
        args.out or runtime.capture_root / range_slug(start, end)
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(output_dir / "capture.log", verbose=args.verbose)
    try:
        body_from_file, headers_from_body = read_request_config(args.body_file)
        base_body = dict(body_from_file)
        if request_start_field:
            base_body[request_start_field] = portal_datetime_text(start)
            base_body[request_end_field] = portal_datetime_text(end)
        headers_file = args.headers_file.resolve()
        captured_headers = {
            **headers_from_body,
            **read_captured_headers(headers_file),
        }
        browser_bridge_url = getattr(args, "browser_bridge_url", "")
        token = ""
        if not browser_bridge_url:
            admin_host = urlparse(runtime.list_api_url).hostname or "api.example.invalid"
            token = TokenProvider(
                source_dir=args.token_source.resolve(),
                output_dir=output_dir,
                captured_headers=captured_headers,
                explicit_token="",
                environment_token=os.environ.get("WORKORDER_TOKEN", ""),
                admin_host=admin_host,
            ).resolve()

        client = ApiClient(
            list_url=runtime.list_api_url,
            detail_url=runtime.detail_api_url,
            token=token,
            captured_headers=captured_headers,
            timeout_seconds=args.request_timeout,
            retries=args.retries,
            retry_backoff_seconds=args.retry_backoff,
            retryable_api_codes=runtime.retryable_api_codes,
            browser_bridge_url=browser_bridge_url,
            browser_bridge_token=getattr(args, "browser_bridge_token", ""),
            logger=logger,
        )
        service = CaptureService(
            api_client=client,
            max_pages=args.max_pages,
            stop_on_empty_page=runtime.stop_on_empty_page and not args.no_empty_page_stop,
            validate_total=runtime.validate_total and not args.no_total_validation,
            allow_partial=args.allow_partial,
            logger=logger,
        )
        result = service.capture(
            start=start,
            end=end,
            date_field=args.date_field,
            page_size=args.page_size,
            base_body=base_body,
            include_details=args.details,
        )
        if args.details:
            enforce_detail_failure_threshold(
                result,
                args.max_detail_failures,
                output_dir,
                start,
                end,
                args.date_field,
                runtime.detail_api_url,
                logger,
            )
        diagnostic_only = bool(result.statistics.incomplete_reasons) and not args.allow_partial
        write_dir = output_dir
        if diagnostic_only:
            write_dir = output_dir / "failed-attempts" / (
                f"capture-{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}"
            )
        paths = OutputWriter(write_dir).write(
            result=result,
            start=start,
            end=end,
            date_field=args.date_field,
            list_url=runtime.list_api_url,
            detail_url=runtime.detail_api_url,
            headers_file=headers_file,
            body_file=args.body_file.resolve() if args.body_file else None,
            page_size=args.page_size,
            captured_headers_used=bool(captured_headers),
            include_details=args.details,
        )

        stats = result.statistics
        logger.info(
            "Batch rows: reported=%s fetched=%s unique=%s duplicate=%s matched=%s",
            stats.source_total_reported,
            stats.source_rows_fetched,
            stats.unique_rows,
            stats.duplicate_rows,
            stats.matched_rows,
        )
        logger.info("Date field: %s", args.date_field)
        logger.info("Date range: %s - %s", start.isoformat(sep=" "), end.isoformat(sep=" "))
        logger.info("JSON: %s", paths["list_json"])
        logger.info("CSV: %s", paths["list_csv"])
        if args.details:
            logger.info(
                "Details: requested=%s succeeded=%s failed=%s",
                stats.details_requested,
                stats.details_succeeded,
                stats.details_failed,
            )
            logger.info("Details JSON: %s", paths["details_json"])
            logger.info("Details CSV: %s", paths["details_csv"])
            logger.info("Detail failures JSON: %s", paths["failures_json"])
        if diagnostic_only:
            raise RuntimeError(
                "Capture is incomplete; diagnostic files were retained: "
                + "; ".join(stats.incomplete_reasons)
            )
        return paths
    finally:
        _close_file_handlers(logger)


def main() -> None:
    args = parse_args()
    run_capture(args, args.runtime_config)


if __name__ == "__main__":
    run_cli_safely(
        main,
        failure_message="日常管理数据抓取失败",
        interrupted_message="已停止日常管理数据抓取",
    )
