"""Command line entry point for the single canonical visual-only ClipFlow program."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__
from .cut_planner import CutConflict, VisualCue, merge_reviewed_boundaries, plan_cuts
from .delivery import build_and_verify, sha256
from .stills import export_still
from .visual_ocr import cue_rows, scan_video


def _command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("命令失败: " + " ".join(map(str, args[:10])) + "\n" + result.stderr[-2000:])
    return result.stdout


def _required_binary(name: str) -> None:
    if not shutil.which(name):
        raise RuntimeError(f"缺少 {name}，停止处理")


def media_info(path: Path) -> dict:
    _required_binary("ffprobe")
    raw = _command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate,duration", "-show_entries", "format=duration", "-of", "json", str(path)])
    data = json.loads(raw)
    stream = (data.get("streams") or [None])[0]
    if not stream:
        raise ValueError("输入没有视频流")
    duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法取得原视频时长")
    try:
        fps = float(Fraction(stream.get("avg_frame_rate", "0/0")))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {"duration": duration, "width": int(stream["width"]), "height": int(stream["height"]), "fps": fps, "vcodec": stream.get("codec_name")}


def detect_scenes(path: Path, duration: float, *, step: float = .23, min_spacing: float = .7) -> list[float]:
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError("无法解码视频画面以检测镜头")
    previous = None
    candidates: list[float] = []
    try:
        for n in range(int(duration / step) + 1):
            t = n * step
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            small = cv2.resize(frame, (128, 72), interpolation=cv2.INTER_AREA)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist, alpha=1, norm_type=cv2.NORM_L1)
            if previous is not None:
                last_hist, last_frame = previous
                diff_hist = float(cv2.compareHist(last_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
                diff_frame = float(np.mean(np.abs(small.astype(np.int16) - last_frame.astype(np.int16))))
                if (diff_hist >= .48 and diff_frame >= 28) or (diff_hist >= .38 and diff_frame >= 55):
                    if not candidates or t - candidates[-1] >= min_spacing:
                        candidates.append(round(t, 3))
            previous = hist, small
    finally:
        cap.release()
    return [t for t in candidates if .25 < t < duration - .25]


def _clock(value: float) -> str:
    total = max(0, round(value * 1000))
    hour, rem = divmod(total, 3_600_000)
    minute, rem = divmod(rem, 60_000)
    second, ms = divmod(rem, 1000)
    return f"{hour:02d}:{minute:02d}:{second:02d},{ms:03d}"


def _write_visible_srt(out: Path, cues: list[VisualCue]) -> None:
    lines: list[str] = []
    for i, cue in enumerate(cues, 1):
        lines.extend([str(i), f"{_clock(cue.start)} --> {_clock(cue.end)}", cue.text, ""])
    (out / "visible_captions.srt").write_text("\n".join(lines), encoding="utf-8")


def _font() -> str:
    result = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:style=Bold"], capture_output=True, text=True)
    path = Path(result.stdout.strip())
    if result.returncode or not path.is_file():
        raise RuntimeError("找不到水印字体，停止编码")
    return str(path)


def _draw_filter(index: int, info: dict) -> str:
    font = _font().replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    size = max(30, round(info["width"] * .035))
    return f"drawtext=fontfile='{font}':text='C{index:03d}':x={max(18, round(info['width']*.025))}:y={max(18, round(info['height']*.04))}:fontsize={size}:fontcolor=white@0.82:box=1:boxcolor=black@0.25:boxborderw=8"


def _encoded_duration(path: Path) -> float:
    raw = _command(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]).strip()
    return float(raw)


def _export_segment(source: Path, target: Path, start: float, end: float, index: int, info: dict) -> float:
    _required_binary("ffmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-ss", f"{start:.6f}", "-t", f"{duration:.6f}", "-map", "0:v:0", "-map", "0:a?", "-vf", _draw_filter(index, info), "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "aac", "-movflags", "+faststart", str(target)]
    _command(args)
    actual = _encoded_duration(target)
    if not (3.0 < actual <= 8.0):
        raise RuntimeError(f"第{index:03d}段编码后时长 {actual:.6f} 不满足严格(3,8]；拒绝交付")
    return actual


def _write_csv(out: Path, entries: list[dict]) -> None:
    fields = ["index", "file", "start", "end", "encoded_duration", "still_image", "still_source_time_seconds"]
    rows = [",".join(fields)]
    for entry in entries:
        rows.append(",".join(str(entry.get(field, "")) for field in fields))
    (out / "segments_list.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_hold(out: Path, conflict: CutConflict, source: Path, info: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    payload = conflict.diagnostic() | {"program_version": __version__, "input_file": source.name, "input_sha256": sha256(source), "source_video_info": info, "encoded_segments_created": False}
    (out / "cut_conflict.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    source = Path(args.input).resolve()
    out = Path(args.out).resolve()
    if not source.is_file():
        raise ValueError("输入视频不存在")
    if out.exists() and any(out.iterdir()):
        raise ValueError("输出目录必须为空，避免混入旧结果")
    out.mkdir(parents=True, exist_ok=True)
    info = media_info(source)
    try:
        cues, audit, observations = scan_video(source, info["duration"], step=args.ocr_step)
        timeline = {"program_version": __version__, "evidence_source": "decoded_visible_video_frames_only", "input_sha256": sha256(source), "audit": audit, "cues": cue_rows(cues), "observations": observations}
        timeline_file = out / "visual_ocr_timeline.json"
        timeline_file.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        review = None
        if args.review_json:
            review = json.loads(Path(args.review_json).read_text("utf-8"))
            if review.get("timeline_sha256") != sha256(timeline_file):
                raise CutConflict("SENTENCE_END_UNVERIFIABLE", "句末审核不对应当前画面OCR时间线，禁止编码")
            (out / "visual_sentence_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            (out / "visual_sentence_review.json").write_text(json.dumps({"evidence_source": "visible_video_frames_only", "timeline_sha256": sha256(timeline_file), "boundaries": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        boundaries = merge_reviewed_boundaries(cues, review, max_distance=args.ocr_step + .12)
        if not boundaries and info["duration"] > 8:
            raise CutConflict("SENTENCE_END_UNVERIFIABLE", "OCR已有可靠可见字幕，但没有可验证的完整句末；停止于编码前", cue_count=len(cues))
        cuts = plan_cuts(info["duration"], detect_scenes(source, info["duration"]), boundaries)
    except CutConflict as conflict:
        _write_hold(out, conflict, source, info)
        print(json.dumps(conflict.diagnostic(), ensure_ascii=False))
        return 2
    _write_visible_srt(out, cues)
    visible_lines: list[str] = []
    for cue in cues:
        if cue.text not in visible_lines:
            visible_lines.append(cue.text)
    (out / "文案.txt").write_text("\n".join(visible_lines) + "\n", encoding="utf-8")
    entries: list[dict] = []
    for index, (start, end) in enumerate(zip(cuts, cuts[1:]), 1):
        filename = f"{index:03d}_镜头.mp4"
        segment = out / "segments" / filename
        actual = _export_segment(source, segment, start, end, index, info)
        still = export_still(source, start, end, out / "segment_stills" / f"{index:03d}_镜头.jpg", number=index)
        entries.append({"index": index, "file": filename, "start": round(start, 6), "end": round(end, 6), "encoded_duration": round(actual, 6), **still})
    manifest = {"program_version": __version__, "source_sha256": sha256(source), "source_video_info": info, "visual_only": True, "cuts": cuts, "segments": entries}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(out, entries)
    contract = {"mode": "append_only", "target_field": "处理后音、视频", "artifacts": ["videos_and_screenshot.zip", "supporting_materials.zip", "文案.txt"], "remote_readback_required": True, "uploader": "existing_bitable_mcp"}
    (out / "feishu_append_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Placeholder permits the package verifier to include its own final receipt.
    (out / "verification.json").write_text("{}\n", encoding="utf-8")
    media, support, script, verification = build_and_verify(out, entries)
    print(json.dumps({"status": "PASS", "program_version": __version__, "media_zip": str(media), "support_zip": str(support), "script": str(script), "verification": verification}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipflow-visual", description="ClipFlow v0.6 visual-only strict splitter")
    parser.add_argument("run", nargs="?", default="run")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--review-json", help="visual-only reviewed sentence endpoints tied to this run's timeline")
    parser.add_argument("--ocr-step", type=float, default=.35)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "program_version": __version__, "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
