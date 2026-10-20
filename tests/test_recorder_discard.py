import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from saymore.audio.capture import Recorder


class FakeVad:
    """假 VAD，只记录被调用的顺序。"""

    def __init__(self):
        self.calls = []

    def reset(self):
        self.calls.append("reset")

    def accept_waveform(self, block):
        self.calls.append("accept")

    def is_speech_detected(self):
        return False

    def empty(self):
        return True


class DiscardCurrentTests(unittest.TestCase):
    def setUp(self):
        self.vad = FakeVad()
        self.rec = Recorder(16000, on_segment=lambda seg: None, silence_rms=0.01,
                            silence_seconds=1.0, min_segment_seconds=0.4, vad=self.vad)
        self.vad.calls.clear()  # 构造时那次 reset 不计入

    def feed(self):
        self.rec._callback(np.zeros((512, 1), dtype=np.float32), 512, None, None)

    def test_discard_defers_vad_reset_to_audio_thread(self):
        """discard_current 由 KWS 线程调用，绝不能当场碰 VAD（sherpa-onnx VAD 非线程安全，
        与回调里的 accept_waveform 撞车会抛 ValueError: vector too long），
        清空必须推迟到下一个音频块、在 PortAudio 回调线程里做，且只做一次。"""
        self.rec.discard_current()
        self.assertEqual(self.vad.calls, [])
        self.feed()
        self.assertEqual(self.vad.calls, ["reset", "accept"])
        self.feed()
        self.assertEqual(self.vad.calls, ["reset", "accept", "accept"])


class PlaybackGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("saymore.audio.capture.time.monotonic", return_value=10.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.guard_state = (0, 0.0)
        self.segments = []
        self.blocks = []
        self.speech = Mock()
        self.suppression_changes = []

    def recorder(self, vad=None):
        return Recorder(
            16000, self.segments.append, silence_rms=0.01, silence_seconds=0.032,
            min_segment_seconds=0.032, vad=vad, on_block=self.blocks.append,
            on_speech=self.speech,
            input_guard=lambda: self.guard_state,
            on_input_suppressed=self.suppression_changes.append,
        )

    def feed(self, rec, level=0.1, time_info=None):
        rec._callback(np.full((512, 1), level, dtype=np.float32), 512, time_info, None)

    def test_playback_and_tail_do_not_reach_vad_kws_or_speech_callbacks(self):
        vad = FakeVad()
        rec = self.recorder(vad)
        vad.calls.clear()
        self.feed(rec)
        self.guard_state = (1, float("inf"))
        self.feed(rec)
        self.feed(rec)
        self.guard_state = (1, 10.4)
        self.feed(rec)
        self.assertEqual(vad.calls, ["accept", "reset"])
        self.assertEqual(len(self.blocks), 1)
        self.speech.assert_not_called()
        self.assertEqual(self.suppression_changes, [True])

        self.now.return_value = 10.5
        self.feed(rec)
        self.assertEqual(vad.calls[-2:], ["reset", "accept"])
        self.assertEqual(len(self.blocks), 2)
        self.assertEqual(self.suppression_changes, [True, False])

    def test_rms_discards_incomplete_utterance_and_resumes_with_fresh_audio(self):
        rec = self.recorder()
        self.feed(rec, 0.1)
        self.guard_state = (1, float("inf"))
        self.feed(rec, 0.9)
        self.guard_state = (1, 10.4)
        self.feed(rec, 0.9)
        self.assertEqual(self.segments, [])
        self.now.return_value = 10.5
        self.feed(rec, 0.2)
        self.feed(rec, 0.0)
        self.assertEqual(len(self.segments), 1)
        np.testing.assert_array_equal(self.segments[0],
                                      np.concatenate([np.full(512, 0.2, dtype=np.float32),
                                                      np.zeros(512, dtype=np.float32)]))

    def test_vad_only_emits_user_segments_before_and_after_playback(self):
        class SegmentingVad(FakeVad):
            def __init__(self):
                super().__init__()
                self.segments = []

            def reset(self):
                super().reset()
                self.segments.clear()

            def accept_waveform(self, block):
                self.segments.append(block.copy())

            def empty(self):
                return not self.segments

            @property
            def front(self):
                return SimpleNamespace(samples=self.segments[0])

            def pop(self):
                self.segments.pop(0)

        rec = self.recorder(SegmentingVad())
        self.feed(rec, 0.2)
        self.guard_state = (1, float("inf"))
        self.feed(rec, 0.9)
        self.guard_state = (1, 10.4)
        self.feed(rec, 0.9)
        self.now.return_value = 10.5
        self.feed(rec, 0.3)
        self.assertEqual(len(self.segments), 2)
        np.testing.assert_array_equal(self.segments[0], np.full(512, 0.2, dtype=np.float32))
        np.testing.assert_array_equal(self.segments[1], np.full(512, 0.3, dtype=np.float32))

    def test_completed_user_segments_remain_queued_when_playback_starts(self):
        rec = self.recorder()
        self.feed(rec, 0.1)
        self.feed(rec, 0.0)
        user_audio = self.segments[0]
        self.guard_state = (1, float("inf"))
        self.feed(rec, 0.9)
        rec.stop()
        self.assertEqual(len(self.segments), 1)
        self.assertIs(self.segments[0], user_audio)

    def test_short_playback_between_callbacks_still_clears_old_buffer(self):
        rec = self.recorder()
        self.feed(rec)
        self.guard_state = (1, 10.4)
        self.now.return_value = 10.5
        self.feed(rec, 0.0)
        self.assertEqual(self.segments, [])
        self.assertFalse(rec._has_speech)

    def test_delayed_input_block_from_protection_window_is_discarded(self):
        rec = self.recorder()
        self.guard_state = (1, 10.4)
        self.now.return_value = 10.5
        self.feed(rec, 0.9, SimpleNamespace(currentTime=100.5, inputBufferAdcTime=100.3))
        self.assertEqual(self.blocks, [])
        self.feed(rec, 0.2, SimpleNamespace(currentTime=100.5, inputBufferAdcTime=100.45))
        self.assertEqual(len(self.blocks), 1)

    def test_playback_state_change_only_resets_vad_on_capture_thread(self):
        vad = FakeVad()
        rec = self.recorder(vad)
        vad.calls.clear()
        self.guard_state = (1, float("inf"))
        self.assertEqual(vad.calls, [])
        self.feed(rec)
        self.assertEqual(vad.calls, ["reset"])

    def test_stop_during_playback_or_pending_discard_does_not_flush_audio(self):
        for playing in (True, False):
            with self.subTest(playing=playing):
                self.guard_state = (0, 0.0)
                rec = self.recorder()
                self.feed(rec)
                if playing:
                    self.guard_state = (1, float("inf"))
                else:
                    rec.discard_current()
                rec.stop()
                self.assertEqual(self.segments, [])


if __name__ == "__main__":
    unittest.main()
