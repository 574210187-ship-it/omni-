"""Private configuration loading for the end-to-end Omni Phase-1 worker.

The public repository deliberately contains only environment-variable references.
It must never contain a Feishu app secret, Bitable URL, app token, table ID, or
view ID.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse


EXPECTED_ARTIFACTS = (
    "videos_and_screenshot.zip",
    "supporting_materials.zip",
    "文案.txt",
)


class ConfigError(ValueError):
    """The private binding is absent, malformed, or unsafe to use."""


def _as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    return value


def _env_reference(value: Any, label: str, environ: Mapping[str, str]) -> str:
    """Resolve one required ${NAME} reference without literal secrets."""
    if not isinstance(value, str) or not (value.startswith("${") and value.endswith("}")):
        raise ConfigError(f"{label} must be an environment reference like ${{NAME}}")
    name = value[2:-1]
    if not name or any(not (char.isupper() or char.isdigit() or char == "_") for char in name):
        raise ConfigError(f"{label} has an invalid environment variable name")
    resolved = environ.get(name, "").strip()
    if not resolved:
        raise ConfigError(f"{label} is missing: set {name} in WorkBuddy secrets")
    return resolved


def _optional_env_reference(value: Any, label: str, environ: Mapping[str, str]) -> str | None:
    if value in (None, ""):
        return None
    return _env_reference(value, label, environ)


def _parse_bitable_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigError("FEISHU_BITABLE_URL must be an https Bitable URL")
    host = parsed.hostname.lower()
    if not (host.endswith(".feishu.cn") or host.endswith(".larksuite.com")):
        raise ConfigError("FEISHU_BITABLE_URL must point to Feishu or LarkSuite")
    parts = [part for part in parsed.path.split("/") if part]
    try:
        base_index = parts.index("base")
        app_token = parts[base_index + 1]
    except (ValueError, IndexError) as exc:
        raise ConfigError("FEISHU_BITABLE_URL is missing /base/<app_token>") from exc
    query = parse_qs(parsed.query)
    table_id = (query.get("table") or query.get("table_id") or [""])[0]
    view_id = (query.get("view") or query.get("view_id") or [""])[0]
    if not (app_token and table_id and view_id):
        raise ConfigError(
            "FEISHU_BITABLE_URL must include table and view query parameters; "
            "do not let the worker guess a table or view"
        )
    return app_token, table_id, view_id


@dataclass(frozen=True)
class FieldNames:
    category: str
    input_attachments: str
    output_attachments: str
    audit: str
    status: str
    run_id: str
    lease_until: str
    input_sha256: str
    evidence: str

    @property
    def worker_text_fields(self) -> tuple[str, ...]:
        return (
            self.audit,
            self.status,
            self.run_id,
            self.lease_until,
            self.input_sha256,
            self.evidence,
        )


@dataclass(frozen=True)
class FeishuBinding:
    app_id: str
    app_secret: str
    app_token: str
    table_id: str
    view_id: str
    api_base: str


@dataclass(frozen=True)
class WorkerConfig:
    binding: FeishuBinding
    fields: FieldNames
    expected_view_name: str
    category_value: str
    lease_seconds: int
    max_input_bytes: int
    work_root: Path
    config_path: Path

    @property
    def artifacts(self) -> tuple[str, ...]:
        return EXPECTED_ARTIFACTS


def _required_string(data: Mapping[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def load_config(path: str | Path, environ: Mapping[str, str] | None = None) -> WorkerConfig:
    """Load a JSON job file and resolve its private values from environment only."""
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"private job config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"job config is not valid JSON: {exc.msg}") from exc
    root = _as_dict(raw, "root")
    if root.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    env = os.environ if environ is None else environ
    credentials = _as_dict(root.get("credentials"), "credentials")
    target = _as_dict(root.get("target"), "target")
    fields_raw = _as_dict(root.get("fields"), "fields")
    queue = _as_dict(root.get("queue"), "queue")
    runtime = _as_dict(root.get("runtime"), "runtime")

    app_id = _env_reference(credentials.get("app_id"), "credentials.app_id", env)
    app_secret = _env_reference(credentials.get("app_secret"), "credentials.app_secret", env)
    api_base = str(credentials.get("api_base", "https://open.feishu.cn")).rstrip("/")
    parsed_api = urlparse(api_base)
    if parsed_api.scheme != "https" or not parsed_api.netloc:
        raise ConfigError("credentials.api_base must be an https URL")

    bitable_url = _optional_env_reference(target.get("bitable_url"), "target.bitable_url", env)
    if bitable_url:
        app_token, table_id, view_id = _parse_bitable_url(bitable_url)
    else:
        app_token = _env_reference(target.get("app_token"), "target.app_token", env)
        table_id = _env_reference(target.get("table_id"), "target.table_id", env)
        view_id = _env_reference(target.get("view_id"), "target.view_id", env)

    fields = FieldNames(
        category=_required_string(fields_raw, "category", "fields"),
        input_attachments=_required_string(fields_raw, "input_attachments", "fields"),
        output_attachments=_required_string(fields_raw, "output_attachments", "fields"),
        audit=_required_string(fields_raw, "audit", "fields"),
        status=_required_string(fields_raw, "status", "fields"),
        run_id=_required_string(fields_raw, "run_id", "fields"),
        lease_until=_required_string(fields_raw, "lease_until", "fields"),
        input_sha256=_required_string(fields_raw, "input_sha256", "fields"),
        evidence=_required_string(fields_raw, "evidence", "fields"),
    )
    expected_view_name = _required_string(target, "expected_view_name", "target")
    category_value = _required_string(queue, "category_value", "queue")
    lease_seconds = queue.get("lease_seconds", 7200)
    max_input_bytes = queue.get("max_input_bytes", 2_000_000_000)
    if not isinstance(lease_seconds, int) or not 300 <= lease_seconds <= 86_400:
        raise ConfigError("queue.lease_seconds must be an integer from 300 to 86400")
    if not isinstance(max_input_bytes, int) or not 1 <= max_input_bytes <= 10_000_000_000:
        raise ConfigError("queue.max_input_bytes must be a positive integer no greater than 10GB")
    work_root_value = _required_string(runtime, "work_root", "runtime")
    work_root = (config_path.parent / work_root_value).resolve()
    return WorkerConfig(
        binding=FeishuBinding(
            app_id=app_id,
            app_secret=app_secret,
            app_token=app_token,
            table_id=table_id,
            view_id=view_id,
            api_base=api_base,
        ),
        fields=fields,
        expected_view_name=expected_view_name,
        category_value=category_value,
        lease_seconds=lease_seconds,
        max_input_bytes=max_input_bytes,
        work_root=work_root,
        config_path=config_path,
    )
