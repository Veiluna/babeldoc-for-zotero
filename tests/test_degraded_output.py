"""降级输出与表头专名回退的回归测试。

复现 2026-10-21T09-37-18-320Z 日志：第 8 页 Table 1 的表头作者名
（harrison、russellmitchell、santos、shaw、wardbeck、wheeler、wilson）
被当成“未翻译的英文原文”，补译后仍保留原文，旧策略因此判整篇失败、
丢弃已生成的 PDF。新策略要求：PDF 已生成且至少 1 段真正翻译成功时，
任何段落级/结构性失败降级为警告并导入；无输出或 0 段成功仍失败。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


RUNNER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def decision_with(
    page: int, paragraph: int, reason: str = "模型返回了未翻译的英文原文，已进入合并补译。"
) -> dict:
    return {
        "kind": "translation_failed",
        "failed_paragraphs": 1,
        "failures": [{"page": page, "paragraph": paragraph, "reason": reason}],
        "message": f"第 {page} 页段落 {paragraph}：{reason}",
    }


def quality_failure(page: int, paragraph: int, reason: str) -> dict:
    return {
        "location": f"第 {page} 页第 {paragraph} 段（text）",
        "page": page,
        "paragraph": paragraph,
        "reason": reason,
        "phase": "repair",
        "layout_label": "text",
        "checks": {"placeholder_issues": [{"token": "<style id='1'>"}]},
    }


def request_state(
    translated: int = 595,
    quality: dict | None = None,
    fatal: str = "",
) -> dict:
    return {
        "translated_paragraph_ids": set(range(translated)),
        "quality_failures": quality or {},
        "fatal_error": fatal,
        "job_log_path": "",
    }


class DegradedOutputFailureCollectionTest(unittest.TestCase):
    def test_collects_decision_failures_with_locations(self) -> None:
        state = {"result": "/tmp/mono.pdf", "dual_result": "", "error": ""}
        failures = runner._degraded_output_failures(
            request_state(),
            decision_with(8, 6),
            state,
            0,
            "",
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["page"], failures[0]["paragraph"]), (8, 6))
        self.assertIn("未翻译", failures[0]["reason"])

    def test_deduplicates_quality_and_decision_entries(self) -> None:
        # 同一页/段落同时出现在 decision 与 quality_failures 时只计一次，
        # 保留先收集到的 decision 原因；计数与消息使用同一口径。
        state = {"result": "/tmp/mono.pdf", "dual_result": "", "error": ""}
        quality = {"k": quality_failure(8, 6, "译文中的占位符缺失、重复、被改写或移动了位置。")}
        failures = runner._degraded_output_failures(
            request_state(quality=quality),
            decision_with(8, 6),
            state,
            0,
            "",
        )
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("未翻译", failures[0]["reason"])
        self.assertEqual(runner._degraded_output_failed_count(failures), 1)

    def test_collects_structural_quality_failures(self) -> None:
        state = {"result": "/tmp/mono.pdf", "dual_result": "", "error": ""}
        quality = {"k": quality_failure(3, 2, "译文中的占位符缺失、重复、被改写或移动了位置。")}
        failures = runner._degraded_output_failures(
            request_state(quality=quality),
            None,
            state,
            0,
            "",
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["page"], failures[0]["paragraph"]), (3, 2))

    def test_collects_state_error_and_exit_code(self) -> None:
        state = {"result": "/tmp/mono.pdf", "dual_result": "", "error": "BabelDOC reported an error."}
        failures = runner._degraded_output_failures(
            request_state(), None, state, 1, ""
        )
        # 文档级错误不冒充“段落失败”：不计入 failed_paragraphs。
        self.assertEqual(runner._degraded_output_failed_count(failures), 0)
        self.assertEqual(len(failures), 2, failures)
        self.assertTrue(all(item["page"] is None for item in failures), failures)
        reasons = [item["reason"] for item in failures]
        self.assertTrue(any("退出码" in reason for reason in reasons), failures)

    def test_unbound_failure_is_a_document_error_not_a_paragraph(self) -> None:
        # BabelDOC 丢失线程上下文时 policy 只累计 failed_paragraphs，没有
        # 位置明细；降级门不能被空 failures 骗过。
        decision = {
            "kind": "translation_failed",
            "failed_paragraphs": 2,
            "failures": [],
            "message": "应译段落补译后仍失败。",
        }
        state = {"result": "/tmp/mono.pdf", "dual_result": "", "error": ""}
        failures = runner._degraded_output_failures(
            request_state(), decision, state, 0, "", "mono"
        )
        self.assertTrue(failures)
        self.assertEqual(runner._degraded_output_failed_count(failures), 0)
        self.assertIsNone(failures[0]["page"])

    def test_mode_downgrade_is_a_document_error(self) -> None:
        only_mono = {"result": "/tmp/mono.pdf", "dual_result": "", "error": ""}
        failures = runner._degraded_output_failures(
            request_state(), None, only_mono, 0, "", "dual"
        )
        self.assertTrue(any("降级" in item["reason"] for item in failures), failures)
        only_dual = {"result": "", "dual_result": "/tmp/dual.pdf", "error": ""}
        failures = runner._degraded_output_failures(
            request_state(), None, only_dual, 0, "", "mono"
        )
        self.assertTrue(any("降级" in item["reason"] for item in failures), failures)

    def test_degrade_gate_requires_output_and_a_real_translation(self) -> None:
        self.assertTrue(
            runner._can_degrade_output({"result": "/tmp/mono.pdf"}, 1)
        )
        self.assertTrue(
            runner._can_degrade_output({"result": "", "dual_result": "/tmp/dual.pdf"}, 1)
        )
        self.assertFalse(runner._can_degrade_output({"result": "/tmp/mono.pdf"}, 0))
        self.assertFalse(runner._can_degrade_output({"result": ""}, 5))

    def test_returns_empty_without_any_failure(self) -> None:
        state = {"result": "/tmp/mono.pdf", "dual_result": "", "error": ""}
        failures = runner._degraded_output_failures(
            request_state(), None, state, 0, ""
        )
        self.assertEqual(failures, [])


class DegradedOutputMessageTest(unittest.TestCase):
    def test_message_lists_locations_and_count_summary(self) -> None:
        failures = [
            {"page": 8, "paragraph": paragraph, "reason": "模型返回了未翻译的英文原文。"}
            for paragraph in range(6, 13)
        ]
        message = runner._degraded_output_message(
            failures, 595, "602 段中 595 段成功（7 段保留原文）"
        )
        self.assertIn("7 段", message)
        self.assertIn("第 8 页段落 6", message)
        self.assertIn("602 段中 595 段成功", message)
        self.assertIn("保留原文", message)
        self.assertIn("PDF", message)

    def test_message_without_locations_still_reports_a_failure(self) -> None:
        message = runner._degraded_output_message(
            [{"page": None, "paragraph": None, "reason": "BabelDOC 报告了错误。"}],
            3,
            "",
        )
        self.assertIn("错误", message)
        self.assertIn("PDF", message)
        self.assertNotIn("0 段未能安全翻译", message)


class OutputDiscoveryTest(unittest.TestCase):
    def test_requested_mode_comes_from_the_no_dual_flag(self) -> None:
        self.assertEqual(runner._requested_output_mode(["a", "--no-dual"]), "mono")
        self.assertEqual(runner._requested_output_mode(["a"]), "dual")

    def test_discovers_non_empty_mono_and_dual_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mono = os.path.join(directory, "paper.zh.mono.pdf")
            dual = os.path.join(directory, "paper.zh.dual.pdf")
            empty = os.path.join(directory, "empty.zh.mono.pdf")
            for path in (mono, dual):
                with open(path, "wb") as handle:
                    handle.write(b"x" * 32)
            open(empty, "wb").close()
            found = runner._discover_output_files(directory)
            self.assertEqual(found["mono"], mono)
            self.assertEqual(found["dual"], dual)

    def test_discovery_handles_missing_directory(self) -> None:
        self.assertEqual(
            runner._discover_output_files(""),
            {"mono": "", "dual": ""},
        )
        self.assertEqual(
            runner._discover_output_files("G:/definitely-not-a-directory"),
            {"mono": "", "dual": ""},
        )


class TableHeaderProperNounTest(unittest.TestCase):
    def test_logged_author_names_are_kept_in_table_headers(self) -> None:
        for name in (
            "harrison",
            "russellmitchell",
            "santos",
            "shaw",
            "wardbeck",
            "wheeler",
            "wilson",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    runner._term_like_source(name, "table_header"), name
                )
                result = runner.validate_translation_output(
                    name, name, {"layout_label": "table_header"}, len
                )
                self.assertIs(result["usable"], True, result)
                self.assertIs(result["checks"]["kept_verbatim"], True, result)
                self.assertIs(result["checks"]["identical_english"], False, result)

    def test_capitalized_names_keep_working(self) -> None:
        for name in ("Harrison", "Wheeler", "WILSON"):
            with self.subTest(name=name):
                self.assertTrue(runner._term_like_source(name, "table_header"))

    def test_ordinary_plurals_and_generic_words_are_not_names(self) -> None:
        for word in (
            "observations",
            "measurements",
            "classifications",
            "conditions",
            "parameters",
            "characteristics",
            "requirements",
            "sensor",
            "device",
            "protocol",
            "latency",
            "host",
            "user",
            "event",
            "flow",
            "packet",
            "port",
            "bandwidth",
            "throughput",
            "timestamp",
            "input",
            "output",
            "process",
            "layer",
        ):
            with self.subTest(word=word):
                self.assertFalse(
                    runner._term_like_source(word, "table_header"), word
                )

    def test_long_capitalized_words_must_translate(self) -> None:
        for word in ("Bandwidth", "Throughput", "Timestamp", "Classification"):
            with self.subTest(word=word):
                self.assertFalse(
                    runner._term_like_source(word, "table_header"), word
                )
                result = runner.validate_translation_output(
                    word, word, {"layout_label": "table_header"}, len
                )
                self.assertIs(result["usable"], False, result)

    def test_common_lowercase_table_words_still_translate(self) -> None:
        for word in (
            "detection",
            "average",
            "filtered",
            "prioritization",
            "scenario",
            "detectors",
            "alerts",
            "processing",
            "classification",
            "threshold",
        ):
            with self.subTest(word=word):
                self.assertFalse(
                    runner._term_like_source(word, "table_header"), word
                )
                result = runner.validate_translation_output(
                    word, word, {"layout_label": "table_header"}, len
                )
                self.assertIs(result["usable"], False, result)
                self.assertIs(result["checks"]["identical_english"], True, result)

    def test_blacklisted_header_words_still_translate(self) -> None:
        for word in ("label", "year", "data", "model", "method", "precision"):
            with self.subTest(word=word):
                self.assertFalse(
                    runner._term_like_source(word, "table_header"), word
                )

    def test_proper_noun_fallback_is_table_only(self) -> None:
        # 专名回退只在 table_header / table_body 生效；正文/标题/未知角色
        # 仍走原来的严格判定（generic 短标签启发式不在本任务范围）。
        for label in ("body", "text", "title", ""):
            with self.subTest(label=label):
                self.assertFalse(runner._term_like_source("harrison", label))
        result = runner.validate_translation_output(
            "harrison", "harrison", {"layout_label": "body"}, len
        )
        self.assertIs(result["usable"], False, result)


if __name__ == "__main__":
    unittest.main()
