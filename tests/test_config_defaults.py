import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from saymore.config import DEFAULT_CONFIG, load_config


class AudioSensitivityDefaultsTest(unittest.TestCase):
    def test_new_install_uses_more_sensitive_audio_defaults(self):
        expected = {
            "kws_threshold": 0.12,
            "vad_threshold": 0.25,
            "silence_rms": 0.004,
            "min_speech_peak": 0.02,
            "asr_min_confidence": 0.6,
        }
        self.assertEqual({key: DEFAULT_CONFIG[key] for key in expected}, expected)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            with patch("saymore.config.CONFIG_PATH", config_path):
                self.assertEqual(
                    {key: load_config()[key] for key in expected},
                    expected,
                )
            self.assertTrue(config_path.exists())


if __name__ == "__main__":
    unittest.main()
