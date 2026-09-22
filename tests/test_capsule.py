import ctypes
import unittest

from PIL import Image

from saymore.ui import panel
from saymore.ui.capsule import (CapsuleText, Morph, default_position, draw_capsule,
                                expansion_direction, expansion_bounds, expansion_offset)
from saymore.ui.capsule_toolbar import HEADER_HEIGHT, button_rects, draw_toolbar, hit_button


class MorphTest(unittest.TestCase):
    def test_open_retarget_and_close_without_position_jump(self):
        animation = Morph(48)
        animation.move((320, 48, 24), 0)
        middle = animation.sample(0.1)
        self.assertTrue(48 < middle[0] < 320)
        self.assertEqual(animation.move((360, 160, 16), 0.1), middle)
        self.assertEqual(animation.sample(0.4), (360, 160, 16))
        animation.move((48, 48, 24), 0.4)
        self.assertEqual(animation.sample(0.7), (48, 48, 24))
        self.assertFalse(animation.moving)

    def test_default_is_near_work_area_bottom_right(self):
        for area in ((0, 0, 1920, 1040), (100, 50, 1380, 770)):
            x, y = default_position(area, 48)
            self.assertEqual(area[2] - x - 48, 24)
            self.assertEqual(area[3] - y - 48, 24)

    def test_four_corners_expand_into_available_space(self):
        area = (0, 0, 1280, 720)
        for anchor, expected in (((24, 24), (1, 1)), ((1208, 24), (-1, 1)),
                                 ((24, 648), (1, -1)), ((1208, 648), (-1, -1))):
            direction = expansion_direction(area, anchor, 48, (324, 240))
            self.assertEqual(direction, expected)
            room = expansion_bounds(area, anchor, 48, direction)
            self.assertGreaterEqual(room[0], 324)
            self.assertGreaterEqual(room[1], 240)
            dx, dy = expansion_offset(area, anchor, 48, (324, 240), direction)
            self.assertGreaterEqual(anchor[0] + dx, 0)
            self.assertGreaterEqual(anchor[1] + dy, 0)
            self.assertLessEqual(anchor[0] + dx + 324, 1280)
            self.assertLessEqual(anchor[1] + dy + 240, 720)

    def test_direction_stays_stable_near_screen_center(self):
        self.assertEqual(expansion_direction((0, 0, 1920, 1040), (900, 500), 48,
                                             (324, 240), (-1, -1)), (-1, -1))

    def test_reversing_direction_interpolates_window_origin(self):
        animation = Morph((324, 240, 16, -276, -192))
        animation.move((324, 240, 16, 0, 0), 0)
        self.assertEqual(animation.sample(0), (324, 240, 16, -276, -192))
        middle = animation.sample(0.1)
        self.assertTrue(-276 < middle[3] < 0)
        self.assertTrue(-192 < middle[4] < 0)
        self.assertEqual(animation.sample(0.3)[3:], (0, 0))

    def test_toolbar_hits_same_icons_at_different_dpi(self):
        for scale in (1, 1.25, 1.75, 2):
            rects = button_rects(round(324 * scale), scale)
            self.assertEqual(len(rects), 4)
            for action, (l, t, r, b) in rects.items():
                self.assertGreater(l, round(100 * scale))
                self.assertEqual(hit_button(rects, (l + r) // 2, (t + b) // 2), action)
            self.assertIsNone(hit_button(rects, 0, 0))

    def test_toolbar_has_no_separator_line(self):
        canvas = Image.new("RGBA", (300, 100))
        draw_toolbar(canvas, 1)
        self.assertEqual(canvas.getpixel((150, HEADER_HEIGHT))[3], 0)

    def test_outline_has_transparent_corners_and_solid_center(self):
        image = draw_capsule((48, 48, 24), 48, None, (236, 239, 245), (52, 199, 89))
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.getpixel((24, 24))[3], 255)


class NativeCapsuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kernel = ctypes.windll.kernel32
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        cls.view = CapsuleText(kernel.GetModuleHandleW(None), width=300, font_size=16, pad=4, max_h=240)

    @classmethod
    def tearDownClass(cls):
        cls.view.destroy()

    def test_one_line_without_status_fits_capsule(self):
        view = self.view
        view.prepare("你好，世界。", "", "", "", False, 228, 340)
        self.assertEqual(view._status_h, 0)
        self.assertLessEqual(view.h + 12, 48)
        self.assertEqual(view._get_text(), "你好，世界。")
        colors = view.snapshot.convert("RGB").getcolors(view.w * view.h)
        self.assertGreater(len(colors), 2, "动画快照必须包含实际字形，不能只有底色")

    def test_first_transcript_reflows_before_height_measurement(self):
        view = self.view
        view.prepare("", "", "正在识别…", "", False, 228, 340)
        view.prepare("", "这是第一句话，用来测试缓存窗口的高度。", "", "", False, 228, 340)
        line_h = view._line_h()
        line_count = view.u.SendMessageW(view.edit, panel._EM_GETLINECOUNT, 0, 0)
        expected = panel._text_area_height(line_count, line_h) + view.pad * 2
        self.assertEqual(view.h, expected)

    def test_status_remains_visible_alongside_body(self):
        view = self.view
        for hint in ("正在识别…", "正在整理…"):
            view.prepare("字", "", hint, "", False, 228, 340)
            self.assertEqual(view._status_text, hint)
            self.assertGreater(view._status_h, 0)
            self.assertEqual(view._get_text(), "字")
            self.assertGreater(view.content_w, 50)

    def test_long_body_scrolls_in_bounded_height(self):
        view = self.view
        view.prepare("很长的缓存文字。" * 100, "", "", "", False, 100, 340)
        self.assertLessEqual(view.h, 100)
        self.assertIsNotNone(view._thumb_metrics())
        self.assertGreater(view._first_line(), 0)

    def test_clear_retains_snapshot_for_closing_animation(self):
        view = self.view
        view.prepare("待发送", "", "", "", False, 228, 340)
        snapshot = view.snapshot
        view.prepare("", "", "", "", False, 228, 340)
        self.assertFalse(view.present)
        self.assertIs(view.snapshot, snapshot)

    def test_warning_keeps_panel_open(self):
        view = self.view
        view.prepare("", "", "", "没找到输入框，请先点击输入框再发送。", False, 228, 340)
        self.assertTrue(view.present)
        self.assertGreater(view._warn_h, 0)

    def test_edit_save_and_cancel_keep_original_callbacks(self):
        view = self.view
        results = []
        view.on_edit_end = results.append
        view.prepare("原文", "", "", "", False, 228, 340)
        view._start_edit()
        view.u.SendMessageW(view.edit, 0x000C, 0, ctypes.c_wchar_p("修改后的文字"))
        view._end_edit(save=True)
        self.assertEqual(results, ["修改后的文字"])
        self.assertFalse(view.editing)
        view._start_edit()
        view.cancel_edit()
        self.assertEqual(results, ["修改后的文字", None])
        view.on_edit_end = None


if __name__ == "__main__":
    unittest.main()
