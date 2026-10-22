"""封存回归：表体内容规则契约（0.7.0 起不参与生产翻译）。

0.7.0 表格内容封存后，runner 不再调用这些规则；本测试只验证 archive/
ztf_table_text.py 的封存实现契约，避免将来误改封存代码。
使用 unittest.TestCase 以便 python -m unittest discover 能发现。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "addon" / "content" / "python" / "archive" / "ztf_table_text.py"
SPEC = importlib.util.spec_from_file_location("ztf_table_text_archived", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def _action(text: str, role: str = "body") -> str:
    return helper.classify_table_cell(text, role)["action"]


class SealedTableRoleRulesTest(unittest.TestCase):
    def test_identifiers_stay_verbatim_and_ordinary_words_are_sent(self) -> None:
        # 封存契约：表体只保留纯标识符/数字/URL/代码；普通英文词（含人名、
        # 产品名、1-2 词标签）进入送译路径。
        for text in ("17", "YES/NO", "A-Acc-Chr1"):
            with self.subTest(text=text):
                self.assertEqual(_action(text), "keep")
        for text in (
            "harrison",
            "russellmitchell",
            "Apache Spark",
            "Benign",
            "Monday",
            "Network Scans",
        ):
            with self.subTest(text=text):
                self.assertEqual(_action(text), "translate")

    def test_full_sentence_descriptions_are_translated(self) -> None:
        text = "Unusual occurrence frequencies of Apache Access request methods."
        self.assertEqual(_action(text), "translate")


if __name__ == "__main__":
    unittest.main()
