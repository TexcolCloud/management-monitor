from __future__ import annotations

import logging
import json
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from common.runtime_config import load_runtime_config
from data_acquisition.capture_daily_range import run_capture

from data_acquisition.capture_components import (
    CaptureService,
    ApiClient,
    captured_bearer_token,
    deduplicate_rows,
    parse_datetime,
)
from data_acquisition.time_range import recent_range
from safety_monitor.ports.acquisition import CaptureResult, CaptureStatistics


class FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self.content = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.content


class FakeApiClient:
    def __init__(self, pages: dict[int, dict], failed_details: set[str] | None = None) -> None:
        self.pages = pages
        self.failed_details = failed_details or set()

    def request_page(self, page_no: int, page_size: int, base_body: dict) -> dict:
        page = dict(self.pages[page_no])
        page["requestBody"] = {**base_body, "pageNo": page_no, "pageSize": page_size}
        return page

    def request_detail(self, record_id: str) -> dict:
        if record_id in self.failed_details:
            raise RuntimeError(
                "simulated detail failure Authorization: Bearer fake-secret-token "
                "open_id=ou_fake-user"
            )
        return {"id": record_id, "detail": True}


class CaptureComponentsTest(unittest.TestCase):
    def test_captured_bearer_token_accepts_only_bearer_authorization(self) -> None:
        self.assertEqual(
            captured_bearer_token({"authorization": "Bearer memory-secret"}),
            "memory-secret",
        )
        self.assertEqual(captured_bearer_token({"authorization": "Basic unsafe"}), "")

    def test_api_business_error_is_retried(self) -> None:
        client = ApiClient(
            "http://example/list",
            "http://example/detail",
            "token",
            {},
            retries=2,
            retry_backoff_seconds=0,
            retryable_api_codes=(500,),
        )
        responses = [
            FakeHttpResponse({"code": 500, "msg": "busy"}),
            FakeHttpResponse({"code": 200, "data": {"pageInfo": {}, "pageData": []}}),
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            result = client.request_page(1, 100, {})
        self.assertEqual(result["rows"], [])
        self.assertEqual(urlopen.call_count, 2)

    def test_non_retryable_business_error_fails_immediately(self) -> None:
        client = ApiClient(
            "http://example/list",
            "http://example/detail",
            "token",
            {},
            retries=3,
            retry_backoff_seconds=0,
            retryable_api_codes=(500,),
        )
        with patch(
            "urllib.request.urlopen",
            return_value=FakeHttpResponse({"code": 401, "msg": "expired"}),
        ) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "API code 401"):
                client.request_page(1, 100, {})
        self.assertEqual(urlopen.call_count, 1)

    def test_api_client_uses_local_browser_bridge_for_authenticated_requests(self) -> None:
        client = ApiClient(
            "http://portal.example/list",
            "http://portal.example/detail",
            "token",
            {"X-Trace": "capture", "Authorization": "Bearer portal-secret"},
            browser_bridge_url="http://127.0.0.1:43123",
            browser_bridge_token="bridge-secret",
        )
        bridge_response = {
            "status": 200,
            "text": json.dumps({"code": 200, "data": {"pageInfo": {}, "pageData": []}}),
        }
        with patch(
            "urllib.request.urlopen", return_value=FakeHttpResponse(bridge_response)
        ) as urlopen:
            result = client.request_page(1, 100, {})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:43123/request")
        request_payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request_payload["url"], "http://portal.example/list")
        self.assertEqual(request_payload["method"], "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer bridge-secret")
        self.assertNotIn("Authorization", request_payload["headers"])
        self.assertEqual(result["rows"], [])

    def test_browser_bridge_retries_retryable_http_status(self) -> None:
        client = ApiClient(
            "http://portal.example/list",
            "http://portal.example/detail",
            "token",
            {},
            retries=2,
            retry_backoff_seconds=0,
            retryable_api_codes=(503,),
            browser_bridge_url="http://127.0.0.1:43123",
            browser_bridge_token="bridge-secret",
        )
        responses = [
            FakeHttpResponse({"status": 503, "text": "busy"}),
            FakeHttpResponse(
                {"status": 200, "text": json.dumps({"code": 200, "data": {"pageInfo": {}, "pageData": []}})}
            ),
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            result = client.request_page(1, 100, {})
        self.assertEqual(result["rows"], [])
        self.assertEqual(urlopen.call_count, 2)

    def test_parse_datetime_expands_date_end(self) -> None:
        self.assertEqual(parse_datetime("2026-07-21"), datetime(2026, 7, 21))
        self.assertEqual(
            parse_datetime("2026-07-21", end_of_day=True),
            datetime(2026, 7, 21, 23, 59, 59, 999999),
        )

    def test_deduplicate_uses_safety_code_as_business_identity(self) -> None:
        rows = [
            {"id": "1", "safetyCode": "A"},
            {"id": "2", "safetyCode": "A"},
            {"id": "3", "safetyCode": "C"},
            {"safetyCode": "C"},
        ]
        self.assertEqual(len(deduplicate_rows(rows)), 2)

    def test_capture_tracks_empty_page_duplicates_and_detail_failure(self) -> None:
        pages = {
            1: {
                "pageInfo": {"totalPage": 3, "totalNumber": 3},
                "rows": [
                    {"id": "1", "safetyCode": "A", "createTime": "2026-07-10 10:00:00"},
                    {"id": "2", "safetyCode": "B", "createTime": "2026-07-11 10:00:00"},
                ],
            },
            2: {
                "pageInfo": {},
                "rows": [
                    {"id": "2", "safetyCode": "B", "createTime": "2026-07-11 10:00:00"},
                    {"id": "3", "safetyCode": "C", "createTime": "2026-06-01 10:00:00"},
                ],
            },
            3: {"pageInfo": {}, "rows": []},
        }
        service = CaptureService(
            FakeApiClient(pages, failed_details={"2"}),
            max_pages=10,
            logger=logging.getLogger("capture-test"),
        )
        result = service.capture(
            start=datetime(2026, 7, 1),
            end=datetime(2026, 7, 31, 23, 59, 59),
            date_field="createTime",
            page_size=100,
            base_body={"pageType": "1"},
            include_details=True,
        )

        self.assertEqual([row["id"] for row in result.rows], ["1", "2"])
        self.assertEqual(result.statistics.source_rows_fetched, 4)
        self.assertEqual(result.statistics.duplicate_rows, 1)
        self.assertEqual(result.statistics.matched_rows, 2)
        self.assertEqual(result.statistics.details_succeeded, 1)
        self.assertEqual(result.statistics.details_failed, 1)
        self.assertTrue(result.statistics.empty_page_stopped)
        self.assertTrue(result.statistics.total_consistent)
        self.assertEqual(len(result.details), 1)
        self.assertIn("_captureError", result.failed_details[0])
        self.assertNotIn("fake-secret-token", result.failed_details[0]["_captureError"])
        self.assertNotIn("ou_fake-user", result.failed_details[0]["_captureError"])
        self.assertIn("<redacted>", result.failed_details[0]["_captureError"])

    def test_capture_rejects_duplicates_that_mask_missing_rows(self) -> None:
        pages = {
            1: {
                "pageInfo": {"totalPage": 2, "totalNumber": 4},
                "rows": [
                    {"id": "1", "createTime": "2026-07-10 10:00:00"},
                    {"id": "2", "createTime": "2026-07-11 10:00:00"},
                ],
            },
            2: {
                "pageInfo": {},
                "rows": [
                    {"id": "2", "createTime": "2026-07-11 10:00:00"},
                    {"id": "3", "createTime": "2026-07-12 10:00:00"},
                ],
            },
        }
        service = CaptureService(FakeApiClient(pages), max_pages=10)
        result = service.capture(
            start=datetime(2026, 7, 1),
            end=datetime(2026, 7, 31),
            date_field="createTime",
            page_size=2,
            base_body={},
            include_details=False,
        )
        self.assertFalse(result.statistics.total_consistent)
        self.assertTrue(
            any("unique=3 duplicates=1" in reason for reason in result.statistics.incomplete_reasons)
        )

    def test_capture_rejects_reported_pages_over_limit(self) -> None:
        service = CaptureService(
            FakeApiClient(
                {
                    1: {
                        "pageInfo": {"totalPage": 11, "totalNumber": 0},
                        "rows": [],
                    }
                }
            ),
            max_pages=10,
        )
        with self.assertRaisesRegex(RuntimeError, "max_pages=10"):
            service.capture(
                start=datetime(2026, 7, 1),
                end=datetime(2026, 7, 31),
                date_field="createTime",
                page_size=100,
                base_body={},
                include_details=False,
            )

    def test_capture_stops_when_first_page_is_empty(self) -> None:
        client = FakeApiClient(
            {
                1: {
                    "pageInfo": {"totalPage": 3, "totalNumber": 5},
                    "rows": [],
                }
            }
        )
        strict_result = CaptureService(client, max_pages=10).capture(
            start=datetime(2026, 7, 1),
            end=datetime(2026, 7, 31),
            date_field="createTime",
            page_size=100,
            base_body={},
            include_details=False,
        )
        self.assertFalse(strict_result.statistics.total_consistent)

        result = CaptureService(client, max_pages=10, allow_partial=True).capture(
            start=datetime(2026, 7, 1),
            end=datetime(2026, 7, 31),
            date_field="createTime",
            page_size=100,
            base_body={},
            include_details=False,
        )
        self.assertEqual(result.statistics.pages_fetched, 1)
        self.assertTrue(result.statistics.empty_page_stopped)
        self.assertFalse(result.statistics.total_consistent)

    def test_detail_merge_preserves_list_values_and_rejects_missing_critical_fields(self) -> None:
        class DetailClient(FakeApiClient):
            def request_detail(self, record_id: str) -> dict:
                if record_id == "1":
                    return {"id": "1", "safetyCode": "", "createTime": None, "theme": "detail"}
                return {"id": "2", "theme": "missing code"}

        pages = {
            1: {
                "pageInfo": {"totalPage": 1, "totalNumber": 2},
                "rows": [
                    {
                        "id": "1",
                        "safetyCode": "A",
                        "createTime": "2026-07-10 10:00:00",
                        "theme": "list",
                    },
                    {"id": "2", "createTime": "2026-07-10 11:00:00"},
                ],
            }
        }
        result = CaptureService(DetailClient(pages)).capture(
            start=datetime(2026, 7, 1),
            end=datetime(2026, 7, 31, 23, 59, 59, 999999),
            date_field="createTime",
            page_size=100,
            base_body={},
            include_details=True,
        )
        self.assertEqual(result.details[0]["safetyCode"], "A")
        self.assertEqual(result.details[0]["createTime"], "2026-07-10 10:00:00")
        self.assertEqual(result.details[0]["theme"], "detail")
        self.assertEqual(result.statistics.details_failed, 1)
        self.assertIn("missing critical fields", result.failed_details[0]["_captureError"])

    def test_invalid_create_time_marks_batch_incomplete(self) -> None:
        pages = {
            1: {
                "pageInfo": {"totalPage": 1, "totalNumber": 1},
                "rows": [{"id": "1", "safetyCode": "A", "createTime": "invalid"}],
            }
        }
        result = CaptureService(FakeApiClient(pages)).capture(
            start=datetime(2026, 7, 1),
            end=datetime(2026, 7, 31, 23, 59, 59, 999999),
            date_field="createTime",
            page_size=100,
            base_body={},
            include_details=False,
        )
        self.assertEqual(result.statistics.invalid_date_rows, 1)
        self.assertTrue(result.statistics.incomplete_reasons)

    def test_recent_window_microseconds_reach_portal_request_body(self) -> None:
        selected = recent_range(now=datetime(2026, 7, 23, 9, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = Namespace(
                start=selected.start_text,
                end=selected.end_text,
                out=root,
                headers_file=root / "headers.json",
                body_file=None,
                token_source=root,
                page_size=100,
                max_pages=10,
                date_field="createTime",
                request_start_field="startTime",
                request_end_field="endTime",
                request_timeout=1,
                retries=1,
                retry_backoff=0,
                max_detail_failures=0,
                browser_bridge_url="",
                browser_bridge_token="",
                no_empty_page_stop=False,
                no_total_validation=False,
                allow_partial=False,
                details=False,
                verbose=False,
            )
            captured_body: dict = {}

            def capture(**kwargs):
                captured_body.update(kwargs["base_body"])
                return CaptureResult(
                    [],
                    [],
                    [],
                    kwargs["base_body"],
                    CaptureStatistics(total_consistent=True),
                )

            with patch(
                "data_acquisition.capture_daily_range.TokenProvider.resolve",
                return_value="test-token",
            ), patch(
                "data_acquisition.capture_daily_range.CaptureService.capture",
                side_effect=capture,
            ):
                run_capture(args, load_runtime_config())

        self.assertEqual(captured_body["startTime"], "2026-06-24 00:00:00")
        self.assertEqual(captured_body["endTime"], "2026-07-23 23:59:59.999999")


if __name__ == "__main__":
    unittest.main()
