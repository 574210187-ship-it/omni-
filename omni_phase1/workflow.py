"""Queue state machine for the end-to-end Omni Phase-1 Bitable worker."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable
from uuid import uuid4

from clipflow.delivery import sha256

from .config import EXPECTED_ARTIFACTS, WorkerConfig
from .feishu import FeishuAPIError, FeishuClient


ACTIVE_STATES = {"CLAIMED", "VALIDATING", "RUNNING", "UPLOADING", "VERIFYING"}
READY_STATES = {"", "QUEUED", "RETRYABLE"}
TERMINAL_STATES = {"COMPLETED", "HOLD"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LockLost(WorkflowError):
    def __init__(self, message: str = "remote record lease is no longer owned by this run") -> None:
        super().__init__("LOCK_LOST", message)


class HoldRequired(WorkflowError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


@dataclass(frozen=True)
class RecordDecision:
    record_id: str
    kind: str
    reason: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _field_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            if isinstance(value.get(key), str):
                return [value[key]]
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_field_values(item))
        return result
    return []


def field_text(value: Any) -> str:
    return " ".join(part.strip() for part in _field_values(value) if part.strip())


def attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def attachment_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or "")


def attachment_token(item: dict[str, Any]) -> str:
    return str(item.get("file_token") or "")


def has_exact_artifacts(items: Iterable[dict[str, Any]]) -> bool:
    names = [attachment_name(item) for item in items]
    counts = Counter(names)
    return all(counts[name] == 1 for name in EXPECTED_ARTIFACTS)


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:100] or "record"


def classify_record(config: WorkerConfig, record: dict[str, Any], now: datetime | None = None) -> RecordDecision:
    """Classify one visible row without changing it."""
    now = now or utc_now()
    record_id = str(record.get("record_id") or "")
    if not record_id:
        return RecordDecision("", "INVALID", "record has no record_id")
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return RecordDecision(record_id, "INVALID", "record has no fields")
    if config.category_value not in _field_values(fields.get(config.fields.category)):
        return RecordDecision(record_id, "OUT_OF_SCOPE", "category is not 剧情")
    inputs = attachments(fields.get(config.fields.input_attachments))
    outputs = attachments(fields.get(config.fields.output_attachments))
    status = field_text(fields.get(config.fields.status)).strip()
    lease = _parse_time(field_text(fields.get(config.fields.lease_until)).strip())
    if has_exact_artifacts(outputs):
        if status == "COMPLETED":
            return RecordDecision(record_id, "COMPLETE", "completed artifacts already present")
        if status in ACTIVE_STATES | {"RETRYABLE"}:
            if status in ACTIVE_STATES and lease and lease > now:
                return RecordDecision(record_id, "BUSY", "another non-expired run owns verification")
            return RecordDecision(record_id, "VERIFY_RESUME", "all artifacts exist but completion was interrupted")
        return RecordDecision(record_id, "EXISTING_COMPLETE", "legacy complete artifacts; worker will not rewrite history")
    if status == "COMPLETED":
        return RecordDecision(record_id, "INCONSISTENT", "status is COMPLETED but exact artifacts are absent")
    if status == "HOLD":
        return RecordDecision(record_id, "HOLD", "row has a prior evidence HOLD")
    if status == "RETRYABLE":
        return RecordDecision(record_id, "RESUME", "previous run is explicitly retryable")
    if status in ACTIVE_STATES:
        if lease and lease > now:
            return RecordDecision(record_id, "BUSY", "another non-expired run owns this row")
        return RecordDecision(record_id, "RESUME", "previous lease expired")
    if status not in READY_STATES:
        return RecordDecision(record_id, "UNSUPPORTED", f"unrecognized worker status: {status}")
    if outputs:
        return RecordDecision(record_id, "PARTIAL_HOLD", "output field has partial or foreign attachments")
    if not inputs:
        return RecordDecision(record_id, "INPUT_HOLD", "input attachment field is empty")
    return RecordDecision(record_id, "READY", "uncompleted 剧情 row with input and empty output")


class LocalWorkerLock:
    """Guard one worker process per local work root; remote lease guards the table."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".worker.lock"
        self._fd: int | None = None

    def __enter__(self) -> "LocalWorkerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkflowError(
                "LOCAL_WORKER_ALREADY_RUNNING",
                f"local worker lock exists at {self.path}; do not run a second drain process",
            ) from exc
        os.write(self._fd, json.dumps({"pid": os.getpid(), "created_at": iso_time(utc_now())}).encode("utf-8"))
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)


class OmniWorker:
    """Stateful coordinator. Every destructive operation requires an explicit caller."""

    def __init__(self, config: WorkerConfig, client: FeishuClient) -> None:
        self.config = config
        self.client = client
        self.field_ids: dict[str, str] = {}

    def validate_binding(self, *, allow_missing_worker_fields: bool = False) -> dict[str, Any]:
        """Read-only schema/view validation. It prevents the historical 聊天-view mistake."""
        views = self.client.list_views()
        selected = [
            item for item in views
            if item.get("view_id") == self.config.binding.view_id
            and item.get("view_name") == self.config.expected_view_name
        ]
        if len(selected) != 1:
            raise WorkflowError(
                "WRONG_VIEW_BINDING",
                f"configured view must exist and be named {self.config.expected_view_name!r}; it was not found",
            )
        fields = self.client.list_fields()
        by_name = {
            str(item.get("field_name")): item
            for item in fields
            if isinstance(item.get("field_name"), str)
        }
        required_business = (
            self.config.fields.category,
            self.config.fields.input_attachments,
            self.config.fields.output_attachments,
        )
        missing_business = [name for name in required_business if name not in by_name]
        if missing_business:
            raise WorkflowError("MISSING_BUSINESS_FIELDS", "missing: " + ", ".join(missing_business))
        for name in (self.config.fields.input_attachments, self.config.fields.output_attachments):
            if by_name[name].get("type") != 17:
                raise WorkflowError("WRONG_ATTACHMENT_FIELD", f"{name} must be an attachment field")
        missing_worker = [name for name in self.config.fields.worker_text_fields if name not in by_name]
        if missing_worker and not allow_missing_worker_fields:
            raise WorkflowError(
                "MISSING_WORKER_FIELDS",
                "run omni-phase1 bootstrap --apply once; missing: " + ", ".join(missing_worker),
            )
        for name in self.config.fields.worker_text_fields:
            if name in by_name and by_name[name].get("type") != 1:
                raise WorkflowError("WRONG_WORKER_FIELD_TYPE", f"{name} must be a text field")
        self.field_ids = {
            name: str(item.get("field_id"))
            for name, item in by_name.items()
            if isinstance(item.get("field_id"), str)
        }
        return {
            "view_id": self.config.binding.view_id,
            "view_name": self.config.expected_view_name,
            "missing_worker_fields": missing_worker,
            "field_count": len(by_name),
        }

    def bootstrap(self, *, apply: bool) -> dict[str, Any]:
        """Create only the dedicated text fields; never edits records."""
        evidence = self.validate_binding(allow_missing_worker_fields=True)
        missing = list(evidence["missing_worker_fields"])
        created: list[str] = []
        if apply:
            for name in missing:
                self.client.create_text_field(name)
                created.append(name)
            final = self.validate_binding(allow_missing_worker_fields=False)
        else:
            final = evidence
        return {
            "status": "APPLIED" if apply else "DRY_RUN",
            "created_fields": created,
            "would_create_fields": [] if apply else missing,
            "validation": final,
        }

    def plan(self) -> dict[str, Any]:
        self.validate_binding()
        decisions = [classify_record(self.config, record) for record in self.client.iter_records()]
        counts = Counter(decision.kind for decision in decisions)
        return {
            "status": "PLAN",
            "queue": [
                {"record_id": decision.record_id, "kind": decision.kind, "reason": decision.reason}
                for decision in decisions
                if decision.kind not in {"OUT_OF_SCOPE", "COMPLETE", "EXISTING_COMPLETE"}
            ],
            "counts": dict(sorted(counts.items())),
        }

    def _record_fields(self, record: dict[str, Any]) -> dict[str, Any]:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            raise WorkflowError("MALFORMED_RECORD", "record does not contain a field map")
        return fields

    def _audit_value(self, current: Any, payload: dict[str, Any]) -> str:
        prior = field_text(current).strip()
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if prior:
            combined = prior + "\n" + line
        else:
            combined = line
        return combined[-15_000:]

    def _lease(self) -> str:
        return iso_time(utc_now() + timedelta(seconds=self.config.lease_seconds))

    def _assert_owned(self, record: dict[str, Any], run_id: str) -> None:
        fields = self._record_fields(record)
        if field_text(fields.get(self.config.fields.run_id)).strip() != run_id:
            raise LockLost()
        lease = _parse_time(field_text(fields.get(self.config.fields.lease_until)).strip())
        if not lease or lease <= utc_now():
            raise LockLost("remote lease is expired")

    def _claim(self, record_id: str, run_id: str) -> dict[str, Any]:
        current = self.client.get_record(record_id)
        decision = classify_record(self.config, current)
        if decision.kind not in {"READY", "RESUME", "VERIFY_RESUME", "INPUT_HOLD", "PARTIAL_HOLD", "INCONSISTENT"}:
            raise WorkflowError("ROW_NOT_CLAIMABLE", f"{decision.kind}: {decision.reason}")
        fields = self._record_fields(current)
        update = {
            self.config.fields.status: "CLAIMED",
            self.config.fields.run_id: run_id,
            self.config.fields.lease_until: self._lease(),
            self.config.fields.audit: self._audit_value(
                fields.get(self.config.fields.audit),
                {"at": iso_time(utc_now()), "run_id": run_id, "phase": "CLAIMED"},
            ),
        }
        self.client.update_record(record_id, update)
        claimed = self.client.get_record(record_id)
        self._assert_owned(claimed, run_id)
        if field_text(self._record_fields(claimed).get(self.config.fields.status)).strip() != "CLAIMED":
            raise LockLost("claim status did not read back")
        return claimed

    def _transition(
        self,
        record_id: str,
        run_id: str,
        phase: str,
        *,
        input_sha256: str | None = None,
        evidence: dict[str, Any] | None = None,
        terminal: bool = False,
    ) -> dict[str, Any]:
        current = self.client.get_record(record_id)
        self._assert_owned(current, run_id)
        fields = self._record_fields(current)
        audit_payload: dict[str, Any] = {"at": iso_time(utc_now()), "run_id": run_id, "phase": phase}
        if evidence:
            audit_payload["evidence"] = evidence
        update: dict[str, Any] = {
            self.config.fields.status: phase,
            self.config.fields.lease_until: "" if terminal else self._lease(),
            self.config.fields.audit: self._audit_value(fields.get(self.config.fields.audit), audit_payload),
        }
        if input_sha256:
            update[self.config.fields.input_sha256] = input_sha256
        if evidence:
            update[self.config.fields.evidence] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        self.client.update_record(record_id, update)
        readback = self.client.get_record(record_id)
        readback_fields = self._record_fields(readback)
        if field_text(readback_fields.get(self.config.fields.run_id)).strip() != run_id:
            raise LockLost()
        if field_text(readback_fields.get(self.config.fields.status)).strip() != phase:
            raise WorkflowError("PHASE_READBACK_MISMATCH", f"phase {phase} did not read back")
        return readback

    def _select_source(self, record: dict[str, Any]) -> dict[str, Any]:
        items = attachments(self._record_fields(record).get(self.config.fields.input_attachments))
        videos = [
            item for item in items
            if str(item.get("type") or "").startswith("video/")
            or Path(attachment_name(item)).suffix.lower() in VIDEO_SUFFIXES
        ]
        if len(videos) != 1:
            raise HoldRequired(
                "INPUT_VIDEO_AMBIGUOUS",
                f"expected exactly one video attachment in {self.config.fields.input_attachments}; found {len(videos)}",
            )
        return videos[0]

    def _record_dir(self, record_id: str, run_id: str) -> Path:
        path = self.config.work_root / _safe_component(record_id) / _safe_component(run_id)
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _run_clipflow(self, source: Path, output: Path) -> tuple[int, str, str]:
        result = subprocess.run(
            [sys.executable, "-m", "clipflow.cli", "run", "--input", str(source), "--out", str(output)],
            capture_output=True,
            text=True,
            timeout=60 * 60,
        )
        return result.returncode, result.stdout[-8000:], result.stderr[-8000:]

    def _local_artifacts(self, output: Path) -> dict[str, Path]:
        found = {name: output / name for name in EXPECTED_ARTIFACTS}
        missing = [name for name, path in found.items() if not path.is_file() or path.stat().st_size <= 0]
        if missing:
            raise WorkflowError("LOCAL_ARTIFACT_MISSING", "missing or empty: " + ", ".join(missing))
        return found

    def _attach_one(self, record_id: str, run_id: str, local: Path) -> dict[str, Any]:
        """Append one attachment after a fresh ownership check, never replacing others."""
        current = self.client.get_record(record_id)
        self._assert_owned(current, run_id)
        fields = self._record_fields(current)
        existing = attachments(fields.get(self.config.fields.output_attachments))
        same_name = [item for item in existing if attachment_name(item) == local.name]
        if len(same_name) > 1:
            raise HoldRequired("OUTPUT_NAME_DUPLICATE", f"multiple remote attachments named {local.name}")
        if same_name:
            return same_name[0]
        token = self.client.upload_attachment(local)
        current = self.client.get_record(record_id)
        self._assert_owned(current, run_id)
        fields = self._record_fields(current)
        existing = attachments(fields.get(self.config.fields.output_attachments))
        if any(attachment_name(item) == local.name for item in existing):
            raise HoldRequired("OUTPUT_NAME_CONFLICT", f"remote attachment appeared while uploading {local.name}")
        preserved: list[dict[str, str]] = []
        for item in existing:
            token_to_preserve = attachment_token(item)
            if not token_to_preserve:
                raise HoldRequired("OUTPUT_ATTACHMENT_TOKEN_MISSING", "cannot safely preserve a remote attachment without file_token")
            preserved.append({"file_token": token_to_preserve})
        update = {
            self.config.fields.output_attachments: [
                *preserved,
                {"file_token": token},
            ],
            self.config.fields.audit: self._audit_value(
                fields.get(self.config.fields.audit),
                {
                    "at": iso_time(utc_now()),
                    "run_id": run_id,
                    "phase": "UPLOADED",
                    "file": local.name,
                    "sha256": sha256(local),
                },
            ),
        }
        self.client.update_record(record_id, update)
        readback = self.client.get_record(record_id)
        readback_items = attachments(self._record_fields(readback).get(self.config.fields.output_attachments))
        matches = [item for item in readback_items if attachment_name(item) == local.name]
        if len(matches) != 1:
            raise WorkflowError("ATTACHMENT_READBACK_MISMATCH", f"{local.name} did not read back exactly once")
        return matches[0]

    def _verify_remote_artifacts(self, record_id: str, run_id: str, local: dict[str, Path], run_dir: Path) -> dict[str, str]:
        current = self.client.get_record(record_id)
        self._assert_owned(current, run_id)
        fields = self._record_fields(current)
        output_items = attachments(fields.get(self.config.fields.output_attachments))
        field_id = self.field_ids.get(self.config.fields.output_attachments)
        if not field_id:
            raise WorkflowError("OUTPUT_FIELD_ID_MISSING", "output attachment field ID is unavailable")
        remote_hashes: dict[str, str] = {}
        for name, local_path in local.items():
            matches = [item for item in output_items if attachment_name(item) == name]
            if len(matches) != 1:
                raise WorkflowError("REMOTE_ARTIFACT_MISMATCH", f"{name} must exist exactly once remotely")
            remote_path = run_dir / "remote-readback" / name
            downloaded = self.client.download_attachment(
                matches[0],
                field_id=field_id,
                record_id=record_id,
                destination=remote_path,
            )
            digest = sha256(downloaded)
            if digest != sha256(local_path):
                raise HoldRequired("REMOTE_SHA256_MISMATCH", f"{name} remote SHA-256 does not match local artifact")
            remote_hashes[name] = digest
        return remote_hashes

    def _terminal(
        self,
        record_id: str,
        run_id: str,
        phase: str,
        evidence: dict[str, Any],
    ) -> Exception | None:
        """Attempt a terminal transition without ever disguising a failure as success."""
        try:
            self._transition(record_id, run_id, phase, evidence=evidence, terminal=True)
        except (LockLost, FeishuAPIError, WorkflowError) as exc:
            return exc
        return None

    @staticmethod
    def _terminal_failure_result(
        record_id: str,
        run_id: str,
        intended_phase: str,
        error: Exception,
    ) -> dict[str, Any]:
        if isinstance(error, LockLost):
            return {"record_id": record_id, "run_id": run_id, "result": "LOCK_LOST", "reason": str(error)}
        return {
            "record_id": record_id,
            "run_id": run_id,
            "result": "RETRYABLE",
            "evidence": {
                "code": "TERMINAL_STATE_WRITE_FAILED",
                "intended_state": intended_phase,
                "reason": str(error),
                "retryable": True,
            },
        }

    def _complete(
        self,
        record_id: str,
        run_id: str,
        local: dict[str, Path],
        run_dir: Path,
        input_digest: str,
    ) -> dict[str, Any]:
        self._transition(record_id, run_id, "UPLOADING")
        for artifact in EXPECTED_ARTIFACTS:
            self._attach_one(record_id, run_id, local[artifact])
        self._transition(record_id, run_id, "VERIFYING")
        remote_hashes = self._verify_remote_artifacts(record_id, run_id, local, run_dir)
        terminal_error = self._terminal(
            record_id,
            run_id,
            "COMPLETED",
            {"remote_sha256": remote_hashes, "input_sha256": input_digest},
        )
        if terminal_error:
            return self._terminal_failure_result(record_id, run_id, "COMPLETED", terminal_error)
        receipt = {
            "record_id": record_id,
            "run_id": run_id,
            "result": "COMPLETED",
            "input_sha256": input_digest,
            "remote_sha256": remote_hashes,
        }
        (run_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt

    def process_record(self, record_id: str) -> dict[str, Any]:
        """Process one row through claim → render → upload → remote SHA readback."""
        generated_run_id = f"omni-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:10]}"
        run_id = generated_run_id
        try:
            initial = self.client.get_record(record_id)
            initial_decision = classify_record(self.config, initial)
            initial_kind = initial_decision.kind
            initial_fields = self._record_fields(initial)
            prior_run_id = field_text(initial_fields.get(self.config.fields.run_id)).strip()
            prior_dir = (
                self.config.work_root / _safe_component(record_id) / _safe_component(prior_run_id)
                if prior_run_id else None
            )
            resuming_local = bool(
                initial_kind in {"RESUME", "VERIFY_RESUME"}
                and prior_dir is not None
                and prior_dir.is_dir()
            )
            if resuming_local:
                run_id = prior_run_id
            claimed = self._claim(record_id, run_id)
            run_dir = prior_dir if resuming_local and prior_dir is not None else self._record_dir(record_id, run_id)
            if initial_kind in {"INPUT_HOLD", "PARTIAL_HOLD", "INCONSISTENT"}:
                raise HoldRequired(f"ROW_{initial_kind}", initial_decision.reason)
            if initial_kind == "VERIFY_RESUME":
                if not resuming_local:
                    raise HoldRequired(
                        "RESUME_LOCAL_RECEIPT_MISSING",
                        "remote artifacts exist but the local run receipt is unavailable; no blind re-upload is allowed",
                    )
                local = self._local_artifacts(run_dir / "output")
                input_digest = field_text(initial_fields.get(self.config.fields.input_sha256)).strip()
                if not input_digest:
                    raise HoldRequired("RESUME_INPUT_SHA_MISSING", "interrupted run lacks its locked input SHA-256")
                return self._complete(record_id, run_id, local, run_dir, input_digest)
            if initial_kind == "RESUME":
                existing_output = run_dir / "output"
                if existing_output.is_dir():
                    try:
                        local = self._local_artifacts(existing_output)
                    except WorkflowError:
                        local = {}
                    if local:
                        input_digest = field_text(initial_fields.get(self.config.fields.input_sha256)).strip()
                        if not input_digest:
                            raise HoldRequired("RESUME_INPUT_SHA_MISSING", "interrupted run lacks its locked input SHA-256")
                        return self._complete(record_id, run_id, local, run_dir, input_digest)
                if attachments(initial_fields.get(self.config.fields.output_attachments)):
                    raise HoldRequired(
                        "RESUME_LOCAL_ARTIFACT_MISSING",
                        "partial remote output exists but matching local artifacts are unavailable; no blind replacement is allowed",
                    )
            source_attachment = self._select_source(claimed)
            input_field_id = self.field_ids.get(self.config.fields.input_attachments)
            if not input_field_id:
                raise WorkflowError("INPUT_FIELD_ID_MISSING", "input attachment field ID is unavailable")
            source_path = run_dir / ("source" + (Path(attachment_name(source_attachment)).suffix.lower() or ".mp4"))
            self.client.download_attachment(
                source_attachment,
                field_id=input_field_id,
                record_id=record_id,
                destination=source_path,
            )
            if source_path.stat().st_size > self.config.max_input_bytes:
                raise HoldRequired("INPUT_TOO_LARGE", "source video exceeds configured max_input_bytes")
            input_digest = sha256(source_path)
            self._transition(record_id, run_id, "VALIDATING", input_sha256=input_digest)
            output = run_dir / "output"
            self._transition(record_id, run_id, "RUNNING")
            returncode, stdout, stderr = self._run_clipflow(source_path, output)
            if returncode == 2:
                conflict = output / "cut_conflict.json"
                detail: dict[str, Any] = {"code": "SENTENCE_END_UNVERIFIABLE"}
                if conflict.is_file():
                    try:
                        detail["cut_conflict"] = json.loads(conflict.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        detail["cut_conflict"] = "unparseable local cut_conflict.json"
                terminal_error = self._terminal(record_id, run_id, "HOLD", detail)
                if terminal_error:
                    return self._terminal_failure_result(record_id, run_id, "HOLD", terminal_error)
                return {"record_id": record_id, "run_id": run_id, "result": "HOLD", "evidence": detail}
            if returncode != 0:
                raise WorkflowError(
                    "LOCAL_PROCESSING_ERROR",
                    f"clipflow exited {returncode}: {(stderr or stdout)[-500:]}",
                    retryable=True,
                )
            local = self._local_artifacts(output)
            return self._complete(record_id, run_id, local, run_dir, input_digest)
        except HoldRequired as exc:
            detail = {"code": exc.code, "reason": str(exc)}
            terminal_error = self._terminal(record_id, run_id, "HOLD", detail)
            if terminal_error:
                return self._terminal_failure_result(record_id, run_id, "HOLD", terminal_error)
            return {"record_id": record_id, "run_id": run_id, "result": "HOLD", "evidence": detail}
        except LockLost as exc:
            return {"record_id": record_id, "run_id": run_id, "result": "LOCK_LOST", "reason": str(exc)}
        except (WorkflowError, FeishuAPIError, OSError, subprocess.SubprocessError) as exc:
            retryable = getattr(exc, "retryable", True)
            detail = {"code": getattr(exc, "code", "UNEXPECTED_ERROR"), "reason": str(exc), "retryable": retryable}
            terminal_error = self._terminal(record_id, run_id, "RETRYABLE", detail)
            if terminal_error:
                return self._terminal_failure_result(record_id, run_id, "RETRYABLE", terminal_error)
            return {"record_id": record_id, "run_id": run_id, "result": "RETRYABLE", "evidence": detail}

    def drain(self, *, max_records: int | None = None) -> dict[str, Any]:
        """Run one row at a time until no claimable rows remain."""
        self.validate_binding()
        results: list[dict[str, Any]] = []
        attempted: set[str] = set()
        with LocalWorkerLock(self.config.work_root):
            while max_records is None or len(results) < max_records:
                selected: RecordDecision | None = None
                for record in self.client.iter_records():
                    decision = classify_record(self.config, record)
                    if decision.record_id not in attempted and decision.kind in {
                        "READY", "RESUME", "VERIFY_RESUME", "INPUT_HOLD", "PARTIAL_HOLD", "INCONSISTENT",
                    }:
                        selected = decision
                        break
                if not selected:
                    break
                attempted.add(selected.record_id)
                results.append(self.process_record(selected.record_id))
        return {
            "status": "DRAIN_COMPLETE",
            "processed": len(results),
            "results": results,
            "counts": dict(Counter(result["result"] for result in results)),
        }
