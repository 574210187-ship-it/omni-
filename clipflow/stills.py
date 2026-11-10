"""Opening-frame still selection from the first ten original decoded frames."""
from __future__ import annotations

import math
from pathlib import Path

from .visual_ocr import VisualOcr

POLICY = "fewest_visible_OCR_characters_then_area_then_clarity_first_10_original_frames"


def _quality(frame, previous=None) -> tuple[bool, float]:
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    if h < 8 or w < 8:
        return False, -999.0
    scale = min(1.0, 360.0 / w, 480.0 / h)
    small = cv2.resize(frame, (max(8, round(w * scale)), max(8, round(h * scale))), interpolation=cv2.INTER_AREA) if scale < 1 else frame
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    focus = gray[max(0, int(gray.shape[0] * .08)):max(1, int(gray.shape[0] * .76)), max(0, int(gray.shape[1] * .08)):max(1, int(gray.shape[1] * .92))]
    sharpness = float(cv2.Laplacian(focus, cv2.CV_64F).var())
    brightness = float(gray.mean())
    contrast = float(gray.std())
    exposure = float(np.mean(gray < 12)) + float(np.mean(gray > 244))
    stability = 0.0 if previous is None or previous.shape != gray.shape else float(np.abs(gray.astype(np.int16) - previous.astype(np.int16)).mean())
    okay = 20 <= brightness <= 235 and exposure < .82
    return okay, 2.6 * math.log1p(sharpness) + .017 * contrast - 6.5 * exposure - .016 * abs(brightness - 120) - .024 * stability


def export_still(source: Path, start: float, end: float, target: Path, *, number: int) -> dict:
    """Select then watermark. Selection always occurs before the watermark exists."""
    import cv2
    if end <= start:
        raise ValueError("截图片段起止时间无效")
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError("无法打开原视频以选择截图")
    engine = VisualOcr()
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if not math.isfinite(fps) or fps <= .1:
            raise RuntimeError("无法读取可靠帧率，停止截图")
        start_frame = max(0, math.ceil(start * fps - 1e-7))
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        candidates = []
        previous = None
        for rel in range(10):
            ok, frame = cap.read()
            if not ok or frame is None or (start_frame + rel) / fps >= end - 1e-6:
                break
            valid, clarity = _quality(frame, previous)
            small = cv2.resize(frame, (max(8, round(frame.shape[1] * min(1.0, 360 / frame.shape[1]))), max(8, round(frame.shape[0] * min(1.0, 360 / frame.shape[1])))), interpolation=cv2.INTER_AREA) if frame.shape[1] > 360 else frame
            previous = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if valid:
                chars, area = engine.count_visible_text(frame)
                candidates.append((chars, area, -clarity, rel, frame))
        if not candidates:
            raise RuntimeError("该段前10个原始帧均不可用，停止交付")
        chars, area, _neg, relative, selected = min(candidates, key=lambda x: x[:4])
        h, w = selected.shape[:2]
        size = max(26, round(w * .034))
        cv2.putText(selected, f"C{number:03d}", (max(18, round(w * .025)), max(size + 18, round(h * .08))), cv2.FONT_HERSHEY_DUPLEX, size / 48, (230, 230, 230), max(1, round(size / 18)), cv2.LINE_AA)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), selected, [int(cv2.IMWRITE_JPEG_QUALITY), 95]):
            raise RuntimeError("截图写入失败")
        decoded = cv2.imread(str(target))
        if decoded is None or decoded.shape[:2] != (h, w):
            raise RuntimeError("截图读回验证失败")
        stamp = (start_frame + relative) / fps
        return {
            "still_image": f"segment_stills/{target.name}", "still_source_time_seconds": round(stamp, 3),
            "still_offset_seconds": round(max(0.0, stamp - start), 6), "still_source_frame_index": start_frame + relative,
            "still_offset_frame_index": relative, "still_sequence_number": f"{number:03d}",
            "still_text_characters": chars, "still_text_area_ratio": area, "still_candidate_frames": min(10, max(0, int(round((end-start)*fps)))),
            "still_selection_policy": POLICY, "still_watermark_scheme": "C", "still_has_sequence_watermark": True,
            "still_source": "original_video_opening_frame_then_sequence_watermark",
        }
    finally:
        cap.release()
