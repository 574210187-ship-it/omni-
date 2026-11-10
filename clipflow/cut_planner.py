"""Strict scene-aware planning from *visually verified* sentence endpoints only."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Iterable, Sequence

MIN_SECONDS_EXCLUSIVE = 3.0
MAX_SECONDS_INCLUSIVE = 8.0
TERMINAL_MARKS = frozenset("。！？!?")
CLOSERS = "\"'”’」』）)]】》〉"


class CutConflict(ValueError):
    """A quality gate stopped the pipeline before media encoding."""

    def __init__(self, code: str, reason: str, **evidence: object):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.evidence = evidence

    def diagnostic(self) -> dict:
        # Evidence may include its own scan status; the externally visible
        # contract must remain an unambiguous pre-encoding HOLD.
        return {**self.evidence, "status": "HOLD", "code": self.code, "reason": self.reason}


@dataclass(frozen=True)
class VisualCue:
    start: float
    end: float
    text: str
    confidence: float
    observations: int


@dataclass(frozen=True)
class VerifiedBoundary:
    end: float
    cue_index: int
    evidence: str


def has_visible_terminal_mark(text: str) -> bool:
    value = str(text).strip().rstrip(CLOSERS).rstrip()
    return bool(value and value[-1] in TERMINAL_MARKS)


def verified_boundaries_from_visible_cues(cues: Sequence[VisualCue]) -> list[VerifiedBoundary]:
    """Use only an explicit sentence terminator actually recognized in pixels."""
    return [VerifiedBoundary(round(cue.end, 3), i, "visible_terminal_mark")
            for i, cue in enumerate(cues) if has_visible_terminal_mark(cue.text)]


def merge_reviewed_boundaries(
    cues: Sequence[VisualCue], review: dict | None, *, max_distance: float
) -> list[VerifiedBoundary]:
    """Accept explicit visual-only review records tied to an existing cue.

    The review is deliberately not inferred.  Every claimed endpoint must name a
    cue index, have a positive visual-only flag, and land close to that cue's
    observed disappearance time.  No review means only visible terminal marks
    qualify.
    """
    found = {(b.end, b.cue_index): b for b in verified_boundaries_from_visible_cues(cues)}
    if not review:
        return sorted(found.values(), key=lambda b: b.end)
    if review.get("evidence_source") != "visible_video_frames_only":
        raise CutConflict("SENTENCE_END_UNVERIFIABLE", "句末审核未声明为纯画面证据，禁止编码")
    boundaries = review.get("boundaries")
    if not isinstance(boundaries, list):
        raise CutConflict("SENTENCE_END_UNVERIFIABLE", "句末审核缺少 boundaries 列表，禁止编码")
    for row in boundaries:
        try:
            cue_index = int(row["cue_index"])
            end = round(float(row["end"]), 3)
        except (KeyError, TypeError, ValueError) as exc:
            raise CutConflict("SENTENCE_END_UNVERIFIABLE", "句末审核条目缺少 cue_index 或 end", detail=str(exc)) from exc
        if not row.get("sentence_end_verified"):
            continue
        if not (0 <= cue_index < len(cues)):
            raise CutConflict("SENTENCE_END_UNVERIFIABLE", "句末审核引用了不存在的画面字幕 cue", cue_index=cue_index)
        cue = cues[cue_index]
        if abs(end - cue.end) > max_distance:
            raise CutConflict("SENTENCE_END_UNVERIFIABLE", "句末审核时间未绑定到可见字幕的消失时间", cue_index=cue_index, cue_end=cue.end, reviewed_end=end)
        note = str(row.get("visual_evidence") or "").strip()
        if not note:
            raise CutConflict("SENTENCE_END_UNVERIFIABLE", "句末审核没有填写可见画面证据", cue_index=cue_index)
        found[(end, cue_index)] = VerifiedBoundary(end, cue_index, "reviewed_visible_frame_boundary")
    return sorted(found.values(), key=lambda b: b.end)


def plan_cuts(duration: float, scene_boundaries: Iterable[float], boundaries: Sequence[VerifiedBoundary]) -> list[float]:
    """Global DP: every nonfinal cut is visually certified; every clip is (3, 8]."""
    if not math.isfinite(duration) or duration <= MIN_SECONDS_EXCLUSIVE:
        raise CutConflict("NO_FEASIBLE_CUTS", "原视频时长不大于3秒，无法产生合格片段", duration=duration)
    scenes = sorted({round(float(t), 3) for t in scene_boundaries if 0 < float(t) < duration})
    ends = sorted({b.end for b in boundaries if 0 < b.end < duration})
    if not ends and duration > MAX_SECONDS_INCLUSIVE:
        raise CutConflict("SENTENCE_END_UNVERIFIABLE", "没有可验证的可见字幕句末，原片超过8秒，禁止编码", duration=duration)
    nodes = [0.0] + ends + [round(duration, 6)]
    nodes = sorted(set(nodes))
    distance = [float("inf")] * len(nodes)
    steps = [10**9] * len(nodes)
    previous = [-1] * len(nodes)
    distance[0] = 0.0
    steps[0] = 0
    for i in range(1, len(nodes)):
        for j in range(i):
            width = nodes[i] - nodes[j]
            if not (MIN_SECONDS_EXCLUSIVE < width <= MAX_SECONDS_INCLUSIVE + 1e-8):
                continue
            if not math.isfinite(distance[j]):
                continue
            terminal = i == len(nodes) - 1
            scene_distance = 0.0 if terminal else min((abs(nodes[i] - x) for x in scenes), default=9.0)
            cost = distance[j] + 1.0 + (0.0 if terminal else min(3.0, scene_distance * 2.0)) + .08 * ((width - 5.5) / 2.5) ** 2
            count = steps[j] + 1
            if cost < distance[i] - 1e-9 or (abs(cost - distance[i]) <= 1e-9 and count < steps[i]):
                distance[i], steps[i], previous[i] = cost, count, j
    if previous[-1] < 0:
        raise CutConflict(
            "NO_FEASIBLE_CUTS",
            "可见字幕句末、严格时长和镜头优先无法同时满足；禁止硬切或放宽时长",
            duration=duration,
            verified_sentence_ends=ends,
            scene_candidates=scenes,
            duration_contract="3.000 < segment_seconds <= 8.000",
        )
    cuts: list[float] = []
    index = len(nodes) - 1
    while index >= 0:
        cuts.append(nodes[index])
        index = previous[index]
    cuts.reverse()
    if not all(MIN_SECONDS_EXCLUSIVE < b - a <= MAX_SECONDS_INCLUSIVE + 1e-8 for a, b in zip(cuts, cuts[1:])):
        raise AssertionError("internal planner contract failure")
    return cuts
