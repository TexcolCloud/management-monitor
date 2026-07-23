from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlencode


AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
IMAGE_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"
FILE_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/files"
MAX_FIELD_LENGTH = 1000
SUPPORTED_RECEIVE_ID_TYPES = frozenset({"chat_id", "open_id", "user_id", "email"})
RETRYABLE_HTTP_STATUSES = frozenset({408, 429})
MAX_RETRY_ATTEMPTS = 10


def _is_retryable_status(code: int) -> bool:
    return code in RETRYABLE_HTTP_STATUSES or 500 <= code <= 599


class FeishuApiError(RuntimeError):
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


class FeishuRetryableError(FeishuApiError):
    retryable = True


class FeishuPermanentError(FeishuApiError):
    pass


@dataclass(frozen=True)
class FeishuRetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 4.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= MAX_RETRY_ATTEMPTS:
            raise ValueError(f"FEISHU_APP_MAX_ATTEMPTS 必须在 1 到 {MAX_RETRY_ATTEMPTS} 之间。")
        if self.backoff_seconds < 0:
            raise ValueError("FEISHU_APP_RETRY_BACKOFF_SECONDS 不能小于 0。")
        if self.max_backoff_seconds < 0:
            raise ValueError("FEISHU_APP_RETRY_MAX_BACKOFF_SECONDS 不能小于 0。")

    @classmethod
    def from_environment(cls) -> "FeishuRetryPolicy":
        try:
            max_attempts = int(os.environ.get("FEISHU_APP_MAX_ATTEMPTS", "3").strip())
            backoff_seconds = float(
                os.environ.get("FEISHU_APP_RETRY_BACKOFF_SECONDS", "0.5").strip()
            )
            max_backoff_seconds = float(
                os.environ.get("FEISHU_APP_RETRY_MAX_BACKOFF_SECONDS", "4").strip()
            )
        except ValueError as exc:
            raise ValueError("飞书重试配置必须为数字。") from exc
        return cls(max_attempts, backoff_seconds, max_backoff_seconds)

    def delay_after(self, failed_attempt: int) -> float:
        return min(
            self.max_backoff_seconds,
            self.backoff_seconds * (2 ** max(0, failed_attempt - 1)),
        )


def _text(value: Any) -> str:
    text = str(value or "").strip()
    return text[:MAX_FIELD_LENGTH] + "..." if len(text) > MAX_FIELD_LENGTH else text


@dataclass
class FeishuOutboundBot:
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    receive_id: str = field(default="", repr=False)
    timeout_seconds: float = 10.0
    receive_id_type: str = "chat_id"
    retry_policy: FeishuRetryPolicy = field(default_factory=FeishuRetryPolicy)
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False, compare=False)
    _tenant_access_token: str = field(default="", init=False, repr=False)
    _token_expires_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("FEISHU_APP_TIMEOUT_SECONDS 必须大于 0。")

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret and self.receive_id)

    @classmethod
    def from_environment(cls) -> "FeishuOutboundBot":
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        receive_id = os.environ.get("FEISHU_RECEIVE_ID", "").strip()
        if not receive_id:
            receive_id = os.environ.get("FEISHU_CHAT_ID", "").strip()
        receive_id_type = os.environ.get("FEISHU_RECEIVE_ID_TYPE", "chat_id").strip().lower()
        timeout_text = os.environ.get("FEISHU_APP_TIMEOUT_SECONDS", "10").strip()
        try:
            timeout_seconds = float(timeout_text)
        except ValueError as exc:
            raise ValueError("FEISHU_APP_TIMEOUT_SECONDS 必须为数字。") from exc
        if timeout_seconds <= 0:
            raise ValueError("FEISHU_APP_TIMEOUT_SECONDS 必须大于 0。")
        if bool(app_id) != bool(app_secret):
            raise ValueError("FEISHU_APP_ID 和 FEISHU_APP_SECRET 必须同时配置。")
        if receive_id_type not in SUPPORTED_RECEIVE_ID_TYPES:
            supported = ", ".join(sorted(SUPPORTED_RECEIVE_ID_TYPES))
            raise ValueError(f"FEISHU_RECEIVE_ID_TYPE 必须为以下值之一：{supported}。")
        return cls(
            app_id,
            app_secret,
            receive_id,
            timeout_seconds,
            receive_id_type,
            FeishuRetryPolicy.from_environment(),
        )

    def _message_url(self) -> str:
        return f"{MESSAGE_URL}?{urlencode({'receive_id_type': self.receive_id_type})}"

    def _request_json(
        self,
        url: str,
        body: Mapping[str, Any],
        authorization: str = "",
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json;charset=utf-8"}
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                return self._open_json_once(request)
            except FeishuRetryableError:
                if attempt >= self.retry_policy.max_attempts:
                    raise
                self._sleep(self.retry_policy.delay_after(attempt))
        raise AssertionError("unreachable")

    def _open_json_once(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_bytes = response.read()
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            exc.close()
            error_type = (
                FeishuRetryableError
                if _is_retryable_status(status_code)
                else FeishuPermanentError
            )
            raise error_type(
                f"飞书 API HTTP 请求失败（status={status_code}）。",
                status_code=status_code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise FeishuRetryableError("飞书 API 网络请求失败。") from None
        try:
            response_body = response_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise FeishuRetryableError("飞书 API 返回了无法解码的数据。") from None
        try:
            result = json.loads(response_body)
        except json.JSONDecodeError:
            raise FeishuRetryableError("飞书 API 返回了无效的 JSON 数据。") from None
        if not isinstance(result, dict):
            raise FeishuPermanentError("飞书 API 返回的数据结构无效。")
        try:
            api_code = int(result.get("code", -1))
        except (TypeError, ValueError):
            raise FeishuPermanentError("飞书 API 返回的业务状态码无效。") from None
        if api_code != 0:
            error_type = (
                FeishuRetryableError
                if _is_retryable_status(api_code)
                else FeishuPermanentError
            )
            raise error_type(
                f"飞书 API 业务请求失败（code={api_code}）。",
                api_code=api_code,
            )
        return result

    def _access_token(self) -> str:
        if not self.app_id or not self.app_secret:
            raise FeishuPermanentError("未配置飞书应用凭证。")
        if self._tenant_access_token and time.time() < self._token_expires_at:
            return self._tenant_access_token
        result = self._request_json(AUTH_URL, {"app_id": self.app_id, "app_secret": self.app_secret})
        token = str(result.get("tenant_access_token") or "")
        try:
            expires_in = int(result.get("expire", 0))
        except (TypeError, ValueError):
            raise FeishuPermanentError("飞书 tenant_access_token 的有效期无效。") from None
        if not token or expires_in <= 60:
            raise FeishuPermanentError("飞书 tenant_access_token 返回数据不完整。")
        self._tenant_access_token = token
        self._token_expires_at = time.time() + expires_in - 60
        return token

    def _send_message(self, msg_type: str, content: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        self._request_json(
            self._message_url(),
            {
                "receive_id": self.receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            authorization=f"Bearer {self._access_token()}",
        )

    def _upload_media(
        self,
        url: str,
        file_path: str,
        field_name: str,
        fields: Mapping[str, str],
    ) -> dict[str, Any]:
        token = self._access_token()
        boundary = f"----management-monitorFeishu{uuid.uuid4().hex}"
        filename = os.path.basename(file_path)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            with open(file_path, "rb") as file:
                content = file.read()
        except OSError:
            raise FeishuPermanentError("飞书待上传文件无法读取。") from None
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        parts.extend(
            [
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                    f"Content-Type: {mime_type}\r\n\r\n"
                ).encode("utf-8"),
                content,
                f"\r\n--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        request = urllib.request.Request(
            url,
            data=b"".join(parts),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        result = self._open_json(request)
        data = result.get("data")
        if not isinstance(data, dict):
            raise FeishuPermanentError("飞书媒体上传返回数据不完整。")
        return data

    def upload_image(self, file_path: str) -> str:
        data = self._upload_media(IMAGE_UPLOAD_URL, file_path, "image", {"image_type": "message"})
        image_key = str(data.get("image_key") or "")
        if not image_key:
            raise FeishuPermanentError("飞书图片上传返回数据不完整。")
        return image_key

    def upload_file(self, file_path: str) -> str:
        filename = os.path.basename(file_path)
        data = self._upload_media(
            FILE_UPLOAD_URL,
            file_path,
            "file",
            {"file_type": "stream", "file_name": filename},
        )
        file_key = str(data.get("file_key") or "")
        if not file_key:
            raise FeishuPermanentError("飞书文件上传返回数据不完整。")
        return file_key

    @staticmethod
    def work_order_text(row: Mapping[str, Any]) -> str:
        return "\n".join(
            [
                f"【工单编号】 {_text(row.get('safetyCode')) or '-'}",
                f"【所属班组】 {_text(row.get('companyName')) or '-'}",
                f"【工单主题】 {_text(row.get('theme')) or '-'}",
                f"【创建时间】 {_text(row.get('createTime')) or '-'}",
            ]
        )

    def send_work_order(self, row: Mapping[str, Any]) -> None:
        self._send_message("text", {"text": self.work_order_text(row)})

    def send_interactive_card(self, card: Mapping[str, Any]) -> None:
        self._send_message("interactive", card)

    def send_text(self, text: str) -> None:
        self._send_message("text", {"text": _text(text)})

    def send_image(self, file_path: str) -> None:
        self._send_message("image", {"image_key": self.upload_image(file_path)})

    def send_file(self, file_path: str) -> None:
        self._send_message("file", {"file_key": self.upload_file(file_path)})
