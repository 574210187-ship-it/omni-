import unittest

from clipflow.cut_planner import CutConflict, VerifiedBoundary, VisualCue, merge_reviewed_boundaries, plan_cuts
from clipflow.visual_ocr import consolidate_observations


class QualityContractTests(unittest.TestCase):
    def test_planner_keeps_strict_bounds(self):
        cuts = plan_cuts(24.0, [6.9, 13.1, 20.0], [
            VerifiedBoundary(7.0, 0, "reviewed_visible_frame_boundary"),
            VerifiedBoundary(13.0, 1, "reviewed_visible_frame_boundary"),
            VerifiedBoundary(20.0, 2, "reviewed_visible_frame_boundary"),
        ])
        self.assertEqual(cuts, [0.0, 7.0, 13.0, 20.0, 24.0])
        self.assertTrue(all(3.0 < end - start <= 8.0 for start, end in zip(cuts, cuts[1:])))

    def test_no_sentence_end_is_hold_for_long_source(self):
        with self.assertRaises(CutConflict) as caught:
            plan_cuts(9.0, [], [])
        self.assertEqual(caught.exception.code, "SENTENCE_END_UNVERIFIABLE")

    def test_review_must_be_visual_only_and_tied_to_existing_cue(self):
        cues = [VisualCue(0.0, 5.0, "可见字幕", .99, 4)]
        with self.assertRaises(CutConflict) as caught:
            merge_reviewed_boundaries(cues, {"evidence_source": "other", "boundaries": []}, max_distance=.5)
        self.assertEqual(caught.exception.code, "SENTENCE_END_UNVERIFIABLE")
        boundary = merge_reviewed_boundaries(cues, {"evidence_source": "visible_video_frames_only", "boundaries": [{"cue_index": 0, "end": 5.0, "sentence_end_verified": True, "visual_evidence": "字幕在该帧结束"}]}, max_distance=.5)
        self.assertEqual(len(boundary), 1)

    def test_one_isolated_read_is_not_a_reliable_visual_cue(self):
        cues = consolidate_observations([{"time": 1.0, "text": "偶发文字", "confidence": .90, "coverage": .02}], 4.0, .35)
        self.assertEqual(cues, [])


if __name__ == "__main__":
    unittest.main()
