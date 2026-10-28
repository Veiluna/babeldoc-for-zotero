"""T2 切行的公开接口回归，不读取 PDF。"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon/content/python"))
from ztf_split_types import Char
from ztf_split_lines import build_lines


class BuildLinesTests(unittest.TestCase):
    def test_empty_page(self):
        self.assertEqual(build_lines(0, ()), ())

    def test_overlapping_boxes_join_superscript_without_transitive_baselines(self):
        chars = [Char(0, 0, 0, 10, 10, 15, 20, "x", 2),
                 Char(1, 1, 0, 15, 8, 18, 15, "2", -1),
                 Char(2, 2, 0, 18, 10, 23, 20, "y", 3),
                 Char(3, 3, 0, 10, 19, 15, 29, "z", 4)]
        lines = build_lines(0, chars)
        self.assertEqual([line.char_ids for line in lines], [(0, 1, 2), (3,)])
        self.assertEqual(lines[0].baseline, 20)
        self.assertEqual(lines[0].paragraphs, (2, 3))
        self.assertEqual((lines[0].y0, lines[0].y1), (8, 20))

    def test_same_baseline_disconnected_runs_and_backwards_writes_stay_separate(self):
        chars = [Char(0, 0, 0, 10, 10, 15, 20, "a"),
                 Char(1, 1, 0, 16, 10, 21, 20, "b"),
                 Char(2, 2, 0, 100, 10, 105, 20, "c"),
                 Char(3, 3, 0, 10, 10, 15, 20, "d")]
        self.assertEqual([line.char_ids for line in build_lines(0, chars)],
                         [(0,), (1,), (2,), (3,)])

    def test_every_positive_gap_is_preserved_with_shared_baseline(self):
        for gap in (0.000001, 5, 50):
            with self.subTest(gap=gap):
                chars = (Char(0, 0, 0, 0, 10, 10, 20, "a", 0),
                         Char(1, 1, 0, 10 + gap, 8, 15 + gap, 15, "2", 1),
                         Char(2, 2, 0, 15 + gap, 10, 25 + gap, 20, "b", 1))
                lines = build_lines(0, chars)
                self.assertEqual([row.char_ids for row in lines], [(0,), (1, 2)])
                self.assertEqual([row.baseline for row in lines], [20, 20])
                self.assertEqual((lines[0].x1, lines[1].x0), (10, 10 + gap))
                self.assertEqual([row.paragraphs for row in lines], [(0,), (1,)])

    def test_overlapping_character_covers_apparent_gap_between_later_characters(self):
        chars = (Char(0, 0, 0, 0, 10, 30, 20, "a"),
                 Char(1, 1, 0, 5, 10, 10, 20, "b"),
                 Char(2, 2, 0, 20, 10, 25, 20, "c"))
        lines = build_lines(0, chars)
        self.assertEqual([row.char_ids for row in lines], [(0, 1, 2)])
        self.assertEqual((lines[0].x0, lines[0].x1), (0, 30))

    def test_nonfinite_or_inverted_boxes_raise_without_text(self):
        for box in [(0, 0, float("nan"), 10), (10, 0, 0, 10),
                    (0, 10, 10, 0), (0, 0, 10, float("inf"))]:
            with self.subTest(box=box), self.assertRaisesRegex(ValueError, "^invalid_char_box$"):
                build_lines(0, [Char(0, 0, 0, *box, "private")])

    def test_content_order_and_sub_order_define_line_order(self):
        chars = [
            Char(2, 1, 1, 15, 10, 20, 20, "b", 3),
            Char(3, 2, 0, 10, 30, 15, 40, "c", 4),
            Char(1, 1, 0, 10, 10, 15, 20, "a", 3),
        ]
        before = list(chars)
        lines = build_lines(7, chars)
        self.assertIsInstance(lines, tuple)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].char_ids, (1, 2))
        self.assertEqual(lines[0].text, "ab")
        self.assertEqual(lines[0].paragraphs, (3,))
        self.assertEqual((lines[0].page, lines[0].index), (7, 0))
        self.assertEqual((lines[0].order_start, lines[0].order_end), (1, 1))
        self.assertEqual((lines[0].x0, lines[0].x1, lines[0].baseline), (10, 20, 20))
        self.assertEqual(lines[1].char_ids, (3,))
        self.assertEqual(chars, before)


if __name__ == "__main__":
    unittest.main()
