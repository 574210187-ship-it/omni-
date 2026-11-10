"""Multi-frame OCR of decoded video pixels only.

This module never opens, decodes, samples, or reasons from an audio stream.
Its timestamps describe screen visibility, not speech timing.
"""
from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Any

from .cut_planner import CutConflict, VisualCue


def _clean_text(text: object) -> str:
    value = re.sub(r"\s+", "", str(text or "")).strip()
    # Preserve terminal punctuation: it is the only automatic visual sentence
    # boundary proof this program will accept.
    return value.strip("|／/·-_，．*~").strip()


def _substantive(text: str) -> bool:
    return len(re.findall(r"[\u3400-\u9fff]", text)) >= 2 or len(re.findall(r"[A-Za-z0-9]", text)) >= 5


class VisualOcr:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError("缺少 rapidocr-onnxruntime；这是纯画面OCR运行依赖，未安装时停止，不会改走其它文字来源。") from exc
        self._engine = RapidOCR()

    def read(self, frame) -> tuple[str, float, float]:
        """Return concatenated lower-caption text, confidence, and covered area."""
        import cv2

        h, w = frame.shape[:2]
        crop = frame[int(h * 0.46):int(h * 0.97), int(w * 0.03):int(w * 0.97)]
        if crop.size == 0:
            return "", 0.0, 0.0
        result, _elapsed = self._engine(crop)
        if not result:
            return "", 0.0, 0.0
        rows: list[tuple[float, float, str, float, float]] = []
        crop_area = float(max(1, crop.shape[0] * crop.shape[1]))
        for item in result:
            try:
                box, text, confidence = item
                value = _clean_text(text)
                confidence = float(confidence)
                if confidence < 0.84 or not _substantive(value):
                    continue
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                width = max(xs) - min(xs)
                height = max(ys) - min(ys)
                if width < crop.shape[1] * .08:
                    continue
                rows.append((min(ys), min(xs), value, confidence, max(0.0, width * height / crop_area)))
            except (TypeError, ValueError, IndexError):
                continue
        if not rows:
            return "", 0.0, 0.0
        rows.sort()
        text = _clean_text("".join(r[2] for r in rows))
        if not _substantive(text):
            return "", 0.0, 0.0
        confidence = sum(r[3] for r in rows) / len(rows)
        coverage = sum(r[4] for r in rows)
        return text, round(confidence, 6), round(coverage, 8)

    def count_visible_text(self, frame) -> tuple[int, float]:
        text, _confidence, coverage = self.read(frame)
        return len(text), coverage


def _same_text(left: str, right: str) -> bool:
    return left == right or SequenceMatcher(None, left, right).ratio() >= .84


def consolidate_observations(observations: list[dict[str, Any]], duration: float, step: float) -> list[VisualCue]:
    """Require temporal agreement before a visible OCR read becomes a cue."""
    runs: list[dict[str, Any]] = []
    for item in observations:
        last = runs[-1] if runs else None
        if last and item["time"] - last["last"] <= step * 2.55 and _same_text(item["text"], last["text"]):
            last["last"] = item["time"]
            last["observations"] += 1
            if item["confidence"] > last["confidence"]:
                last["text"] = item["text"]
                last["confidence"] = item["confidence"]
        else:
            runs.append({"first": item["time"], "last": item["time"], "text": item["text"], "confidence": item["confidence"], "observations": 1})
    return [VisualCue(round(max(0.0, x["first"] - step / 2), 3), round(min(duration, x["last"] + step / 2), 3), x["text"], x["confidence"], x["observations"])
            for x in runs if x["observations"] >= 2 or (x["confidence"] >= .985 and len(x["text"]) >= 8)]


def scan_video(video: Path, duration: float, *, step: float = .35) -> tuple[list[VisualCue], dict[str, Any], list[dict[str, Any]]]:
    """Build a resilient visual-only cue timeline; fail before encoding when weak."""
    if not (.20 <= step <= 1.0):
        raise ValueError("--ocr-step 仅允许 0.20—1.00 秒")
    import cv2
    engine = VisualOcr()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"无法解码视频画面: {video}")
    observations: list[dict[str, Any]] = []
    samples = 0
    try:
        t = min(step / 2, max(0.0, duration / 2))
        while t < duration:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if ok and frame is not None:
                samples += 1
                text, confidence, coverage = engine.read(frame)
                if text:
                    observations.append({"time": round(t, 3), "text": text, "confidence": confidence, "coverage": coverage})
            t += step
    finally:
        cap.release()
    cues = consolidate_observations(observations, duration, step)
    audit: dict[str, Any] = {
        "engine": "rapidocr-onnxruntime",
        "evidence_source": "decoded_visible_video_frames_only",
        "audio_stream_opened": False,
        "sample_step_seconds": step,
        "frames_sampled": samples,
        "raw_text_observations": len(observations),
        "confirmed_visible_subtitle_cues": len(cues),
        "status": "captions_detected" if cues else ("ocr_unreliable" if observations else "ocr_empty"),
    }
    if not observations:
        raise CutConflict("OCR_EMPTY", "多帧画面OCR未识别到可见字幕文字；停止于编码前", **audit)
    if not cues:
        raise CutConflict("OCR_UNRELIABLE", "画面OCR虽有零散结果，但没有达到多帧一致性/置信度门；停止于编码前", **audit)
    return cues, audit, observations


def cue_rows(cues: list[VisualCue]) -> list[dict[str, Any]]:
    return [{"cue_index": i, **asdict(cue)} for i, cue in enumerate(cues)]
