"""真实消息循环验收；使用假的缓存和发送回调，不调用语音模型或外部输入框。"""
import ctypes
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

from saymore.ui import overlay
from saymore.ui.capsule_toolbar import button_rects


class CapsuleWindowTest(unittest.TestCase):
    def test_toolbar_actions_and_reposition(self):
        class Buffer:
            text_parts = ("用于测试的缓存文字。", "", False)
            cleaning_mode = None
            countdown = None

            def pause(self):
                pass

            def resume(self):
                pass

            def replace_all(self, text):
                self.text_parts = (text, "", False)

            def trigger_polish_now(self):
                polished.set()

        polished = threading.Event()
        buffer = Buffer()
        state = {"mode": "awake", "status": "awake", "levels": [], "quit": False,
                 "panel": buffer, "runtime": {"ready": True}, "glass_cfg": {"max_h": 240}}
        views, sent, copied, notices, errors = [], [], [], [], []
        opened = threading.Event()
        original_toolbar = overlay.draw_toolbar

        def draw_toolbar(*args, **kwargs):
            result = original_toolbar(*args, **kwargs)
            notices.append(kwargs.get("notice", ""))
            if args[-1] >= 1:
                opened.set()
            return result

        class View(overlay.CapsuleText):
            _CLASS = "SaymoreCapsuleWindowTest"

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                views.append(self)

        def send():
            sent.append(buffer.text_parts[0])
            buffer.text_parts = ("", "", False)

        state["send_panel"] = send
        u = ctypes.windll.user32
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]

        def drive():
            try:
                for _ in range(80):
                    if views and u.GetWindowLongPtrW(views[0].hwnd, -8):
                        break
                    time.sleep(0.05)
                # 测试线程与 UI 线程使用相同坐标系，避免窗口矩形被 DPI 虚拟化。
                u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
                view = views[0]
                hwnd = u.GetWindowLongPtrW(view.hwnd, -8)
                scale = u.GetDpiForSystem() / 96
                diameter = round(48 * scale)

                def rect():
                    result = wintypes.RECT()
                    u.GetWindowRect(hwnd, ctypes.byref(result))
                    return result

                def click(action=None):
                    bounds = rect()
                    if action:
                        l, t, r, b = button_rects(bounds.right - bounds.left, scale)[action]
                        x, y = (l + r) // 2, (t + b) // 2
                    else:
                        x = y = diameter // 2
                    lp = x | (y << 16)
                    u.SendMessageW(hwnd, 0x0201, 0, lp)
                    u.SendMessageW(hwnd, 0x0202, 0, lp)

                self.assertTrue(opened.wait(8), "标题栏动画未完成")
                self.assertEqual(u.SendMessageW(hwnd, 0x0021, 0, 0), 3)
                click("copy")
                self.assertEqual(copied, ["用于测试的缓存文字。"])
                click("polish")
                self.assertTrue(polished.wait(2), "整理键未触发整理")
                click()  # 点猫收起，缓存保留
                time.sleep(0.4)
                self.assertEqual(rect().right - rect().left, diameter)
                self.assertTrue(buffer.text_parts[0])
                click()  # 再点猫展开
                time.sleep(0.4)
                self.assertGreater(rect().right - rect().left, diameter)
                click("edit")
                self.assertTrue(view.editing)
                u.SendMessageW(view.edit, 0x000C, 0, ctypes.c_wchar_p("修改后的文字"))
                click("send")  # 编辑中其余键变灰不响应
                self.assertTrue(view.editing)
                click("edit")  # 再点编辑键＝保存退出
                self.assertFalse(view.editing)
                click("send")
                time.sleep(0.4)
                self.assertEqual(sent, ["修改后的文字"])
                self.assertFalse(view.editing)
                self.assertEqual(rect().right - rect().left, diameter)
                state["speaking"] = True
                time.sleep(0.4)
                bounds = rect()
                self.assertGreater(bounds.right - bounds.left, diameter)
                self.assertEqual(bounds.bottom - bounds.top, diameter)
                self.assertIn("正在说话…", notices)
                state["speaking"] = False
                time.sleep(0.4)
                buffer.text_parts = ("拖到左上角以后向右下展开。" * 15, "", False)
                area = wintypes.RECT()
                u.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)
                state["capsule_offset"] = [(area.right - area.left - diameter - 24) / scale,
                                            (area.bottom - area.top - diameter - 24) / scale]
                u.SendMessageW(hwnd, 0x007E, 0, 0)
                time.sleep(0.5)
                bounds = rect()
                self.assertAlmostEqual(bounds.left, area.left + 24, delta=2)
                self.assertAlmostEqual(bounds.top, area.top + 24, delta=2)
                self.assertLessEqual(bounds.right, area.right)
                self.assertLessEqual(bounds.bottom, area.bottom)
            except Exception as exc:
                errors.append(exc)
            finally:
                state["quit"] = True

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(overlay, "CONFIG_PATH", Path(tmp) / "config.json"), \
                patch.object(overlay.tray, "TrayIcon"), \
                patch.object(overlay, "CapsuleText", View), \
                patch.object(overlay, "draw_toolbar", draw_toolbar), \
                patch("pyperclip.copy", copied.append):
            driver = threading.Thread(target=drive, daemon=True)
            driver.start()
            overlay.run_overlay(state)
            driver.join(1)
        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main()
