from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from omni_phase1.config import ConfigError, load_config
from omni_phase1.feishu import FeishuClient
from omni_phase1.workflow import OmniWorker, classify_record, iso_time, utc_now


def write_config(root: Path) -> tuple[Path, dict[str, str]]:
    config = {
        "schema_version": 1,
        "credentials": {
            "app_id": "${FEISHU_APP_ID}",
            "app_secret": "${FEISHU_APP_SECRET}",
        },
        "target": {
            "bitable_url": "${FEISHU_BITABLE_URL}",
            "expected_view_name": "剧情",
        },
        "fields": {
            "category": "二次分类型",
            "input_attachments": "对标视频、图文",
            "output_attachments": "处理后音、视频",
            "audit": "执行记录",
            "status": "ClipFlow状态",
            "run_id": "ClipFlow运行ID",
            "lease_until": "ClipFlow锁到期",
            "input_sha256": "ClipFlow输入SHA",
            "evidence": "ClipFlow错误码/证据",
        },
        "queue": {"category_value": "剧情", "lease_seconds": 600, "max_input_bytes": 1000000},
        "runtime": {"work_root": ".omni-phase1"},
    }
    path = root / "job-config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    env = {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "private-test-secret",
        "FEISHU_BITABLE_URL": "https://sample.feishu.cn/base/appTEST?table=tblDRAMA&view=vewDRAMA",
    }
    return path, env


class FakeClient:
    def __init__(self, records=None, include_worker_fields=True):
        names = [
            ("二次分类型", 3),
            ("对标视频、图文", 17),
            ("处理后音、视频", 17),
        ]
        if include_worker_fields:
            names += [
                ("执行记录", 1),
                ("ClipFlow状态", 1),
                ("ClipFlow运行ID", 1),
                ("ClipFlow锁到期", 1),
                ("ClipFlow输入SHA", 1),
                ("ClipFlow错误码/证据", 1),
            ]
        self.fields = [
            {"field_name": name, "field_id": f"fld{index}", "type": field_type}
            for index, (name, field_type) in enumerate(names, 1)
        ]
        self.records = {item["record_id"]: deepcopy(item) for item in (records or [])}
        self.created = []
        self.token_names = {}

    def list_views(self):
        return [{"view_id": "vewDRAMA", "view_name": "剧情"}]

    def list_fields(self):
        return deepcopy(self.fields)

    def create_text_field(self, field_name):
        self.created.append(field_name)
        self.fields.append({"field_name": field_name, "field_id": f"new{len(self.fields)}", "type": 1})
        return self.fields[-1]

    def iter_records(self):
        return iter(deepcopy(list(self.records.values())))

    def get_record(self, record_id):
        return deepcopy(self.records[record_id])

    def update_record(self, record_id, fields):
        if "处理后音、视频" in fields:
            fields = deepcopy(fields)
            fields["处理后音、视频"] = [
                {"file_token": item["file_token"], "name": self.token_names.get(item["file_token"], "")}
                for item in fields["处理后音、视频"]
            ]
        self.records[record_id]["fields"].update(deepcopy(fields))
        return self.get_record(record_id)

    def upload_attachment(self, path):
        token = "new-token"
        self.token_names[token] = Path(path).name
        return token


def record(record_id, category="剧情", input_items=None, output_items=None, status=""):
    return {
        "record_id": record_id,
        "fields": {
            "二次分类型": category,
            "对标视频、图文": input_items or [],
            "处理后音、视频": output_items or [],
            "执行记录": "",
            "ClipFlow状态": status,
            "ClipFlow运行ID": "",
            "ClipFlow锁到期": "",
            "ClipFlow输入SHA": "",
            "ClipFlow错误码/证据": "",
        },
    }


class OmniPhase1WorkerTests(unittest.TestCase):
    def test_config_binds_only_from_private_environment_url(self):
        with tempfile.TemporaryDirectory() as temp:
            path, env = write_config(Path(temp))
            config = load_config(path, env)
        self.assertEqual(config.binding.app_token, "appTEST")
        self.assertEqual(config.binding.table_id, "tblDRAMA")
        self.assertEqual(config.binding.view_id, "vewDRAMA")
        self.assertEqual(config.expected_view_name, "剧情")

    def test_config_rejects_target_url_without_exact_view(self):
        with tempfile.TemporaryDirectory() as temp:
            path, env = write_config(Path(temp))
            env["FEISHU_BITABLE_URL"] = "https://sample.feishu.cn/base/appTEST?table=tblDRAMA"
            with self.assertRaises(ConfigError):
                load_config(path, env)

    def test_classification_never_queues_chat_or_existing_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            path, env = write_config(Path(temp))
            config = load_config(path, env)
        video = [{"name": "source.mp4", "file_token": "input"}]
        complete = [{"name": name, "file_token": name} for name in config.artifacts]
        self.assertEqual(classify_record(config, record("chat", category="聊天", input_items=video)).kind, "OUT_OF_SCOPE")
        self.assertEqual(classify_record(config, record("done", input_items=video, output_items=complete)).kind, "EXISTING_COMPLETE")
        self.assertEqual(classify_record(config, record("ready", input_items=video)).kind, "READY")

    def test_bootstrap_creates_only_dedicated_text_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path, env = write_config(Path(temp))
            config = load_config(path, env)
            client = FakeClient(include_worker_fields=False)
            worker = OmniWorker(config, client)
            preview = worker.bootstrap(apply=False)
            self.assertEqual(client.created, [])
            self.assertEqual(set(preview["would_create_fields"]), set(config.fields.worker_text_fields))
            applied = worker.bootstrap(apply=True)
            self.assertEqual(set(client.created), set(config.fields.worker_text_fields))
            self.assertEqual(applied["validation"]["missing_worker_fields"], [])

    def test_unprocessable_drama_row_is_claimed_then_visible_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            path, env = write_config(Path(temp))
            config = load_config(path, env)
            client = FakeClient(records=[record("rec-no-input")])
            worker = OmniWorker(config, client)
            worker.validate_binding()
            result = worker.process_record("rec-no-input")
            self.assertEqual(result["result"], "HOLD")
            final = client.get_record("rec-no-input")["fields"]
            self.assertEqual(final["ClipFlow状态"], "HOLD")
            self.assertIn("ROW_INPUT_HOLD", final["ClipFlow错误码/证据"])

    def test_attachment_append_preserves_existing_files_by_token_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, env = write_config(root)
            config = load_config(path, env)
            existing = {"name": "keep.txt", "file_token": "old-token", "url": "https://ignored.example/old"}
            client = FakeClient(records=[record("rec-append", input_items=[{"name": "source.mp4", "file_token": "source"}], output_items=[existing])])
            client.token_names["old-token"] = "keep.txt"
            client.records["rec-append"]["fields"].update({
                "ClipFlow状态": "UPLOADING",
                "ClipFlow运行ID": "run-1",
                "ClipFlow锁到期": iso_time(utc_now() + timedelta(minutes=10)),
            })
            worker = OmniWorker(config, client)
            worker.validate_binding()
            local = root / "文案.txt"
            local.write_text("visible text\n", encoding="utf-8")
            worker._attach_one("rec-append", "run-1", local)
            output = client.get_record("rec-append")["fields"]["处理后音、视频"]
            self.assertEqual(output, [
                {"file_token": "old-token", "name": "keep.txt"},
                {"file_token": "new-token", "name": "文案.txt"},
            ])

    def test_drain_includes_interrupted_verification_with_all_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            path, env = write_config(Path(temp))
            config = load_config(path, env)
            complete = [{"name": name, "file_token": f"token-{index}"} for index, name in enumerate(config.artifacts)]
            client = FakeClient(records=[record("rec-verify", output_items=complete, status="RETRYABLE")])

            class RecordingWorker(OmniWorker):
                def __init__(self, worker_config, worker_client):
                    super().__init__(worker_config, worker_client)
                    self.seen = []

                def process_record(self, record_id):
                    self.seen.append(record_id)
                    return {"record_id": record_id, "result": "COMPLETED"}

            worker = RecordingWorker(config, client)
            result = worker.drain()
            self.assertEqual(worker.seen, ["rec-verify"])
            self.assertEqual(result["counts"], {"COMPLETED": 1})


class _Response:
    status_code = 200
    headers = {}

    def json(self):
        return {"code": 0, "tenant_access_token": "t-test", "expire": 7200}


class _Session:
    def request(self, *_args, **_kwargs):
        return _Response()


class _DownloadResponse:
    status_code = 200
    headers = {}

    def iter_content(self, chunk_size):
        del chunk_size
        yield b"video-bytes"


class _DownloadSession:
    def __init__(self):
        self.download_url = None
        self.download_kwargs = None

    def request(self, *_args, **_kwargs):
        return _Response()

    def get(self, url, **kwargs):
        self.download_url = url
        self.download_kwargs = kwargs
        return _DownloadResponse()


class FeishuAdapterTests(unittest.TestCase):
    def test_token_endpoint_uses_top_level_response_not_data_wrapper(self):
        with tempfile.TemporaryDirectory() as temp:
            path, env = write_config(Path(temp))
            config = load_config(path, env)
        client = FeishuClient(config.binding, session=_Session(), sleep=lambda _seconds: None)
        self.assertEqual(client._tenant_token(), "t-test")

    def test_bitable_download_always_uses_canonical_endpoint_and_extra(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, env = write_config(root)
            config = load_config(path, env)
            session = _DownloadSession()
            client = FeishuClient(config.binding, session=session, sleep=lambda _seconds: None)
            destination = root / "input.mp4"
            client.download_attachment(
                {"file_token": "file-token", "url": "https://untrusted.example/ignored"},
                field_id="fldOutput",
                record_id="rec1",
                destination=destination,
            )
            self.assertEqual(
                session.download_url,
                "https://open.feishu.cn/open-apis/drive/v1/medias/file-token/download",
            )
            extra = json.loads(session.download_kwargs["params"]["extra"])
            self.assertEqual(
                extra,
                {"bitablePerm": {"tableId": "tblDRAMA", "attachments": {"fldOutput": {"rec1": ["file-token"]}}}},
            )
            self.assertEqual(destination.read_bytes(), b"video-bytes")


if __name__ == "__main__":
    unittest.main()
