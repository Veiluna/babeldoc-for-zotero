"""T2 认栏的公开接口合成页与脱文样本回归。"""

from dataclasses import replace
import gzip
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon/content/python"))
from ztf_split_types import Char, Line, MARGIN, SPANNING
from ztf_split_lines import build_lines
from ztf_split_columns import detect_columns


def line(index, x0, x1, y, paragraph=None):
    """构造单行：y 是框底，框高 10 是合成数据定义。"""
    return Line(0, index, (index,), index, index, x0, x1, y - 10, y, y,
                "x", (index if paragraph is None else paragraph,))


def character_runs(runs):
    """每段 24 个宽 10 的相接字符；runs 为 (x, baseline, paragraph)。"""
    chars = []
    for x, y, paragraph in runs:
        for offset in range(24):
            index = len(chars)
            chars.append(Char(index, index, 0, x + offset * 10, y - 10,
                              x + (offset + 1) * 10, y, "x", paragraph))
    return chars


class DetectColumnsTests(unittest.TestCase):
    def test_review_a_positive_gutter_narrower_than_character_uses_gap(self):
        chars = character_runs((x, y, paragraph)
                               for paragraph, (x, y) in enumerate(
                                   ((40, 100), (285, 100), (40, 200), (285, 200),
                                    (40, 300), (285, 300))))
        result = detect_columns(0, 600, 800, build_lines(0, chars), {})
        self.assertEqual((result.columns, result.basis, result.gutters),
                         (2, "gap", ((280, 285),)))
        self.assertEqual(result.line_column, (0, 1, 0, 1, 0, 1))
        self.assertEqual(result.paragraph_column,
                         ((0, 0), (1, 1), (2, 0), (3, 1), (4, 0), (5, 1)))
        self.assertEqual(result.split_paragraphs, ())

    def test_review_b_independent_paragraphs_beside_gutter_are_not_spanning(self):
        chars = character_runs(((40, 100, 0), (40, 700, 1),
                                (285, 100, 2), (285, 700, 3),
                                (40, 750, 4), (285, 750, 5)))
        result = detect_columns(0, 600, 800, build_lines(0, chars), {})
        self.assertEqual((result.columns, result.basis, result.gutters),
                         (2, "jump", ((280, 285),)))
        self.assertEqual(result.paragraph_column,
                         ((0, 0), (1, 0), (2, 1), (3, 1), (4, 0), (5, 1)))
        self.assertEqual(result.line_column, (0, 0, 1, 1, 0, 1))
        self.assertEqual(result.split_paragraphs, ())
        self.assertNotIn("spanning_line", result.reasons)

    def test_same_paragraph_on_both_sides_of_empty_gutter_is_split(self):
        chars = character_runs(((40, 100, 0), (40, 700, 1),
                                (285, 100, 2), (285, 700, 3),
                                (40, 750, 4), (285, 750, 4)))
        result = detect_columns(0, 600, 800, build_lines(0, chars), {})
        self.assertEqual((result.columns, result.basis, result.gutters),
                         (2, "jump", ((280, 285),)))
        self.assertEqual(result.line_column, (0, 0, 1, 1, 0, 1))
        self.assertEqual(result.paragraph_column, ((0, 0), (1, 0), (2, 1), (3, 1)))
        self.assertEqual(result.split_paragraphs, (4,))
        self.assertNotIn("spanning_line", result.reasons)

    def test_character_in_gutter_remains_spanning_despite_other_positive_gaps(self):
        chars = character_runs(((40, 100, 0), (40, 700, 1),
                                (285, 100, 2), (285, 700, 3), (40, 750, 4)))
        for x0, x1 in ((282, 288), (290, 525)):
            index = len(chars)
            chars.append(Char(index, index, 0, x0, 740, x1, 750, "x", 4))
        result = detect_columns(0, 600, 800, build_lines(0, chars), {})
        self.assertEqual((result.columns, result.basis, result.gutters),
                         (2, "jump", ((280, 285),)))
        self.assertEqual(result.line_column, (0, 0, 1, 1, 0, SPANNING, 1))
        self.assertEqual(dict(result.paragraph_column)[4], SPANNING)
        self.assertEqual(result.split_paragraphs, ())
        self.assertIn("spanning_line", result.reasons)

    def test_invalid_coordinates_raise_fixed_error_codes(self):
        valid = line(0, 40, 280, 100)
        for width, height in ((float("nan"), 800), (600, float("inf")), (0, 800), (600, -1)):
            with self.subTest(size=(width, height)), self.assertRaisesRegex(ValueError, "^invalid_page_size$"):
                detect_columns(0, width, height, (valid,), {})
        for bad in (replace(valid, x0=float("nan")), replace(valid, baseline=float("inf")),
                    replace(valid, x1=30), replace(valid, y1=0)):
            with self.subTest(line=bad), self.assertRaisesRegex(ValueError, "^invalid_line_box$"):
                detect_columns(0, 600, 800, (bad,), {})
        for box in ((0, 0, float("inf"), 100), (100, 0, 0, 100)):
            with self.subTest(box=box), self.assertRaisesRegex(ValueError, "^invalid_figure_box$"):
                detect_columns(0, 600, 800, (valid,), {}, (box,))

    def test_half_page_jump_is_strict_and_overlapping_groups_use_gap(self):
        exact_half = (line(0, 40, 280, 600), line(1, 320, 560, 200))
        self.assertEqual(detect_columns(0, 600, 800, exact_half, {}).basis, "single")
        # 先写右侧页眉，再写左栏正文，使内容流组极值相交，必须走备用依据。
        lines = (line(0, 400, 560, 50), line(1, 40, 280, 100),
                 line(2, 40, 280, 200), line(3, 40, 280, 700),
                 line(4, 320, 560, 100), line(5, 320, 560, 200), line(6, 320, 560, 700))
        result = detect_columns(0, 600, 800, lines, {})
        self.assertEqual((result.columns, result.basis, result.gutters), (2, "gap", ((280, 320),)))

    def test_alternating_character_runs_work_without_baseline_tolerance(self):
        chars = []
        for paragraph, (x, y) in enumerate(((40, 100), (320, 100), (40, 200), (320, 200),
                                             (40, 300), (320, 300))):
            for offset in range(24):
                index = len(chars)
                chars.append(Char(index, index, 0, x + offset * 10, y - 10,
                                  x + (offset + 1) * 10, y, "x", paragraph))
        lines = build_lines(0, chars)
        self.assertEqual(len(lines), 6)
        result = detect_columns(0, 600, 800, lines, {})
        self.assertEqual((result.columns, result.basis, result.gutters), (2, "gap", ((280, 320),)))
        # 同比缩放保持归属，排除固定 pt 阈值。
        for factor in (0.001, 1000):
            scaled = tuple(replace(row, x0=row.x0 * factor, x1=row.x1 * factor,
                                   y0=row.y0 * factor, y1=row.y1 * factor,
                                   baseline=row.baseline * factor) for row in lines)
            actual = detect_columns(0, 600 * factor, 800 * factor, scaled, {})
            self.assertEqual(actual.line_column, result.line_column)
            self.assertEqual(actual.gutters, ((280 * factor, 320 * factor),))

    def test_spanning_title_and_paragraph_precedence(self):
        lines = (line(0, 40, 560, 50, 9), line(1, 40, 280, 100, 9),
                 line(2, 40, 280, 700, 1), line(3, 320, 560, 100, 2),
                 line(4, 320, 560, 700, 3))
        result = detect_columns(0, 600, 800, lines, {})
        self.assertEqual(result.columns, 2)
        self.assertEqual(result.line_column, (SPANNING, 0, 0, 1, 1))
        self.assertEqual(dict(result.paragraph_column)[9], SPANNING)
        self.assertEqual(result.split_paragraphs, ())
        self.assertIn("spanning_line", result.reasons)

    def test_mixed_paragraphs_go_to_split_and_mapping_is_unique(self):
        lines = (line(0, 40, 280, 100, 7), line(1, 40, 280, 700, 7),
                 line(2, 320, 560, 100, 7), line(3, 320, 560, 700, 8))
        result = detect_columns(0, 600, 800, lines, {})
        self.assertEqual(result.split_paragraphs, (7,))
        self.assertEqual(result.paragraph_column, ((8, 1),))
        self.assertIn("split_paragraph", result.reasons)

    def test_margin_indices_and_return_positions_are_distinct(self):
        # margins 用 Line.index；返回归属按输入位置，不要求 index 连续。
        lines = (line(90, 40, 560, 50), line(10, 40, 280, 100),
                 line(20, 40, 280, 700), line(30, 320, 560, 100),
                 line(40, 320, 560, 700))
        margins = {90: "tagged"}
        result = detect_columns(0, 600, 800, lines, margins)
        self.assertEqual(result.line_column, (MARGIN, 0, 0, 1, 1))
        self.assertNotIn(90, dict(result.paragraph_column))
        self.assertEqual(margins, {90: "tagged"})
        only_margin = detect_columns(0, 600, 800, lines[:1], margins)
        self.assertEqual((only_margin.columns, only_margin.line_column), (1, (MARGIN,)))

    def test_three_columns_by_jump_and_gap(self):
        by_jump = tuple(line(i, x, x + 120, y)
                        for i, (x, y) in enumerate((
                            (40, 100), (40, 700), (220, 100), (220, 700),
                            (400, 100), (400, 700))))
        by_gap = tuple(line(i, x, x + 120, y)
                       for i, (x, y) in enumerate((
                           (40, 100), (220, 100), (400, 100),
                           (40, 200), (220, 200), (400, 200))))
        for lines, basis, columns in ((by_jump, "jump", (0, 0, 1, 1, 2, 2)),
                                      (by_gap, "gap", (0, 1, 2, 0, 1, 2))):
            with self.subTest(basis=basis):
                result = detect_columns(0, 600, 800, lines, {})
                self.assertEqual((result.columns, result.basis), (3, basis))
                self.assertEqual(result.gutters, ((160, 220), (340, 400)))
                self.assertEqual(result.line_column, columns)

    def test_figure_ranges_only_classify_spanning_elements(self):
        lines = tuple(line(i, x, x + 240, y) for i, (x, y) in enumerate((
            (40, 100), (320, 100), (40, 200), (320, 200), (40, 300), (320, 300))))
        plain = detect_columns(0, 600, 800, lines, {})
        result = detect_columns(0, 600, 800, lines, {}, ((30, 80, 570, 110),))
        self.assertEqual((result.columns, result.basis, result.gutters),
                         (plain.columns, plain.basis, plain.gutters))
        self.assertEqual(result.line_column, (SPANNING, SPANNING, 0, 1, 0, 1))
        self.assertIn("spanning_figure", result.reasons)
        empty = detect_columns(0, 600, 800, (), {}, ((30, 80, 570, 110),))
        self.assertEqual(empty.columns, 1)

    def test_real_rawdict_pages_are_two_columns(self):
        # 从夹具坐标核对的代表性字符 ID、所属段落及左右栏。
        # 不绑定切段后的 Line.index，也不从待测结果推导期望值。
        samples = {
            "chen2026-p10": ((3347, 2, 0), (4624, 7, 1)),
            "chen2026-p18": ((297, 2, 0), (4541, 24, 1)),
            "foba-p01": ((759, 5, 0), (5402, 14, 1)),
            "foba-p17": ((2043, 7, 0), (3403, 11, 1)),
            "foba-p19": ((2786, 15, 0), (5022, 27, 1)),
            "P01-p02": ((1013, 2, 0), (3860, 3, 1)),
            "P01-p03": ((1832, 0, 0), (3889, 4, 1)),
            "P05-p02": ((1921, 2, 0), (7099, 10, 1)),
            "P05-p04": ((1111, 3, 0), (4728, 9, 1)),
        }
        for name, anchors in samples.items():
            with self.subTest(sample=name):
                path = Path(__file__).parent / "fixtures/split080" / f"{name}.json.gz"
                raw = json.loads(gzip.decompress(path.read_bytes()))
                chars = tuple(Char(index, index, 0, *box[:4], "x", box[4])
                              for index, box in enumerate(raw["chars"]))
                lines = build_lines(raw["page"], chars)
                margins = {}
                if name == "foba-p17":
                    # 源页 rawdict 最后 141 字符是 IEEE 下载水印；人工标注，
                    # 不冒充 T3 判定或 BabelDOC IL 实机结果。
                    watermark_start = len(chars) - 141
                    margins = {line.index: "repeated" for line in lines
                               if any(char_id > watermark_start - 1 for char_id in line.char_ids)}
                    self.assertTrue(margins)
                result = detect_columns(raw["page"], raw["width"], raw["height"], lines, margins)
                self.assertEqual(result.columns, 2)
                self.assertEqual(len(result.gutters), 1)
                self.assertGreater(result.gutters[0][1], result.gutters[0][0])
                for char_id, paragraph, column in anchors:
                    self.assertEqual(chars[char_id].paragraph, paragraph)
                    positions = [index for index, row in enumerate(lines)
                                 if char_id in row.char_ids]
                    self.assertEqual(len(positions), 1)
                    self.assertEqual(result.line_column[positions[0]], column)
                    self.assertEqual(dict(result.paragraph_column)[paragraph], column)
                    self.assertNotIn(paragraph, result.split_paragraphs)
                for index, row in enumerate(lines):
                    if row.index in margins:
                        self.assertEqual(result.line_column[index], MARGIN)

    def test_alternating_rows_use_gap_intersection(self):
        lines = (line(0, 40, 275, 100), line(1, 325, 560, 100),
                 line(2, 40, 280, 200), line(3, 320, 560, 200),
                 line(4, 40, 270, 300), line(5, 330, 560, 300))
        result = detect_columns(0, 600, 800, lines, {})
        self.assertEqual((result.columns, result.basis), (2, "gap"))
        self.assertEqual(result.gutters, ((280, 320),))
        self.assertEqual(result.line_column, (0, 1, 0, 1, 0, 1))
        self.assertEqual(result.reasons, ("gap_intersection",))

    def test_no_evidence_is_single_including_empty_page(self):
        for lines in [(), (line(0, 40, 560, 100), line(1, 40, 540, 200))]:
            with self.subTest(lines=lines):
                result = detect_columns(0, 600, 800, lines, {})
                self.assertEqual((result.columns, result.basis, result.gutters), (1, "single", ()))
                self.assertEqual(result.line_column, (0,) * len(lines))
                self.assertEqual(result.reasons, ("single_no_evidence",))

    def test_strict_majority_excludes_one_accidental_aligned_gap(self):
        lines = (line(0, 40, 280, 100), line(1, 320, 560, 100),
                 line(2, 40, 560, 200), line(3, 40, 560, 300))
        self.assertEqual(detect_columns(0, 600, 800, lines, {}).columns, 1)

    def test_jump_two_columns_and_positive_gutter(self):
        lines = (line(0, 40, 280, 100), line(1, 40, 270, 700),
                 line(2, 320, 560, 100), line(3, 330, 560, 700))
        result = detect_columns(0, 600, 800, lines, {})
        self.assertEqual((result.page, result.columns, result.basis), (0, 2, "jump"))
        self.assertEqual(result.gutters, ((280, 320),))
        self.assertEqual(result.line_column, (0, 0, 1, 1))
        self.assertEqual(result.paragraph_column, ((0, 0), (1, 0), (2, 1), (3, 1)))
        self.assertEqual(result.reasons, ("jump",))


if __name__ == "__main__":
    unittest.main()
