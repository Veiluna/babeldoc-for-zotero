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
    def test_identifiers_stay_verbatim_and_ordinary_words_are_sent(self) -> None:
        # 用户确认语义：表体只保留纯标识符/数字/URL/代码；普通英文词（含人名、
        # 产品名、1-2 词标签）进入送译路径，最终是否显示中文由模型输出决定。
        for text in ("17", "YES/NO", "A-Acc-Chr1"):
            with self.subTest(text=text):
                self.assertFalse(runner._table_body_should_translate(_Paragraph(text)))
        for text in (
            "harrison",
            "russellmitchell",
            "Apache Spark",
            "Benign",
            "Monday",
            "Network Scans",
        ):
            with self.subTest(text=text):
                self.assertTrue(runner._table_body_should_translate(_Paragraph(text)))

    def test_full_sentence_descriptions_are_translated(self) -> None:
        text = (
            "Unusual occurrence frequencies of Apache Access request methods."
        )
        self.assertTrue(runner._table_body_should_translate(_Paragraph(text)))


if __name__ == "__main__":
    unittest.main()
