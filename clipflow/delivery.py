"""Final two-ZIP plus standalone TXT package and proof checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

MIN_SECONDS_EXCLUSIVE = 3.0
MAX_SECONDS_INCLUSIVE = 8.0
STILL_POLICY = "fewest_visible_OCR_characters_then_area_then_clarity_first_10_original_frames"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _need(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"交付缺少或为空: {label}")


def _validate_txt(path: Path) -> None:
    _need(path, "文案.txt")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError("文案.txt必须是UTF-8无BOM")
    text = raw.decode("utf-8")
    if not text.splitlines() or any(not line.strip() for line in text.splitlines()):
        raise RuntimeError("文案.txt只能含非空的逐行可见文字")


def _validate_entries(entries: list[dict]) -> None:
    if not entries:
        raise RuntimeError("没有片段，禁止打包")
    if [int(row["index"]) for row in entries] != list(range(1, len(entries) + 1)):
        raise RuntimeError("片段编号不连续")
    for row in entries:
        number = f"{int(row['index']):03d}"
        requested = float(row["end"]) - float(row["start"])
        encoded = float(row["encoded_duration"])
        if not (MIN_SECONDS_EXCLUSIVE < requested <= MAX_SECONDS_INCLUSIVE and MIN_SECONDS_EXCLUSIVE < encoded <= MAX_SECONDS_INCLUSIVE):
            raise RuntimeError(f"第{number}段时长不在严格(3,8]内")
        if row.get("still_image") != f"segment_stills/{Path(row['file']).stem}.jpg":
            raise RuntimeError(f"第{number}段视频与截图编号不匹配")
        if row.get("still_selection_policy") != STILL_POLICY or not (0 <= int(row.get("still_offset_frame_index", -1)) < 10):
            raise RuntimeError(f"第{number}段截图并非首十原始帧纯画面选图")
        if row.get("still_sequence_number") != number or not row.get("still_has_sequence_watermark"):
            raise RuntimeError(f"第{number}段截图水印证明不足")


def _zip(destination: Path, members: dict[str, Path]) -> Path:
    partial = destination.with_name("." + destination.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
            for name, source in sorted(members.items()):
                _need(source, name)
                mode = zipfile.ZIP_STORED if source.suffix.lower() in {".mp4", ".jpg"} else zipfile.ZIP_DEFLATED
                archive.write(source, name, compress_type=mode)
        with zipfile.ZipFile(partial) as archive:
            if set(archive.namelist()) != set(members) or archive.testzip() is not None:
                raise RuntimeError(f"{destination.name}成员或CRC校验失败")
            for name, source in members.items():
                if archive.getinfo(name).file_size != source.stat().st_size:
                    raise RuntimeError(f"{destination.name}内部长度不一致: {name}")
        partial.replace(destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)


def build_and_verify(out: Path, entries: list[dict]) -> tuple[Path, Path, Path, dict]:
    _validate_entries(entries)
    script = out / "文案.txt"
    _validate_txt(script)
    media: dict[str, Path] = {}
    for row in entries:
        media[f"segments/{row['file']}"] = out / "segments" / row["file"]
        media[row["still_image"]] = out / row["still_image"]
    support_names = ["visible_captions.srt", "visual_ocr_timeline.json", "visual_sentence_review.json", "manifest.json", "segments_list.csv", "verification.json", "feishu_append_contract.json"]
    support = {name: out / name for name in support_names}
    media_zip = _zip(out / "videos_and_screenshot.zip", media)
    support_zip = _zip(out / "supporting_materials.zip", support)
    verification = {
        "delivery_contract": "two_zip_plus_one_txt",
        "visual_evidence_only": True,
        "media_zip": {"file": media_zip.name, "sha256": sha256(media_zip), "bytes": media_zip.stat().st_size, "members": sorted(media)},
        # A ZIP cannot contain a stable hash of itself.  Its final hash is
        # reported by the outer run receipt after the archive is closed.
        "support_zip": {"file": support_zip.name, "members": sorted(support)},
        "script": {"file": script.name, "sha256": sha256(script), "bytes": script.stat().st_size},
        "segments": len(entries),
    }
    # Package has to include the final verification itself, so write then rebuild support once.
    (out / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    support_zip = _zip(out / "supporting_materials.zip", support)
    # Rebuild once so the support archive contains the final verification file.
    support_zip = _zip(out / "supporting_materials.zip", support)
    return media_zip, support_zip, script, verification
