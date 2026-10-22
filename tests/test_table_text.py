"""Regression tests for table cell content rules."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


# 0.7.0 表格翻译封存：只验证封存实现的内容规则契约，不依赖生产部署。
HELPER_PATH = (
    Path(__file__).parents[1]
    / "addon" / "content" / "python" / "archive" / "ztf_table_text.py"
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

    def test_body_translates_ordinary_english_and_keeps_identifiers(self) -> None:
        # 用户确认语义：表体只保留纯标识符/数字/URL/代码；普通英文（含人名、
        # 产品名、1-2 词标签）全部送译。
        for value in (
            "Benign",
            "Monday",
            "Harrison",
            "Network Scans",
            "harrison",
            "russellmitchell",
            "Apache Spark",
            "F1 score",
            "Days",
            "slowloris, Slowhttptest, Hulk and GoldenEye",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    table_text.classify_table_cell(value, "body")["action"],
                    "translate",
                )
        for value in (
            "A-Acc-Chr1",
            "T1042",
            "17",
            "YES/NO",
            "https://example.org/x",
            "www.example.org/path",
            "Apache Spark 3.5",
            "",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    table_text.classify_table_cell(value, "body")["action"], "keep"
                )

    def test_long_sentence_with_identifier_token_is_translated(self) -> None:
        for sentence in (
            "New cred_disp parameter combination in Audit logs.",
            "New apparmor parameter combination in Audit logs.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(
                    table_text.classify_table_cell(sentence, "body")["action"],
                    "translate",
                )
        for value in ("Apache Spark 3.5", "A-Acc-Chr1"):
            with self.subTest(value=value):
                self.assertEqual(
                    table_text.classify_table_cell(value, "body")["action"],
                    "keep",
                )

    def test_url_and_code_identifiers_are_recognized_regardless_of_word_count(self) -> None:
        for value in (
            "https://example.org/x",
            "http://example.org/a/b/c/d",
            "www.example.org/a/b/c",
            "user@example.org",
            "cred_disp",
            "A-Acc-Chr1",
        ):
            with self.subTest(value=value):
                self.assertTrue(table_text.is_identifier(value))
        for value in (
            "New cred_disp parameter combination in Audit logs.",
            "The detector identifies new characters in the Apache access log referer field.",
            "See https://example.org/x for the source data.",
            "https://example.org/x is the dataset we used",
            "www.example.org hosts the data",
            "Benign",
        ):
            with self.subTest(value=value):
                self.assertFalse(table_text.is_identifier(value))

    def test_url_identifier_boundaries(self) -> None:
        # 整格就是一个 URL（含端口/查询串/片段/结尾标点）时保留原文。
        for value in (
            "https://example.org:8443/x",
            "https://example.org/x?q=1&y=2",
            "https://example.org/x#fragment",
            "https://example.org/x.",
            "https://example.org/x,",
            "http://example.org",
            "www.example.org",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    table_text.classify_table_cell(value, "body")["action"], "keep"
                )
        # URL 后紧跟其它文字（含无空格的中文正文）不是整格 URL，仍须送译。
        for value in (
            "https://example.org/x这是正文",
            "https://example.org/x，见说明",
            "https://example.org/x is the dataset we used",
            "www.example.org hosts the data",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    table_text.classify_table_cell(value, "body")["action"],
                    "translate",
                )

    def test_meaningful_chinese_accepts_mixed_and_single_cjk_output(self) -> None:
        for source, translated in (
            ("detection score", "检测分数"),
            ("score", "分"),
            ("sO,sA,sW,sR,sL and B)", "sO、sA、sW、sR、sL 和 B）"),
            (
                "PortScans (sS,sT,sF,sX,sN,sP,sV,sU,",
                "端口扫描（sS、sT、sF、sX、sN、sP、sV、sU、",
            ),
            ("Web BForce, XSS and Sql Inject.", "Web 暴力破解、XSS 和 SQL 注入。"),
            ("Init Win F.Bytes", "初始化 Win F.Bytes"),
        ):
            with self.subTest(translated=translated):
                self.assertTrue(table_text.meaningful_chinese(source, translated))

    def test_meaningful_chinese_rejects_echo_noise_and_numbers(self) -> None:
        for source, translated in (
            ("harrison", "harrison"),
            ("harrison", "  harrison  "),
            ("yes", "YES"),
            ("17", "17"),
            ("score", "123"),
            ("score", "，。！"),
            ("score", ""),
        ):
            with self.subTest(source=source, translated=translated):
                self.assertFalse(table_text.meaningful_chinese(source, translated))


if __name__ == "__main__":
    unittest.main()
