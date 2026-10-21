"""Punctuation cleanup regressions for translated PDF text."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


RUNNER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class TranslatedPunctuationTest(unittest.TestCase):
    def test_ascii_comma_before_chinese_punctuation_is_removed(self) -> None:
        self.assertEqual(
            runner._normalize_translated_punctuation(
                "取值范围为[0, 1], ；值越接近1越好"
            ),
            "取值范围为[0, 1]；值越接近1越好",
        )
        self.assertEqual(
            runner._normalize_translated_punctuation("区间[0, 1], ，数值越高"),
            "区间[0, 1]，数值越高",
        )

    def test_decimals_citations_and_english_lists_keep_commas(self) -> None:
        for text in ("0.01 alerts", "[4, 7, 18]", "one, two, three", "1, 2, 3"):
            with self.subTest(text=text):
                self.assertEqual(runner._normalize_translated_punctuation(text), text)

    def test_duplicate_chinese_punctuation_is_collapsed(self) -> None:
        self.assertEqual(
            runner._normalize_translated_punctuation("区间[0, 1]，，其中"),
            "区间[0, 1]，其中",
        )
        self.assertEqual(
            runner._normalize_translated_punctuation("结论；；下一步"),
            "结论；下一步",
        )

    def test_non_string_values_pass_through(self) -> None:
        self.assertIsNone(runner._normalize_translated_punctuation(None))
        self.assertEqual(runner._normalize_translated_punctuation(""), "")


if __name__ == "__main__":
    unittest.main()
