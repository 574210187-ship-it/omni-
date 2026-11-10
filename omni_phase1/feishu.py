"""Small, auditable Feishu REST adapter used by the Omni Phase-1 worker.

The adapter intentionally uses only documented REST endpoints. It neither
scrapes the Feishu web UI nor stores credentials on disk.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Iterator
from urllib.parse import urlparse
import zlib

import requests

from .config import FeishuBinding


RETRYABLE_CODES = {
    1254002,  # temporary generic failure
    1254036,  # base is copying
    1254290,  # too many requests
    1254291,  # write conflict
    1254607,  # data not ready
    1255001,
    1255002,
    1255040,
    1061045,  # media upload rate control
}
MAX_SINGLE_UPLOAD_BYTES = 20 * 1024 * 1024


@dataclass
class FeishuAPIError(RuntimeError):
    message: str
    status_code: int | None = None
    code: int | None = None
    request_id: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        bits = [self.message]
        if self.code is not None:
            bits.append(f"code={self.code}")
        if self.status_code is not None:
            bits.append(f"http={self.status_code}")
        if self.request_id:
            bits.append(f"request_id={self.request_id}")
        return " | ".join(bits)


def _request_id(response: requests.Response) -> str | None:
    return response.headers.get("X-Request-Id") or response.headers.get("Request-Id")


class FeishuClient:
    """Feishu Bitable + media adapter with bounded retries and TLS verification."""

    def __init__(
        self,
        binding: FeishuBinding,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (15.0, 120.0),
        max_attempts: int = 4,
        sleep=time.sleep,
    ) -> None:
        self.binding = binding
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _url(self, path: str) -> str:
        return self.binding.api_base + path

    def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        data = self._request_json(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json_body={
                "app_id": self.binding.app_id,
                "app_secret": self.binding.app_secret,
            },
            authenticated=False,
            return_envelope=True,
        )
        token = data.get("tenant_access_token")
        expiry = data.get("expire")
        if not isinstance(token, str) or not token or not isinstance(expiry, int):
            raise FeishuAPIError("token response is missing tenant_access_token or expiry")
        self._token = token
        self._token_expires_at = time.time() + max(1, expiry - 60)
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tenant_token()}"}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
        return_envelope: bool = False,
    ) -> dict[str, Any]:
        """Issue a JSON request, retrying only transient API or transport errors."""
        last_error: FeishuAPIError | None = None
        for attempt in range(self.max_attempts):
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if authenticated:
                headers.update(self._headers())
            try:
                response = self.session.request(
                    method,
                    self._url(path),
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = FeishuAPIError(
                    f"transport error calling {path}: {type(exc).__name__}",
                    retryable=True,
                )
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                code = body.get("code") if isinstance(body, dict) else None
                request_id = _request_id(response)
                if 200 <= response.status_code < 300 and code == 0:
                    if return_envelope:
                        return body
                    data = body.get("data", {})
                    return data if isinstance(data, dict) else {}
                message = body.get("msg") if isinstance(body, dict) else None
                retryable = response.status_code in {429, 500, 502, 503, 504} or code in RETRYABLE_CODES
                last_error = FeishuAPIError(
                    message or f"Feishu request failed for {path}",
                    status_code=response.status_code,
                    code=code if isinstance(code, int) else None,
                    request_id=request_id,
                    retryable=retryable,
                )
            if not last_error.retryable or attempt + 1 >= self.max_attempts:
                raise last_error
            self.sleep(min(8.0, 0.5 * (2**attempt)))
        raise last_error or FeishuAPIError(f"unknown request failure for {path}")

    def _upload_multipart(
        self,
        path: str,
        *,
        data: dict[str, str],
        file_path: Path,
    ) -> dict[str, Any]:
        """Upload a multipart request; reopen the file for each retry."""
        last_error: FeishuAPIError | None = None
        for attempt in range(self.max_attempts):
            try:
                with file_path.open("rb") as handle:
                    response = self.session.post(
                        self._url(path),
                        data=data,
                        files={"file": (file_path.name, handle, "application/octet-stream")},
                        headers=self._headers(),
                        timeout=self.timeout,
                    )
            except (OSError, requests.RequestException) as exc:
                last_error = FeishuAPIError(
                    f"multipart transport error calling {path}: {type(exc).__name__}",
                    retryable=True,
                )
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                code = body.get("code") if isinstance(body, dict) else None
                if 200 <= response.status_code < 300 and code == 0:
                    data_out = body.get("data", {})
                    return data_out if isinstance(data_out, dict) else {}
                last_error = FeishuAPIError(
                    body.get("msg", f"Feishu upload failed for {path}") if isinstance(body, dict) else f"Feishu upload failed for {path}",
                    status_code=response.status_code,
                    code=code if isinstance(code, int) else None,
                    request_id=_request_id(response),
                    retryable=response.status_code in {429, 500, 502, 503, 504} or code in RETRYABLE_CODES,
                )
            if not last_error.retryable or attempt + 1 >= self.max_attempts:
                raise last_error
            self.sleep(min(8.0, 0.5 * (2**attempt)))
        raise last_error or FeishuAPIError(f"unknown multipart failure for {path}")

    def list_views(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET",
                f"/open-apis/bitable/v1/apps/{self.binding.app_token}/tables/{self.binding.table_id}/views",
                params=params,
            )
            result.extend(item for item in data.get("items", []) if isinstance(item, dict))
            if not data.get("has_more"):
                return result
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                raise FeishuAPIError("view pagination said has_more without a page_token")

    def list_fields(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET",
                f"/open-apis/bitable/v1/apps/{self.binding.app_token}/tables/{self.binding.table_id}/fields",
                params=params,
            )
            result.extend(item for item in data.get("items", []) if isinstance(item, dict))
            if not data.get("has_more"):
                return result
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                raise FeishuAPIError("field pagination said has_more without a page_token")

    def create_text_field(self, field_name: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.binding.app_token}/tables/{self.binding.table_id}/fields",
            json_body={"field_name": field_name, "type": 1},
        ).get("field", {})

    def iter_records(self) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "view_id": self.binding.view_id,
                "page_size": 100,
                "text_field_as_array": "false",
            }
            if page_token:
                params["page_token"] = page_token
            data = self._request_json(
                "GET",
                f"/open-apis/bitable/v1/apps/{self.binding.app_token}/tables/{self.binding.table_id}/records",
                params=params,
            )
            for item in data.get("items", []):
                if isinstance(item, dict):
                    yield item
            if not data.get("has_more"):
                return
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                raise FeishuAPIError("record pagination said has_more without a page_token")

    def get_record(self, record_id: str) -> dict[str, Any]:
        data = self._request_json(
            "GET",
            f"/open-apis/bitable/v1/apps/{self.binding.app_token}/tables/{self.binding.table_id}/records/{record_id}",
            params={"text_field_as_array": "false"},
        )
        record = data.get("record")
        if not isinstance(record, dict):
            raise FeishuAPIError("record readback did not contain a record")
        return record

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        data = self._request_json(
            "PUT",
            f"/open-apis/bitable/v1/apps/{self.binding.app_token}/tables/{self.binding.table_id}/records/{record_id}",
            json_body={"fields": fields},
        )
        record = data.get("record")
        if not isinstance(record, dict):
            raise FeishuAPIError("record update did not return a record")
        return record

    def _media_extra(self) -> str:
        return json.dumps({"drive_route_token": self.binding.app_token}, separators=(",", ":"))

    def upload_attachment(self, file_path: str | Path) -> str:
        source = Path(file_path)
        if not source.is_file() or source.stat().st_size <= 0:
            raise FeishuAPIError(f"cannot upload missing or empty artifact: {source.name}")
        if source.stat().st_size <= MAX_SINGLE_UPLOAD_BYTES:
            return self._upload_small(source)
        return self._upload_chunked(source)

    def _upload_small(self, source: Path) -> str:
        data = self._upload_multipart(
            "/open-apis/drive/v1/medias/upload_all",
            data={
                "file_name": source.name,
                "parent_type": "bitable_file",
                "parent_node": self.binding.app_token,
                "size": str(source.stat().st_size),
                "extra": self._media_extra(),
            },
            file_path=source,
        )
        token = data.get("file_token")
        if not isinstance(token, str) or not token:
            raise FeishuAPIError("upload_all response did not contain file_token")
        return token

    def _upload_chunked(self, source: Path) -> str:
        prepared = self._request_json(
            "POST",
            "/open-apis/drive/v1/medias/upload_prepare",
            json_body={
                "file_name": source.name,
                "parent_type": "bitable_file",
                "parent_node": self.binding.app_token,
                "size": source.stat().st_size,
                "extra": self._media_extra(),
            },
        )
        upload_id = prepared.get("upload_id")
        block_size = prepared.get("block_size")
        block_num = prepared.get("block_num")
        if not isinstance(upload_id, str) or not upload_id:
            raise FeishuAPIError("upload_prepare response did not contain upload_id")
        if not isinstance(block_size, int) or block_size <= 0:
            raise FeishuAPIError("upload_prepare response did not contain a valid block_size")
        if not isinstance(block_num, int) or block_num <= 0:
            raise FeishuAPIError("upload_prepare response did not contain a valid block_num")
        with source.open("rb") as handle:
            for sequence in range(block_num):
                chunk = handle.read(block_size)
                if not chunk:
                    raise FeishuAPIError("upload_prepare block count exceeds local file bytes")
                chunk_path = source.parent / f".{source.name}.part-{sequence}"
                try:
                    chunk_path.write_bytes(chunk)
                    self._upload_multipart(
                        "/open-apis/drive/v1/medias/upload_part",
                        data={
                            "upload_id": upload_id,
                            "seq": str(sequence),
                            "size": str(len(chunk)),
                            "checksum": str(zlib.adler32(chunk) & 0xFFFFFFFF),
                        },
                        file_path=chunk_path,
                    )
                finally:
                    chunk_path.unlink(missing_ok=True)
            if handle.read(1):
                raise FeishuAPIError("upload_prepare block count is smaller than local file bytes")
        finished = self._request_json(
            "POST",
            "/open-apis/drive/v1/medias/upload_finish",
            json_body={"upload_id": upload_id, "block_num": block_num},
        )
        token = finished.get("file_token")
        if not isinstance(token, str) or not token:
            raise FeishuAPIError("upload_finish response did not contain file_token")
        return token

    def download_attachment(
        self,
        attachment: dict[str, Any],
        *,
        field_id: str,
        record_id: str,
        destination: str | Path,
    ) -> Path:
        """Download an exact Bitable attachment and atomically materialize it locally."""
        token = attachment.get("file_token")
        if not isinstance(token, str) or not token:
            raise FeishuAPIError("attachment is missing file_token")
        url = attachment.get("url")
        if isinstance(url, str) and url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != urlparse(self.binding.api_base).hostname:
                raise FeishuAPIError("attachment URL is not a Feishu API URL")
            request_url = url
            params = None
        else:
            request_url = self._url(f"/open-apis/drive/v1/medias/{token}/download")
            extra = {
                "bitablePerm": {
                    "tableId": self.binding.table_id,
                    "attachments": {field_id: {record_id: [token]}},
                }
            }
            params = {"extra": json.dumps(extra, separators=(",", ":"))}
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name("." + target.name + ".partial")
        last_error: FeishuAPIError | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self.session.get(
                    request_url,
                    params=params,
                    headers=self._headers(),
                    timeout=self.timeout,
                    stream=True,
                    allow_redirects=False,
                )
                if response.status_code == 200:
                    with partial.open("wb") as handle:
                        for block in response.iter_content(chunk_size=1024 * 1024):
                            if block:
                                handle.write(block)
                    partial.replace(target)
                    return target
                code = None
                message = None
                try:
                    body = response.json()
                    code = body.get("code")
                    message = body.get("msg")
                except ValueError:
                    pass
                last_error = FeishuAPIError(
                    message or "attachment download failed",
                    status_code=response.status_code,
                    code=code if isinstance(code, int) else None,
                    request_id=_request_id(response),
                    retryable=response.status_code in {429, 500, 502, 503, 504} or code in RETRYABLE_CODES,
                )
            except (OSError, requests.RequestException) as exc:
                last_error = FeishuAPIError(
                    f"attachment download transport error: {type(exc).__name__}",
                    retryable=True,
                )
            finally:
                partial.unlink(missing_ok=True)
            if not last_error.retryable or attempt + 1 >= self.max_attempts:
                raise last_error
            self.sleep(min(8.0, 0.5 * (2**attempt)))
        raise last_error or FeishuAPIError("attachment download failed")
