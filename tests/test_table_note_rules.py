"""回归：表格内容与表注保留原文（0.7.0 方案 A 收敛）。

表注（注号/Source:/Note:/table_footnote）不再合并/翻译，直接保留原文；
表内单元格（含 */†/‡ 开头）同样保留；普通表外正文照常翻译。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ztf_babeldoc_runner_note_rules",
    ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _box(x: float, y: float, x2: float, y2: float) -> NS:
    return NS(x=x, y=y, x2=x2, y2=y2)


def _table(box: NS) -> NS:
    return NS(class_name="table", box=box)


def _paragraph(text: str, box: NS, label: str = "fallback_line") -> NS:
    return NS(unicode=text, box=box, layout_label=label)


def _page(table_box: NS, paragraphs: list[NS]) -> NS:
    return NS(page_layout=[_table(table_box)], pdf_paragraph=list(paragraphs))


class TableNotePreserveRulesTest(unittest.TestCase):
    def test_marker_cells_inside_table_are_preserved(self) -> None:
        marker = _paragraph("* p < 0.05", _box(10, 10, 40, 20))
        value = _paragraph("0.032", _box(45, 10, 70, 20))
        page = _page(_box(0, 0, 100, 100), [marker, value])
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertIn(id(marker), content_ids)
        self.assertIn(id(value), content_ids)

    def test_indices_and_long_marker_cells_inside_table_are_preserved(self) -> None:
        for text in ("† p < 0.01", "‡ p < 0.001", "* Values accurate at time of", "# Overlap"):
            with self.subTest(text=text):
                cell = _paragraph(text, _box(10, 30, 90, 40))
                page = _page(_box(0, 0, 100, 100), [cell])
                content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
                self.assertIn(id(cell), content_ids)

    def test_boundary_cell_with_79_percent_overlap_is_preserved(self) -> None:
        cell = _paragraph("* p < 0.05", _box(0, 97.6, 100, 109))
        page = _page(_box(0, 100, 100, 200), [cell])
        self.assertTrue(runner._paragraph_inside_table_frame(cell, page))
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertIn(id(cell), content_ids)

    def test_outside_star_note_is_preserved_not_translated(self) -> None:
        note = _paragraph(
            "* Values accurate at time of publication.",
            _box(0, 50, 80, 58),
        )
        page = _page(_box(0, 100, 100, 200), [note])
        self.assertFalse(runner._paragraph_inside_table_frame(note, page))
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertIn(id(note), content_ids)

    def test_outside_source_note_is_preserved_not_translated(self) -> None:
        for text in ("Source: Open data portal", "Note: values are normalized"):
            with self.subTest(text=text):
                note = _paragraph(text, _box(0, 50, 80, 58))
                page = _page(_box(0, 100, 200, 200), [note])
                content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
                self.assertIn(id(note), content_ids)

    def test_table_footnote_label_is_preserved(self) -> None:
        note = _paragraph(
            "Reference label counts from the published dataset.",
            _box(0, 50, 90, 58),
            label="table_footnote",
        )
        page = _page(_box(0, 100, 100, 200), [note])
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertIn(id(note), content_ids)

    def test_note_touching_table_bottom_is_preserved(self) -> None:
        note = _paragraph(
            "* Values accurate at time of publication.", _box(0, 93.0, 100, 111.4)
        )
        page = _page(_box(0, 100, 100, 200), [note])
        self.assertFalse(runner._paragraph_inside_table_frame(note, page))
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertIn(id(note), content_ids)

    def test_plain_outside_text_is_not_preserved(self) -> None:
        body = _paragraph("The table above summarizes the results.", _box(0, 50, 80, 58), label="text")
        page = _page(_box(0, 100, 100, 200), [body])
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertNotIn(id(body), content_ids)

    def test_table_note_fragments_are_not_merged(self) -> None:
        first = _paragraph("* Values accurate at time of", _box(0, 50, 60, 58))
        second = _paragraph("publication.", _box(62, 50, 90, 58))
        page = _page(_box(0, 100, 200, 200), [first, second])
        document = NS(page=[page])
        content_ids = runner.collect_table_content_paragraph_ids(document)
        self.assertIn(id(first), content_ids)
        self.assertIn(id(second), content_ids)
        # 方案 A 不合并表注碎片，BabelDOC 保留原 glyph。
        self.assertEqual(len(page.pdf_paragraph), 2)


    def test_source_note_words_without_colon_or_near_table_are_not_preserved(self) -> None:
        # 普通正文以 Note/Source 开头但没有冒号 → 不保留（照常翻译）。
        for text in ("Note that the results are significant.", "Source data were collected."):
            with self.subTest(text=text):
                body = _paragraph(text, _box(0, 50, 120, 58))
                page = _page(_box(0, 100, 100, 200), [body])
                content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
                self.assertNotIn(id(body), content_ids)
        # 带冒号但页面没有表格布局 → 不保留。
        body = _paragraph("Source: data were collected.", _box(0, 50, 120, 58))
        page = NS(page_layout=[], pdf_paragraph=[body])
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertNotIn(id(body), content_ids)

    def test_source_note_far_from_table_is_not_preserved(self) -> None:
        body = _paragraph("Source: data were collected.", _box(0, 320, 120, 328))
        page = _page(_box(0, 100, 200, 200), [body])
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertNotIn(id(body), content_ids)

    def test_paragraph_below_note_with_large_gap_is_not_preserved(self) -> None:
        note = _paragraph("* Values accurate at time of publication.", _box(0, 50, 80, 58))
        # 表注下方 16pt 的同列 fallback_line 正文：超过连续行 gap，不得并入 preserved。
        body = _paragraph("This paragraph continues the discussion.", _box(0, 74, 80, 82))
        page = _page(_box(0, 100, 200, 200), [note, body])
        content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
        self.assertIn(id(note), content_ids)
        self.assertNotIn(id(body), content_ids)

    def test_known_limits_hash_prefix_and_short_marker_note(self) -> None:
        # 已知限制（方案 A）：井号开头不在注号集合、少于 8 字符的注号表注
        # （如 "* p<.05"）也不识别为表注，仍走普通翻译。
        for text in ("# note about the table", "* p<.05"):
            with self.subTest(text=text):
                note = _paragraph(text, _box(0, 50, 80, 58))
                page = _page(_box(0, 100, 200, 200), [note])
                content_ids = runner.collect_table_content_paragraph_ids(NS(page=[page]))
                self.assertNotIn(id(note), content_ids)


if __name__ == "__main__":
    unittest.main()
