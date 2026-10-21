"""Regression tests for restoring figure labels and row-label alignment."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import pymupdf


HELPER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_figure_text.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_figure_text", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
figure_text = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(figure_text)


class FigureTextRestoreTest(unittest.TestCase):
    def test_restores_labels_and_realigns_chinese_row_labels(self) -> None:
        source = pymupdf.open()
        output = pymupdf.open()
        try:
            source_page = source.new_page(width=300, height=400)
            for index in range(8):
                source_page.insert_text(
                    (40 + index * 16, 110 + index * 6),
                    "A-Acc-%d" % (index + 1),
                    fontname="helv",
                    fontsize=6,
                )
            source_page.insert_text(
                (170, 160), "RightLabel", fontname="helv", fontsize=6
            )
            output_page = output.new_page(width=600, height=400)
            output_page.insert_text(
                (430, 160), "网络扫描", fontname="china-s", fontsize=8
            )
            region = pymupdf.Rect(30, 90, 230, 200)
            stats = figure_text._restore_region(
                source_page, output_page, region, 300.0
            )
            self.assertGreaterEqual(stats["restored_labels"], 8)
            right = pymupdf.Rect(300, 80, 600, 220)
            self.assertTrue(
                output_page.search_for("A-Acc-1", clip=right),
                "dropped axis labels must be restored on the translated side",
            )
            chinese = [
                word for word in output_page.get_text("words")
                if "网络扫描" in word[4]
            ]
            self.assertTrue(chinese)
            self.assertGreaterEqual(
                chinese[0][2], 480,
                "Chinese row label must be anchored to the source right edge",
            )
            self.assertGreaterEqual(stats["aligned_labels"], 1)
        finally:
            output.close()
            source.close()


if __name__ == "__main__":
    unittest.main()
