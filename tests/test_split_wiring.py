"""runner 中分栏双语接线（#40 S1）的回归测试：开关、输出路径、失败回退与成功产物。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner_split", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


@dataclass(frozen=True)
class _Plan:
    source_index: int
    columns: int
    regions: tuple = ()


def _fake_helpers(compose):
    plan = types.SimpleNamespace(
        plan_page=lambda page: _Plan(page["index"], 2 if page.get("paragraphs") else 0)
    )
    composer = types.SimpleNamespace(compose_split_document=compose)
    return {"ztf_split_plan": plan, "ztf_split_compose": composer}


class SplitWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mono = os.path.join(self.tmp.name, "input.no_watermark.zh.mono.pdf")
        Path(self.mono).write_bytes(b"%PDF-1.7 mono")
        self.source = os.path.join(self.tmp.name, "input.pdf")
        source = pymupdf.open()
        try:
            source.new_page(width=612, height=792)
            source.new_page(width=612, height=792)
            source.save(self.source)
        finally:
            source.close()
        self.state = {"result": self.mono}
        paragraph = {"box": [60, 100, 290, 160], "output_box": [60, 100, 290, 170],
                     "label": "text", "translated": True, "marginal": False,
                     "author": False}
        self.request_state = {
            "split_layout": {"version": 1, "pages": [
                {"index": 0, "width": 612, "height": 792, "il_top": 792,
                 "rotation": 0, "paragraphs": [paragraph], "layouts": []},
                {"index": 1, "width": 612, "height": 792, "il_top": 792,
                 "rotation": 0, "paragraphs": [], "layouts": []},
            ]},
        }
        argv = ["runner", "--files", self.source]
        patcher = mock.patch.object(sys, "argv", argv)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        log_patch = mock.patch.object(
            runner, "_write_job_log", lambda state, value, key="": self.logs.append(value)
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _with_env(self, value):
        return mock.patch.dict(os.environ, {runner.SPLIT_DUAL_ENV: value})

    def test_switch_reads_environment(self):
        with self._with_env("1"):
            self.assertTrue(runner._split_dual_requested())
        with self._with_env("0"):
            self.assertFalse(runner._split_dual_requested())
        with self._with_env("bogus"):
            self.assertFalse(runner._split_dual_requested())
            self.assertEqual(runner._split_dual_setting(), (False, False))

    def test_invalid_setting_is_logged(self):
        with self._with_env("bogus"):
            result = runner._dual_after_layout(self.state, self.request_state, {}, "")
        self.assertIsNone(result)
        self.assertEqual(
            self.logs[-1], {"event": "split_dual", "skipped": "invalid_setting", "count": 1}
        )

    def test_output_path_replaces_mono_suffix(self):
        self.assertEqual(
            runner._split_dual_output_path("C:\\w\\paper.no_watermark.zh.mono.pdf"),
            "C:\\w\\paper.no_watermark.zh.dual.pdf",
        )

    def test_not_requested_keeps_legacy_path(self):
        with self._with_env("0"), mock.patch.object(
            runner, "_restore_dual_links_after_layout", return_value={"legacy": True}
        ) as legacy:
            result = runner._dual_after_layout(self.state, self.request_state, {}, "")
        self.assertEqual(result, {"legacy": True})
        legacy.assert_called_once()
        self.assertNotIn("dual_result", self.state)

    def test_success_sets_dual_result_and_writes_layout(self):
        def compose(source, mono, plans, output):
            self.assertEqual((source, mono), (self.source, self.mono))
            self.assertEqual([plan.source_index for plan in plans], [0, 1])
            Path(output).write_bytes(b"%PDF-1.7 split")
            return {"output_pages": 3}

        with self._with_env("1"), mock.patch.dict(
            runner._HELPER_MODULES, _fake_helpers(compose)
        ):
            result = runner._dual_after_layout(self.state, self.request_state, {}, "")
        expected = self.mono.replace(".zh.mono.pdf", ".zh.dual.pdf")
        self.assertEqual(self.state["dual_result"], expected)
        self.assertEqual(result["output_pages"], 3)
        self.assertEqual(result["fallback_pages"], 1)
        with open(self.mono + ".layout.json", encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(len(written["pages"]), 2)
        # 默认几何（MediaBox 原点为 0、无 CropBox）下换算是恒等的。
        self.assertEqual(written["pages"][0]["paragraphs"][0]["box"], [60.0, 100.0, 290.0, 160.0])
        events = [entry["event"] for entry in self.logs]
        self.assertIn("split_layout_geometry", events)
        self.assertEqual(self.logs[-1]["event"], "split_dual")

    def test_compose_error_falls_back_without_dual(self):
        def compose(source, mono, plans, output):
            Path(output).write_bytes(b"partial")
            raise RuntimeError("boom")

        with self._with_env("1"), mock.patch.dict(
            runner._HELPER_MODULES, _fake_helpers(compose)
        ):
            result = runner._dual_after_layout(self.state, self.request_state, {}, "")
        self.assertIn("error", result)
        self.assertNotIn("dual_result", self.state)
        self.assertFalse(os.path.exists(self.mono.replace(".zh.mono.pdf", ".zh.dual.pdf")))
        self.assertEqual(self.logs[-1]["event"], "split_dual_error")
        self.assertEqual(self.logs[-1]["pages"], 2)

    def test_cleanup_failure_is_logged(self):
        def compose(source, mono, plans, output):
            Path(output).write_bytes(b"partial")
            raise RuntimeError("boom")

        with self._with_env("1"), mock.patch.dict(
            runner._HELPER_MODULES, _fake_helpers(compose)
        ), mock.patch.object(runner.os, "remove", side_effect=PermissionError("locked")):
            runner._dual_after_layout(self.state, self.request_state, {}, "")
        self.assertEqual(self.logs[-1]["event"], "split_dual_cleanup_error")
        self.assertEqual(self.logs[-1]["count"], 1)
        os.remove(self.mono.replace(".zh.mono.pdf", ".zh.dual.pdf"))

    def test_missing_layout_is_logged(self):
        self.request_state.pop("split_layout")
        with self._with_env("1"):
            result = runner._dual_after_layout(self.state, self.request_state, {}, "")
        self.assertEqual(result, {"error": "missing_layout"})
        self.assertNotIn("dual_result", self.state)
        self.assertEqual(
            self.logs[-1], {"event": "split_dual", "skipped": "missing_layout", "pages": 0}
        )

    def test_export_skips_are_logged(self):
        self.request_state["split_layout_skips"] = runner.Counter(missing_snapshot=2)
        with self._with_env("1"), mock.patch.dict(
            runner._HELPER_MODULES, _fake_helpers(lambda *args: {})
        ):
            runner._dual_after_layout(self.state, self.request_state, {}, "")
        self.assertIn(
            {"event": "split_layout_export_skips", "missing_snapshot": 2}, self.logs
        )

    def test_geometry_follows_original_cropbox_and_rotation(self):
        # BabelDOC 解析时 MediaBox 归零、CropBox 清除；IL 框是原用户空间坐标。
        source = pymupdf.open()
        try:
            source.new_page(width=600, height=800)
            source.new_page(width=600, height=800)
            source[0].set_cropbox(pymupdf.Rect(50, 100, 550, 700))
            source[1].set_rotation(90)
            source.save(self.source)
        finally:
            source.close()
        layout = {"version": 1, "pages": [
            # 用户空间文字 y=600..650 → IL 左上坐标（il_top=800）y=150..200
            {"index": 0, "width": 600, "height": 800, "il_top": 800, "rotation": 0,
             "paragraphs": [{"box": [60, 150, 300, 200], "output_box": [1, 2, 3],
                             "label": "text"}],
             "layouts": [{"box": [60, 150, 300, float("nan")], "class": "text"}]},
            {"index": 1, "width": 600, "height": 800, "il_top": 800, "rotation": 0,
             "paragraphs": [], "layouts": []},
        ]}
        normalized, counts = runner._normalize_split_layout_geometry(layout, self.source)
        first, second = normalized["pages"]
        self.assertEqual((first["width"], first["height"]), (500.0, 600.0))
        self.assertEqual(first["paragraphs"][0]["box"], [10.0, 50.0, 250.0, 100.0])
        self.assertEqual(first["layouts"], [])
        self.assertEqual(second["rotation"], 90)
        self.assertEqual(counts["pages_normalized"], 2)
        self.assertEqual(counts["rotated_pages"], 1)
        self.assertEqual(counts["nonfinite_boxes"], 1)
        self.assertEqual(counts["dropped_layouts"], 1)
        self.assertIsNone(first["paragraphs"][0]["output_box"])
        self.assertEqual(counts["malformed_boxes"], 1)
        self.assertEqual(counts["dropped_output_boxes"], 1)

    def test_layout_pages_use_source_boxes(self):
        class Box:
            def __init__(self, x, y, x2, y2):
                self.x, self.y, self.x2, self.y2 = x, y, x2, y2

        page = types.SimpleNamespace(
            cropbox=types.SimpleNamespace(box=Box(0, 0, 612, 792)),
            mediabox=types.SimpleNamespace(box=Box(0, 0, 612, 792)),
            pdf_paragraph=[],
            page_layout=[types.SimpleNamespace(box=Box(50, 600, 300, 700), class_name="figure")],
        )
        paragraph = types.SimpleNamespace(
            box=Box(60, 500, 290, 560), layout_label="text", xobj_id=None
        )
        page.pdf_paragraph.append(paragraph)
        document = types.SimpleNamespace(page=[page])
        snapshots = {id(paragraph): {"source_box": [55.0, 100.0, 295.0, 160.0]}}
        skips = runner.Counter()
        pages = runner._split_layout_pages(
            document, snapshots, set(), page_offset=4, skips=skips
        )
        self.assertEqual(pages[0]["index"], 4)
        self.assertEqual((pages[0]["il_top"], pages[0]["rotation"]), (792.0, 0))
        self.assertFalse(skips)
        self.assertEqual((pages[0]["width"], pages[0]["height"]), (612.0, 792.0))
        self.assertEqual(pages[0]["paragraphs"][0]["box"], [55.0, 100.0, 295.0, 160.0])
        self.assertEqual(pages[0]["paragraphs"][0]["output_box"], [60.0, 232.0, 290.0, 292.0])
        self.assertTrue(pages[0]["paragraphs"][0]["translated"])
        self.assertEqual(pages[0]["layouts"], [{"box": [50.0, 92.0, 300.0, 192.0], "class": "figure"}])
        self.assertFalse(pages[0]["paragraphs"][0]["marginal"])
        self.assertFalse(pages[0]["paragraphs"][0]["author"])
        preserved = runner._split_layout_pages(document, snapshots, {id(paragraph)})
        self.assertFalse(preserved[0]["paragraphs"][0]["translated"])
        # 作者块这类保留段落不是页眉页脚；只有重复边缘行或戳记才标 marginal。
        self.assertFalse(preserved[0]["paragraphs"][0]["marginal"])
        marginal = runner._split_layout_pages(
            document, snapshots, {id(paragraph)}, marginal_ids={id(paragraph)}
        )
        self.assertTrue(marginal[0]["paragraphs"][0]["marginal"])
        paragraph.unicode = "Max Landauer max.landauer@ait.ac.at Austrian Institute of Technology"
        author = runner._split_layout_pages(document, snapshots, {id(paragraph)})
        self.assertTrue(author[0]["paragraphs"][0]["author"])
        later = runner._split_layout_pages(document, snapshots, {id(paragraph)}, page_offset=1)
        self.assertFalse(later[0]["paragraphs"][0]["author"], "只有文档首页的作者块")

    def test_layout_export_counts_skipped_elements(self):
        class Box:
            def __init__(self, x, y, x2, y2):
                self.x, self.y, self.x2, self.y2 = x, y, x2, y2

        page = types.SimpleNamespace(
            cropbox=types.SimpleNamespace(box=Box(0, 0, float("inf"), 792)),
            mediabox=types.SimpleNamespace(box=Box(0, 0, 612, 792)),
            pdf_paragraph=[types.SimpleNamespace(box=Box(1, 1, 2, 2), layout_label="text")],
            page_layout=[types.SimpleNamespace(box=Box(5, 5, 1, 1), class_name="figure")],
        )
        nan_paragraph = types.SimpleNamespace(
            box=Box(1, 1, float("nan"), 2), layout_label="text"
        )
        page.pdf_paragraph.append(nan_paragraph)
        snapshots = {id(nan_paragraph): {"source_box": [1.0, 1.0, 5.0, 5.0]}}
        skips = runner.Counter()
        pages = runner._split_layout_pages(
            types.SimpleNamespace(page=[page]), snapshots, set(), skips=skips
        )
        self.assertEqual((pages[0]["width"], pages[0]["height"]), (0.0, 0.0))
        self.assertIsNone(pages[0]["paragraphs"][0]["output_box"])
        self.assertEqual(
            dict(skips),
            {"invalid_page_geometry": 1, "missing_snapshot": 1,
             "invalid_layout_box": 1, "invalid_output_box": 1},
        )


if __name__ == "__main__":
    unittest.main()
