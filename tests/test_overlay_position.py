import unittest

from saymore.ui.overlay import _dpi_scale, _overlay_position, _panel_font_size


class OverlayPositionTest(unittest.TestCase):
    def test_dpi_scale_follows_windows_display_scaling(self):
        self.assertEqual(_dpi_scale(96), 1.0)
        self.assertEqual(_dpi_scale(120), 1.25)
        self.assertEqual(_dpi_scale(144), 1.5)
        self.assertEqual(_dpi_scale(192), 2.0)

    def test_panel_font_defaults_to_system_size_without_scaling_twice(self):
        self.assertEqual(_panel_font_size(None, 144, 18), 18)
        self.assertEqual(_panel_font_size(16, 144, 18), 24)

    def test_default_position_tracks_bottom_right_across_resolutions(self):
        self.assertEqual(_overlay_position((0, 0, 1920, 1040)), (1822, 870))
        self.assertEqual(_overlay_position((0, 0, 3840, 2120)), (3742, 1950))

    def test_saved_offset_is_relative_to_work_area(self):
        self.assertEqual(_overlay_position((0, 0, 2560, 1400), (25, 35)), (2477, 1285))

    def test_position_stays_inside_work_area(self):
        self.assertEqual(_overlay_position((100, 50, 900, 650), (5000, 5000)), (100, 50))


if __name__ == "__main__":
    unittest.main()
