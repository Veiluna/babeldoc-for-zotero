"""Regression tests for which table-body cells enter translation."""

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


class _Paragraph:
    def __init__(self, text: str) -> None:
        self.unicode = text


class TableBodyRuleTest(unittest.TestCase):
    def test_short_values_and_names_stay_verbatim(self) -> None:
        for text in ("harrison", "russellmitchell", "Apache Spark", "17", "YES/NO"):
            with self.subTest(text=text):
                self.assertFalse(runner._table_body_should_translate(_Paragraph(text)))

    def test_full_sentence_descriptions_are_translated(self) -> None:
        text = (
            "Unusual occurrence frequencies of Apache Access request methods."
        )
        self.assertTrue(runner._table_body_should_translate(_Paragraph(text)))


if __name__ == "__main__":
    unittest.main()
