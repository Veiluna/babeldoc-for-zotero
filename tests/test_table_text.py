"""Regression tests for table cell content rules."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


HELPER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_table_text.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_table_text", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
table_text = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(table_text)


class TableCellContentTest(unittest.TestCase):
    def test_headers_translate_ordinary_words_but_keep_identifiers(self) -> None:
        self.assertEqual(
            table_text.classify_table_cell("Network Scans", "header")["action"],
            "translate",
        )
        for value in ("YES/NO", "A-Acc-Chr1", "T1042", "Apache Spark 3.5", ""):
            with self.subTest(value=value):
                self.assertEqual(
                    table_text.classify_table_cell(value, "header")["action"],
                    "keep",
                )

    def test_body_keeps_short_values_and_translates_sentences(self) -> None:
        for value in ("harrison", "russellmitchell", "Apache Spark", "F1 score", "17"):
            with self.subTest(value=value):
                self.assertEqual(
                    table_text.classify_table_cell(value, "body")["action"], "keep"
                )
        sentence = (
            "The detector identifies new characters in the Apache access log "
            "referer field."
        )
        self.assertEqual(
            table_text.classify_table_cell(sentence, "body")["action"], "translate"
        )

    def test_meaningful_chinese_rejects_echo_noise_and_numbers(self) -> None:
        self.assertTrue(
            table_text.meaningful_chinese(
                "detection score", "检测分数"
            )
        )
        self.assertFalse(table_text.meaningful_chinese("harrison", "harrison"))
        self.assertFalse(table_text.meaningful_chinese("yes", "YES"))
        self.assertFalse(table_text.meaningful_chinese("17", "17"))
        self.assertFalse(table_text.meaningful_chinese("score", "123"))


if __name__ == "__main__":
    unittest.main()
