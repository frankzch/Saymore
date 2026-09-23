import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from saymore.hotwords.learn import HotWords, rank_terms
from saymore import paths
from saymore.ui.settings import _wordlist_path


class HotwordRankingTest(unittest.TestCase):
    def test_packaged_data_moves_out_of_install_dir_and_settings_uses_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            install = root / "install"
            install.mkdir()
            (install / "hotwords.json").write_text('{"done":{},"terms":{}}', encoding="utf-8")
            (install / "hotwords.txt").write_text("旧词\n", encoding="utf-8")
            history = install / "typed_history"
            history.mkdir()
            (history / "2026-09-23.jsonl").write_text("旧记录\n", encoding="utf-8")
            with patch.object(paths.sys, "frozen", True, create=True), \
                 patch.object(paths, "PROJECT_ROOT", install), \
                 patch.dict(paths.os.environ, {"APPDATA": str(root / "roaming")}):
                saved = root / "roaming" / "Saymore"
                for name in ("hotwords.json", "hotwords.txt", "typed_history"):
                    self.assertEqual(paths._resolve(name), saved / name)
                    self.assertTrue((saved / name).exists())
                    self.assertFalse((install / name).exists())
                self.assertEqual(_wordlist_path("hotwords_file", {}, install), saved / "hotwords.txt")
                self.assertEqual(paths._resolve("hotwords.json"), saved / "hotwords.json")

    def test_two_rankings_are_persisted_and_recency_has_two_thirds_weight(self):
        terms = {
            "最新一次": {"count": 1, "last_seen": "2026-08-23 10:00:00"},
            "较新两次": {"count": 2, "last_seen": "2026-08-22 10:00:00"},
            "很老百次": {"count": 100, "last_seen": "2025-01-01 10:00:00"},
        }

        self.assertEqual(rank_terms(terms), ["最新一次", "较新两次", "很老百次"])
        self.assertEqual(terms["最新一次"]["recency_rank"], 1)
        self.assertEqual(terms["最新一次"]["frequency_rank"], 3)
        self.assertAlmostEqual(terms["最新一次"]["weighted_rank"], 5 / 3)

    def test_equal_values_share_rank(self):
        terms = {
            "甲词": {"count": 2, "last_seen": "2026-08-23 10:00:00"},
            "乙词": {"count": 2, "last_seen": "2026-08-23 10:00:00"},
        }

        rank_terms(terms)

        self.assertEqual(terms["甲词"]["recency_rank"], terms["乙词"]["recency_rank"])
        self.assertEqual(terms["甲词"]["frequency_rank"], terms["乙词"]["frequency_rank"])

    def test_distill_records_count_time_and_writes_ranked_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history = root / "typed_history"
            history.mkdir()
            rows = [
                {"t": "2026-08-22 10:00:00", "final": "老词出现了两遍：老词"},
                {"t": "2026-08-23 10:00:00", "final": "这是最新词"},
            ]
            (history / "2026-08-23.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            hotwords = HotWords(
                lambda name: root / name,
                lambda _system, _text: "老词\n老词\n最新词",  # extract 人格训练格式：每行一词
            )

            hotwords._distill()

            state = json.loads((root / "hotwords.json").read_text(encoding="utf-8"))
            self.assertEqual(state["terms"]["老词"]["count"], 2)
            self.assertEqual(state["terms"]["老词"]["last_seen"], "2026-08-22 10:00:00")
            self.assertEqual(state["terms"]["最新词"]["last_seen"], "2026-08-23 10:00:00")
            self.assertEqual(
                (root / "hotwords.txt").read_text(encoding="utf-8").splitlines(),
                ["最新词", "老词"],
            )


if __name__ == "__main__":
    unittest.main()
