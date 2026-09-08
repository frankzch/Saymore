import unittest

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


if __name__ == "__main__":
    unittest.main()
