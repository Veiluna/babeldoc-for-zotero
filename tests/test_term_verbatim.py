"""术语/标识符原样返回的回归测试。

覆盖 2026-10-20T16-59-45-017Z-GRASRNS3 日志中补译后仍被误判的四段
（F1、Adaboost、http、https），普通句子与常见标题词的反例，
table_header 中通用表头必须翻译、术语与标识符仍可保留的规则，
以及全大写变体、正文单 token 收紧和主译/补译调用点 layout_label 的回归。
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


RUNNER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class TermVerbatimTest(unittest.TestCase):
    def validate(
        self, source: str, translated: str | None = None, layout_label: str = "title"
    ):
        return runner.validate_translation_output(
            source,
            source if translated is None else translated,
            {"layout_label": layout_label},
            len,
        )

    def assert_kept(self, source: str, layout_label: str) -> dict:
        self.assertTrue(runner._term_like_source(source, layout_label))
        result = self.validate(source, layout_label=layout_label)
        self.assertIs(result["usable"], True)
        self.assertIs(result["checks"]["kept_verbatim"], True)
        self.assertIs(result["checks"]["identical_english"], False)
        return result

    def assert_must_translate(self, source: str, layout_label: str) -> dict:
        self.assertFalse(runner._term_like_source(source, layout_label))
        result = self.validate(source, layout_label=layout_label)
        self.assertIs(result["usable"], False)
        self.assertIs(result["checks"]["kept_verbatim"], False)
        self.assertIs(result["checks"]["identical_english"], True)
        return result

    def test_user_logged_terms_keep_the_original(self) -> None:
        # F1/http/https are tight identifiers, so any layout label may keep them.
        for term in ("F1", "http", "https"):
            with self.subTest(term=term):
                self.assertTrue(runner._term_like_source(term))
                self.assert_kept(term, "title")
        # Adaboost is a loose single-word term: only table roles keep it.
        self.assertFalse(runner._term_like_source("Adaboost", "title"))
        self.assert_kept("Adaboost", "table_header")

    def test_titles_must_translate(self) -> None:
        # Regression from HEAD: the loose single-word/all-caps fallback let
        # these titles through as kept_verbatim.
        for word in (
            "Security",
            "Overview",
            "Analysis",
            "Method",
            "Result",
            "Evaluation",
            "Performance",
            "Dataset",
            "Model",
            "Data",
            "System",
            "Approach",
            "Framework",
            "Experiments",
            "MACHINE LEARNING",
        ):
            with self.subTest(word=word):
                self.assert_must_translate(word, "title")
                self.assertFalse(runner._term_like_source(word, "heading"))
        result = runner.validate_translation_output(
            "Method", "Method", {"layout_label": "title"}, len
        )
        self.assertIs(result["usable"], False)

    def test_all_caps_variants_do_not_bypass_translation_rules(self) -> None:
        # 全大写快捷曾先于表头黑名单和标题词表执行，YEAR/METHOD 等被当成
        # 缩写保留；规则顺序调整后这些通用表头/标题必须翻译。
        for word in ("YEAR", "TITLE", "METHOD", "TASK", "RANK", "NUMBER"):
            with self.subTest(layout="table_header", word=word):
                self.assert_must_translate(word, "table_header")
        for word in (
            "METHOD",
            "RESULT",
            "YEAR",
            "TITLE",
            "TASK",
            "RANK",
            "NUMBER",
        ):
            with self.subTest(layout="title", word=word):
                self.assert_must_translate(word, "title")
        result = runner.validate_translation_output(
            "METHOD", "METHOD", {"layout_label": "title"}, len
        )
        self.assertIs(result["usable"], False)

    def test_ordinary_sentences_still_fail(self) -> None:
        for sentence in ("of the system", "Our method is accurate."):
            with self.subTest(sentence=sentence):
                self.assertFalse(runner._term_like_source(sentence))
                result = self.validate(sentence, layout_label="text")
                self.assertIs(result["usable"], False)
                self.assertIs(result["checks"]["identical_english"], True)

    def test_common_title_words_are_not_terms(self) -> None:
        for word in (
            "Abstract",
            "Introduction",
            "Conclusion",
            "Keywords",
            "References",
            "Acknowledgements",
            "Appendix",
            "Table",
            "Figure",
            "Section",
            "Results",
            "Discussion",
            "Methods",
            "CONCLUSION",
        ):
            with self.subTest(word=word):
                self.assertFalse(runner._term_like_source(word))
        result = self.validate("Conclusion")
        self.assertIs(result["usable"], False)
        self.assertIs(result["checks"]["identical_english"], True)

    def test_table_header_common_words_must_translate(self) -> None:
        for word in (
            "Label",
            "Labels",
            "Feature",
            "Features",
            "Weight",
            "Network",
            "Traffic",
            "Email",
            "Browser",
            "Other",
            "Method",
            "Precision",
            "Recall",
            "Accuracy",
            "Year",
            "Title",
            "Authors",
            "Number",
            "Task",
            "Rank",
            "NETWORK TRAFFIC",
            "Date",
            "Pages",
            "Volume",
            "Issue",
            "Journal",
            "Conference",
            "Booktitle",
            "Publisher",
            "Address",
            "Edition",
            "Chapter",
            "Section",
            "Figure",
            "Note",
            "Machine Learning",
            "Attack Type",
            "Time",
            "Day",
            "Hour",
            "Percentage",
        ):
            with self.subTest(word=word):
                self.assert_must_translate(word, "table_header")
        self.assertFalse(
            runner._verbatim_echo_allowed("Label", "Label", "table_header")
        )

    def test_marked_table_headers_must_translate(self) -> None:
        # 表头常带显著性/脚注标记（*、†、‡、#）。整串匹配前必须剥离标记，
        # 否则 Method* 等会落入单首字母大写的宽松回退，未翻译却计为保留；
        # ztf_table_headers 会把这些列标题送译。
        for word in ("Method*", "YEAR*", "Label*", "Recall*", "Feature*"):
            with self.subTest(word=word):
                self.assert_must_translate(word, "table_header")
        for word in ("Method†", "Label‡", "Recall#"):
            with self.subTest(word=word):
                self.assertFalse(runner._term_like_source(word, "table_header"))
        # 专名剥离标记后仍是术语，可以在表格角色中保留。
        self.assert_kept("Adaboost*", "table_header")

    def test_numeric_values_are_not_untranslated(self) -> None:
        # 数值、百分比与区间不需要翻译，回显不算失败；小数点不能把
        # "12.7%"、"0.912" 判成句子标点。
        for value in ("12.7%", "0.912", "1,024", "3–5", "±0.3", "0.5"):
            with self.subTest(value=value):
                self.assertIs(runner._term_like_source(value, "body"), True)
                self.assert_kept(value, "body")
        result = runner.validate_translation_output(
            "12.7%", "12.7%", {"layout_label": "body"}, len
        )
        self.assertIs(result["usable"], True)
        self.assertIs(result["checks"]["kept_verbatim"], True)
        self.assertIs(result["checks"]["identical_english"], False)
        # 数字标题同样不需要翻译。
        self.assert_kept("12.7%", "title")
        # 数值规则不得放过普通句子或“数字 + 英文单词”。
        for sentence in ("of the system", "5 models"):
            with self.subTest(sentence=sentence):
                self.assertFalse(runner._term_like_source(sentence, "body"))

    def test_table_header_identifiers_keep_the_original(self) -> None:
        # Tight identifiers/abbreviations win over the header blacklist; only
        # the loose single-word fallback is filtered by it.
        for term in (
            "Adaboost",
            "SECOND",
            "Swin",
            "F1",
            "http",
            "https",
            "AUC",
            "TP",
        ):
            with self.subTest(term=term):
                self.assertTrue(runner._term_like_source(term, "table_header"))
        self.assert_kept("Adaboost", "table_header")
        self.assert_kept("SECOND", "table_header")
        self.assert_kept("Swin", "table_header")
        self.assert_kept("AUC", "table_header")
        self.assert_kept("TP", "table_header")

    def test_tight_identifiers_work_in_body(self) -> None:
        for term in ("http", "https", "F1", "CIC-IDS-2017", "IEEE 754"):
            with self.subTest(term=term):
                self.assertTrue(runner._term_like_source(term, "body"))
                self.assert_kept(term, "body")
        # Loose fallback (single capitalized short word) is not allowed here.
        self.assertFalse(runner._term_like_source("Security", "body"))

    def test_body_single_word_ordinary_english_must_translate(self) -> None:
        # 正文单 token 兜底曾直接返回 True，Security/Adaboost 被当成保留；
        # 现在只允许中文、链接/邮箱或 _term_like_source 认可的术语。
        for word in ("Security", "Adaboost"):
            with self.subTest(word=word):
                self.assertFalse(runner._term_like_source(word, "body"))
                result = self.validate(word, layout_label="body")
                self.assertIs(result["usable"], False)
                self.assertIs(result["checks"]["identical_english"], True)
        result = runner.validate_translation_output(
            "Security", "Security", {"layout_label": "body"}, len
        )
        self.assertIs(result["usable"], False)

    def test_digit_shortcut_requires_no_ordinary_words(self) -> None:
        # “含数字”不能单独作为术语证据，否则 "5 models" 会被原样保留。
        self.assertFalse(runner._term_like_source("5 models", "body"))
        result = self.validate("5 models", layout_label="body")
        self.assertIs(result["usable"], False)
        self.assertIs(result["checks"]["identical_english"], True)

    def test_repair_verbatim_echo_uses_layout_label(self) -> None:
        # The main and repair passes share _verbatim_echo_allowed; a table
        # header must not be kept just because its repair label was dropped.
        self.assertFalse(
            runner._verbatim_echo_allowed("Label", "Label", "table_header")
        )
        self.assertFalse(
            runner._verbatim_echo_allowed("Year", "Year", "table_header")
        )
        # 这些用例捕获主译/补译调用点把 layout_label 传空的回归：没有版式
        # 上下文时，表格宽松回退与未知角色的短标签回退都不得生效。
        self.assertTrue(
            runner._verbatim_echo_allowed("Adaboost", "Adaboost", "table_header")
        )
        self.assertFalse(
            runner._verbatim_echo_allowed("Adaboost", "Adaboost", "")
        )
        self.assertFalse(
            runner._verbatim_echo_allowed("Label", "Label", "")
        )
        self.assertTrue(
            runner._verbatim_echo_allowed("SECOND", "SECOND", "table_header")
        )

    def test_verbatim_echo_call_sites_pass_layout_label(self) -> None:
        # 只测 helper 发现不了 runner 主译/补译调用点把 layout_label 置空；
        # 直接检查两处 _verbatim_echo_allowed 调用的第三个实参。
        source = RUNNER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_verbatim_echo_allowed"
        ]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(len(call.args), 3)
            argument = ast.get_source_segment(source, call.args[2]) or ""
            self.assertIn("layout_label", argument)

    def test_repair_kept_verbatim_does_not_count_as_failure(self) -> None:
        request_state: dict[str, object] = {}
        policy = runner.TranslationPolicy()
        term = SimpleNamespace(layout_label="table_header", unicode="Adaboost")
        policy.record_failure(term, "provisional failure before its output arrived")
        self.assertEqual(policy.failed_paragraphs, 1)

        quality = self.validate("Adaboost", layout_label="table_header")
        self.assertIs(quality["checks"]["kept_verbatim"], True)
        runner._mark_paragraph_kept_verbatim(request_state, policy, term)

        self.assertEqual(policy.failed_paragraphs, 0)
        self.assertIsNone(policy.decision())
        self.assertEqual(request_state["kept_verbatim_paragraph_ids"], {id(term)})

    def test_chinese_output_is_a_translation_not_kept_verbatim(self) -> None:
        self.assertFalse(runner._term_like_source("中文"))
        result = self.validate("Adaboost", "Adaboost 算法")
        self.assertIs(result["usable"], True)
        self.assertIs(result["checks"]["kept_verbatim"], False)

    def test_identifier_shapes(self) -> None:
        for term in (
            "CIC-IDS-2017",
            "IEEE 754",
            "HTTP",
            "URL",
            "IDS",
            "api",
            "PDF",
            "AUC",
            "TP",
        ):
            with self.subTest(term=term):
                self.assertTrue(runner._term_like_source(term))
        for text in (
            "A Novel Method",
            "Precision",
            "F1.",
            "Design, implementation",
            "a" * 41,
        ):
            with self.subTest(text=text):
                self.assertFalse(runner._term_like_source(text))


if __name__ == "__main__":
    unittest.main()
