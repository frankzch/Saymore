import ast
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from saymore.reminder import chat
from saymore.reminder.mode import ReminderMode


class SpeechFeedbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / "saymore/main.py"
        module = ast.parse(path.read_text(encoding="utf-8"))
        main = next(node for node in module.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        cls.functions = {node.name: node for node in main.body if isinstance(node, ast.FunctionDef)}

    def setUp(self):
        self.state = {"mode": "awake", "quit": False}
        self.buffer = Mock()
        self.buffer.text_parts = ("", "", False)
        self.reminder = Mock()
        self.reminder.is_to_dictation.side_effect = lambda word: word == "听写模式"
        self.environment = dict(
            state=self.state, buffer=self.buffer, reminder=self.reminder,
            time=SimpleNamespace(time=lambda: 100), threading=Mock(),
            tts=SimpleNamespace(speak=Mock(), play_cue=Mock()),
            SENTENCE_END="。！？.!?…~", send_words={"发送"}, confirm_words={"确认"},
            polish_words={"整理"}, sleep_words={"休眠"}, quit_words={"退出"},
            undo_words={"撤销"}, clear_words={"清空"}, reminder_enter_words=set(),
            click_permission_button=Mock(), focus_window=Mock(return_value=True),
            focus_title="", focus_input="", keyboard=Mock(), hotwords=Mock(),
            flush_pending_audio=Mock(), auto_enter=False,
            enter_sleep=Mock(), confirm_quit=Mock(),
        )
        for name in ("_send_buffer_impl", "send_buffer", "run_global_command", "emit",
                     "handle_reminder_turn", "flush_reminder_buffer"):
            exec(compile(ast.Module(body=[self.functions[name]], type_ignores=[]),
                         "<main-behavior>", "exec"), self.environment)

    def assert_no_feedback(self):
        self.environment["threading"].Thread.assert_not_called()
        self.environment["tts"].speak.assert_not_called()
        self.environment["tts"].play_cue.assert_not_called()

    def test_dictation_keeps_user_acknowledgment_words_without_reply(self):
        words = ["收到", "好的", "嗯", "正文内容"]
        for word in words:
            self.environment["emit"](word)
        self.assertEqual([call.args[0] for call in self.buffer.add.call_args_list], words)
        self.assert_no_feedback()

    def test_commands_execute_without_acknowledgment(self):
        self.state["panel"] = self.buffer
        self.buffer.flush_all.return_value = True
        for command in ("发送", "确认", "整理", "休眠"):
            with self.subTest(command=command):
                self.assertTrue(self.environment["run_global_command"](command))
        self.buffer.flush_all.assert_called_once_with()
        self.buffer.trigger_polish_now.assert_called_once_with()
        self.environment["enter_sleep"].assert_called_once()
        self.environment["click_permission_button"].assert_called_once_with("")
        self.assert_no_feedback()

    def test_undo_and_clear_execute_without_acknowledgment(self):
        self.buffer.undo.return_value = True
        self.environment["emit"]("撤销")
        self.environment["emit"]("清空")
        self.buffer.undo.assert_called_once_with()
        self.buffer.clear.assert_called_once_with()
        self.assert_no_feedback()

    def test_reminder_collects_sentences_and_flushes_without_filler(self):
        self.state["mode"] = "reminder"
        self.environment["handle_reminder_turn"]("明天三点")
        self.environment["handle_reminder_turn"]("提醒开会")
        self.assertEqual(self.state["rbuf"], ["明天三点", "提醒开会"])
        self.environment["flush_reminder_buffer"]()
        self.reminder.handle_turn.assert_called_once_with("明天三点提醒开会")
        self.assertEqual(self.state["rbuf"], [])
        self.assert_no_feedback()

    def test_busy_and_mode_switch_do_not_reply(self):
        self.state.update(mode="reminder", llm_busy=True)
        self.environment["handle_reminder_turn"]("新的内容")
        self.assertNotIn("rbuf", self.state)
        self.environment["handle_reminder_turn"]("听写模式")
        self.assertEqual(self.state["mode"], "awake")
        self.reminder.clear.assert_called_once_with()
        self.assert_no_feedback()

    def test_reminder_model_filler_is_silent_but_substantive_result_is_spoken(self):
        reminder = ReminderMode.__new__(ReminderMode)
        for text in ("收到", "好的。", "嗯", "嗯，好的，收到。", "", " "):
            with self.subTest(text=text), patch.object(reminder, "_run_turn", return_value=(text, [])), \
                    patch("saymore.reminder.mode.tts.speak") as speak:
                reminder.handle_turn("用户内容")
                speak.assert_not_called()
        result = "好的，已添加明天三点的开会提醒。"
        with patch.object(reminder, "_run_turn", return_value=(result, [])), \
                patch("saymore.reminder.mode.tts.speak") as speak:
            reminder.handle_turn("用户内容")
            speak.assert_called_once_with(result)

    def test_stopping_nag_does_not_play_another_acknowledgment(self):
        with tempfile.TemporaryDirectory() as directory:
            reminder = ReminderMode({"reminders_file": "reminders.json"},
                                    lambda name: str(Path(directory) / name), None)
            stop = threading.Event()
            reminder._nag = {"stop": stop}
            reminder.on_nag_stop = Mock()
            with patch("saymore.reminder.mode.threading.Thread") as thread, \
                    patch("saymore.reminder.mode.tts.speak") as speak:
                reminder.stop_nag()
                reminder.stop_nag()
                speak.assert_not_called()
                thread.assert_not_called()
            self.assertTrue(stop.is_set())
            reminder.on_nag_stop.assert_called_once_with()

    def test_model_missing_say_does_not_insert_default_acknowledgment(self):
        with patch.object(chat, "build_system", return_value="system"):
            say, actions = chat.handle(Mock(), [], "用户内容", None,
                                       chat_fn=lambda messages, cfg: '{"actions": []}')
        self.assertEqual(say, "")
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
