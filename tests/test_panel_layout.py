import unittest

from saymore.ui.panel import _text_area_height, _text_bottom_slack, _visible_line_count


class PanelLayoutTest(unittest.TestCase):
    def test_text_area_reserves_bottom_slack_for_last_line(self):
        self.assertEqual(_text_bottom_slack(20), 3)
        self.assertEqual(_text_area_height(2, 20), 43)

    def test_bottom_slack_is_not_counted_as_an_extra_visible_line(self):
        self.assertEqual(_visible_line_count(43, 20), 2)
        self.assertEqual(_visible_line_count(42, 20), 1)


if __name__ == "__main__":
    unittest.main()
