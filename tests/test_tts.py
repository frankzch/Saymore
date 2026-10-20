import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import saymore.tts as tts
from saymore.ui.settings import save


class TtsControlTest(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.config_path = Path(tmpdir.name) / "config.json"
        self.config_path.write_text("{}", encoding="utf-8")
        for name, value in {
            "CONFIG_PATH": self.config_path,
            "_lock": threading.Lock(),
            "_config_lock": threading.Lock(),
            "_config_version": None,
            "_enabled": True,
            "_cancelled": threading.Event(),
            "_cancel_generation": 0,
            "_watcher_started": True,
            "playing": False,
            "_capture_state": (0, 0.0),
        }.items():
            patched = patch.object(tts, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def set_enabled(self, enabled):
        save(self.config_path, {"tts_enabled": enabled})
        tts._refresh_config()

    def test_disabled_skips_all_synthesis_and_playback(self):
        self.set_enabled(False)
        with (
            patch.object(tts, "_ensure_cue") as ensure,
            patch.object(tts, "_speak_edge") as edge,
            patch.object(tts, "_speak_sapi") as sapi,
            patch.object(tts, "_play_mp3") as mp3,
        ):
            tts.speak("reply")
            tts.play_cue("acknowledged")
            tts.prefetch_cue("wake up")
        for call in (ensure, edge, sapi, mp3):
            call.assert_not_called()
        self.assertFalse(tts.playing)

    def test_missing_setting_defaults_to_enabled_and_reenable_accepts_new_speech(self):
        with patch.object(tts, "_speak_edge") as edge:
            tts.speak("first")
            token = edge.call_args.args[1]
            self.set_enabled(False)
            self.assertTrue(token.is_set())
            tts.speak("muted")
            self.assertEqual(edge.call_count, 1)
            self.set_enabled(True)
            tts.speak("new")
            self.assertEqual(edge.call_count, 2)
            self.assertIsNot(edge.call_args.args[1], token)
            self.assertFalse(edge.call_args.args[1].is_set())

    def test_quick_off_on_cancels_old_requests_even_between_config_polls(self):
        token = tts._request_token()
        save(self.config_path, {"tts_enabled": False})
        save(self.config_path, {"tts_enabled": True})
        fresh = tts._request_token()
        self.assertTrue(token.is_set())
        self.assertIsNot(fresh, token)
        self.assertFalse(fresh.is_set())

    def test_partial_config_write_preserves_disabled_state_and_retries(self):
        self.set_enabled(False)
        self.config_path.write_text('{"tts_enabled":', encoding="utf-8")
        self.assertIsNone(tts._request_token())
        self.config_path.write_text('{"tts_enabled": true}', encoding="utf-8")
        self.assertIsNotNone(tts._request_token())

    def test_disable_during_cue_synthesis_prevents_playback_and_fallback(self):
        for result in ("cue.mp3", None):
            with self.subTest(result=result):
                self.set_enabled(True)

                def synthesize(*args):
                    self.set_enabled(False)
                    return result

                with (
                    patch.object(tts, "_ensure_cue", side_effect=synthesize),
                    patch.object(tts, "_play_mp3") as mp3,
                    patch.object(tts, "_speak_sapi") as sapi,
                ):
                    tts.play_cue("cue")
                mp3.assert_not_called()
                sapi.assert_not_called()

    def test_disable_during_online_failure_prevents_offline_fallback(self):
        def fail(text, cancelled):
            self.set_enabled(False)
            raise RuntimeError("offline")

        with (
            patch.object(tts, "_speak_edge", side_effect=fail),
            patch.object(tts, "_speak_sapi") as sapi,
        ):
            tts.speak("reply")
        sapi.assert_not_called()

    def test_enabled_falls_back_to_sapi_for_speech_and_cues(self):
        with (
            patch.object(tts, "_speak_edge", side_effect=RuntimeError("offline")),
            patch.object(tts, "_ensure_cue", return_value=None),
            patch.object(tts, "_speak_sapi") as sapi,
        ):
            tts.speak("reply")
            tts.play_cue("cue")
        self.assertEqual([call.args[0] for call in sapi.call_args_list], ["reply", "cue"])

    def test_queued_speech_is_dropped_after_off_on_cycle(self):
        queued = threading.Event()
        errors = []
        request_token = tts._request_token

        def capture_token():
            token = request_token()
            queued.set()
            return token

        def speak():
            try:
                tts.speak("queued")
            except BaseException as exc:
                errors.append(exc)

        with (
            patch.object(tts, "_request_token", side_effect=capture_token),
            patch.object(tts, "_speak_edge") as edge,
        ):
            with tts._lock:
                worker = threading.Thread(target=speak, daemon=True)
                worker.start()
                self.assertTrue(queued.wait(2))
                self.set_enabled(False)
                self.set_enabled(True)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            edge.assert_not_called()
            tts.speak("new")
            self.assertEqual(edge.call_args.args[0], "new")

    def test_mp3_playback_stops_and_closes_on_disable(self):
        cancelled = tts._request_token()
        commands = []

        def mci(command, buf, size, callback):
            commands.append(command)
            if command.startswith("status"):
                self.assertTrue(tts.playing)
                self.assertTrue(tts.input_suppressed())
                buf.value = "playing"
                self.set_enabled(False)
            return 0

        with patch.object(tts.ctypes.windll.winmm, "mciSendStringW", side_effect=mci):
            tts._play_mp3("cue.mp3", cancelled)
        self.assertEqual(commands[-2:], ["stop edgetts_play", "close edgetts_play"])
        self.assertFalse(tts.playing)

    def test_sapi_playback_is_purged_on_disable(self):
        cancelled = tts._request_token()
        voice = Mock()

        def wait(timeout):
            self.assertTrue(tts.playing)
            self.assertTrue(tts.input_suppressed())
            self.set_enabled(False)
            return False

        voice.WaitUntilDone.side_effect = wait
        with (
            patch.object(tts.comtypes, "CoInitialize"),
            patch.object(tts.comtypes_cache, "ensure"),
            patch.object(tts.comtypes.client, "CreateObject", return_value=voice),
        ):
            tts._speak_sapi("reply", cancelled)
        self.assertEqual([call.args for call in voice.Speak.call_args_list],
                         [("reply", 1), ("", 3)])
        self.assertFalse(tts.playing)

    def test_completed_sapi_playback_clears_playing_flag(self):
        voice = Mock()
        voice.WaitUntilDone.return_value = True
        with (
            patch.object(tts.comtypes, "CoInitialize"),
            patch.object(tts.comtypes_cache, "ensure"),
            patch.object(tts.comtypes.client, "CreateObject", return_value=voice),
        ):
            tts._speak_sapi("reply", tts._request_token())
        voice.Speak.assert_called_once_with("reply", 1)
        self.assertFalse(tts.playing)

    def test_config_watcher_detects_change_while_idle(self):
        class WatcherStopped(Exception):
            pass

        token = tts._request_token()
        polls = []

        def next_poll(seconds):
            polls.append(seconds)
            if len(polls) == 1:
                save(self.config_path, {"tts_enabled": False})
                return
            raise WatcherStopped()

        with patch.object(tts, "time", SimpleNamespace(sleep=next_poll)):
            with self.assertRaises(WatcherStopped):
                tts._watch_config()
        self.assertTrue(token.is_set())
        self.assertIsNone(tts._request_token())

    def test_mp3_protects_capture_before_play_and_after_close(self):
        clock = Mock(return_value=10.0)

        def mci(command, buf, size, callback):
            if command.startswith("play"):
                self.assertTrue(tts.input_suppressed())
                self.assertEqual(tts.capture_state()[0], 1)
            if command.startswith("status"):
                buf.value = "stopped"
            if command.startswith("close"):
                self.assertTrue(tts.input_suppressed())
            return 0

        with (
            patch.object(tts, "time", SimpleNamespace(monotonic=clock)),
            patch.object(tts.ctypes.windll.winmm, "mciSendStringW", side_effect=mci),
        ):
            tts._play_mp3("cue.mp3", threading.Event())
            self.assertEqual(tts.capture_state(), (1, 10.4))
            clock.return_value = 10.399
            self.assertTrue(tts.input_suppressed())
            clock.return_value = 10.401
            self.assertFalse(tts.input_suppressed())
        self.assertFalse(tts.playing)

    def test_failed_playback_releases_capture_with_tail_protection(self):
        voice = Mock()
        voice.Speak.side_effect = [RuntimeError("device failed"), None]
        with (
            patch.object(tts.comtypes, "CoInitialize"),
            patch.object(tts.comtypes_cache, "ensure"),
            patch.object(tts.comtypes.client, "CreateObject", return_value=voice),
        ):
            with self.assertRaisesRegex(RuntimeError, "device failed"):
                tts._speak_sapi("reply", threading.Event())
        self.assertEqual(voice.Speak.call_args.args, ("", 3))
        self.assertFalse(tts.playing)
        self.assertNotEqual(tts.capture_state()[1], float("inf"))
        self.assertTrue(tts.input_suppressed())

    def test_synthesis_and_disabled_speech_do_not_suppress_capture(self):
        with patch.object(tts, "_ensure_cue", return_value="cue.mp3"):
            tts.prefetch_cue("wake up")
        self.assertEqual(tts.capture_state(), (0, 0.0))
        self.set_enabled(False)
        tts.speak("muted")
        tts.play_cue("muted")
        self.assertFalse(tts.input_suppressed())
        self.assertEqual(tts.capture_state()[0], 0)


if __name__ == "__main__":
    unittest.main()
