"""Regression tests for removing duplicate punctuation glyphs from output PDFs."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import pymupdf


HELPER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python"
    / "ztf_punctuation_cleanup.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_punctuation_cleanup", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class RemovalIndexTest(unittest.TestCase):
    def test_removes_comma_before_chinese_punctuation(self) -> None:
        text = "正如综述所指出的[4, 7, 12, 18], ，阻碍研究。"
        removals = cleanup._removal_indices(text)
        self.assertEqual(len(removals), 1)
        self.assertEqual(text[removals[0]], ",")
        # The comma inside the citation cluster must stay untouched.
        self.assertNotIn(text.index("4, 7"), removals)

    def test_collapses_duplicate_comma_runs_keeping_chinese(self) -> None:
        self.assertEqual(
            cleanup._removal_indices("结果是，, 好的"), [4]
        )
        self.assertEqual(
            cleanup._removal_indices("结果是,，好的"), [3]
        )
        self.assertEqual(
            cleanup._removal_indices("数值 12018,,"), [9]
        )

    def test_collapses_repeated_chinese_punctuation(self) -> None:
        self.assertEqual(cleanup._removal_indices("结束。。下一句"), [3])
        self.assertEqual(cleanup._removal_indices("结束，，下一句"), [3])

    def test_keeps_normal_punctuation(self) -> None:
        for text in (
            "参见第 3.1 节，见附录 B。",
            "区间 [0, 1] 与 [4, 7, 18]，还有 A, B 两个。",
            "公式 (1) 与公式 (2)。",
        ):
            with self.subTest(text=text):
                self.assertEqual(cleanup._removal_indices(text), [])


class PdfCleanupTest(unittest.TestCase):
    def test_removes_duplicate_comma_glyph_from_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            output = root / "output.pdf"
            source_doc = pymupdf.open()
            try:
                source_doc.new_page(width=300, height=200).insert_text(
                    (20, 50), "source", fontname="helv", fontsize=9
                )
                source_doc.save(str(source))
            finally:
                source_doc.close()
            output_doc = pymupdf.open()
            try:
                page = output_doc.new_page(width=600, height=200)
                page.insert_text(
                    (320, 50), "正如[4, 7, 12, 18], ，阻碍研究。",
                    fontname="china-s", fontsize=9,
                )
                page.insert_text(
                    (320, 100), "数值 12018,, 结束。",
                    fontname="china-s", fontsize=9,
                )
                output_doc.save(str(output))
            finally:
                output_doc.close()

            stats = cleanup.clean_duplicate_punctuation(
                str(output), str(source)
            )
            self.assertGreaterEqual(stats["removed"], 2)
            document = pymupdf.open(str(output))
            try:
                text = document[0].get_text("text")
            finally:
                document.close()
            compact = "".join(text.split())
            self.assertNotIn(",，", compact)
            self.assertNotIn(",,", compact)
            self.assertIn("[4,7,12,18]，阻碍研究", compact)
            self.assertIn("12018,", compact)

    def test_monolingual_output_is_cleaned_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "mono.pdf"
            document = pymupdf.open()
            try:
                page = document.new_page(width=300, height=200)
                page.insert_text(
                    (20, 50), "区间 [0, 1], ；结束。",
                    fontname="china-s", fontsize=9,
                )
                document.save(str(output))
            finally:
                document.close()
            stats = cleanup.clean_duplicate_punctuation(str(output))
            self.assertGreaterEqual(stats["removed"], 1)
            document = pymupdf.open(str(output))
            try:
                text = document[0].get_text("text")
            finally:
                document.close()
            compact = "".join(text.split())
            self.assertNotIn(",；", compact)
            self.assertIn("[0,1]；结束", compact)


if __name__ == "__main__":
    unittest.main()
