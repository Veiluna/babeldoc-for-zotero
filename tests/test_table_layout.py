"""封存回归：表格单元格字号 patch 契约（0.7.0 起不参与生产翻译）。

0.7.0 表格内容封存后，runner 不再加载/调用 ztf_table_layout；本测试只验证
archive/ztf_table_layout.py 的封存实现契约（阶梯、原边框、溢出、回退文案），
不启动 runner、不依赖生产接线。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "addon" / "content" / "python" / "archive" / "ztf_table_layout.py"
SPEC = importlib.util.spec_from_file_location("ztf_table_layout_archived", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
table_layout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(table_layout)


def _box(x: float, y: float, x2: float, y2: float) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y, x2=x2, y2=y2)


def _page() -> SimpleNamespace:
    return SimpleNamespace(
        pdf_curve=[],
        page_layout=[],
        pdf_paragraph=[],
        pdf_character=[],
        pdf_figure=[],
    )


class _FakeParagraph:
    def __init__(self, box: SimpleNamespace, layout_label: str = "table_cell") -> None:
        self.box = box
        self.layout_label = layout_label
        self.scale = None
        self.pdf_paragraph_composition = []


class _FakeInstance:
    """按“scale <= fit_scale”模拟放得下，并记录每次布局请求的 box。"""

    is_cjk = True

    def __init__(self, fit_scale: float | None = 1.0, layout_units: bool = True) -> None:
        self.fit_scale = fit_scale
        self.layout_units = layout_units
        self.probed: list[tuple[float, tuple[float, float, float, float]]] = []

    def _layout_typesetting_units(
        self, units, box, scale, line_skip, paragraph, use_english_line_break=True
    ):
        values = (float(box.x), float(box.y), float(box.x2), float(box.y2))
        self.probed.append((round(float(scale), 4), values))
        if self.fit_scale is None or float(scale) > self.fit_scale:
            return (["layout-unit"] if self.layout_units else []), False
        return ["layout-unit"], True


def _fake_original(instance, paragraph, page, units, initial_scale=1.0,
                   use_english_line_break=True, apply_layout=False):
    if not apply_layout:
        return 0.5, None
    typeset_units, fits = instance._layout_typesetting_units(
        units, paragraph.box, initial_scale, 1.5, paragraph, True,
    )
    if fits:
        paragraph.scale = float(initial_scale)
        paragraph.pdf_paragraph_composition = list(typeset_units or [])
        return float(initial_scale), typeset_units
    paragraph.pdf_paragraph_composition = []
    return 0.5, None


class SealedTableLayoutHelperTest(unittest.TestCase):
    def test_table_layout_labels_are_recognized(self) -> None:
        for label in (
            "table_cell", "table_cell_hybrid", "table_text",
            "table_title", "wired_table_cell", "wireless_table_cell",
            "Table Cell Hybrid", "WIRED-TABLE-CELL",
        ):
            with self.subTest(label=label):
                self.assertTrue(table_layout.is_table_layout_label(label))
        for label in ("text", "plain text", "fallback_line", "", None):
            with self.subTest(label=label):
                self.assertFalse(table_layout.is_table_layout_label(label))

    def test_supports_checks_typesetting_api_only(self) -> None:
        class Complete:
            @staticmethod
            def _find_optimal_scale_and_layout(*args, **kwargs):
                return 1.0, None

            @staticmethod
            def _layout_typesetting_units(*args, **kwargs):
                return [], True

        class Incomplete:
            @staticmethod
            def _find_optimal_scale_and_layout(*args, **kwargs):
                return 1.0, None

        self.assertTrue(table_layout.supports(Complete))
        self.assertFalse(table_layout.supports(Incomplete))

    def test_measurement_floors_table_scale_at_ninety_percent(self) -> None:
        calls = []

        def original(instance, paragraph, page, units, initial_scale=1.0,
                     use_english_line_break=True, apply_layout=False):
            calls.append(apply_layout)
            return 0.65, None

        result = table_layout.find_optimal_scale_and_layout(
            original, _FakeInstance(), _FakeParagraph(_box(0, 0, 50, 12)), _page(),
            [], is_table_paragraph=lambda paragraph, page: True,
        )
        self.assertEqual(result[0], table_layout.MIN_TABLE_SCALE)
        self.assertEqual(calls, [False])

    def test_non_table_paragraph_passes_through_original(self) -> None:
        calls = []

        def original(instance, paragraph, page, units, initial_scale=1.0,
                     use_english_line_break=True, apply_layout=False):
            calls.append((initial_scale, use_english_line_break, apply_layout))
            return 1.0, ["untouched"]

        result = table_layout.find_optimal_scale_and_layout(
            original, _FakeInstance(), _FakeParagraph(_box(0, 0, 50, 12)), _page(),
            [], is_table_paragraph=lambda paragraph, page: False,
        )
        self.assertEqual(result, (1.0, ["untouched"]))
        self.assertEqual(calls, [(1.0, True, False)])

    def test_scale_ladder_prefers_1_0_then_0_95_then_0_9(self) -> None:
        for fit_scale, expected in ((1.0, 1.0), (0.95, 0.95), (0.9, 0.9)):
            with self.subTest(fit_scale=fit_scale):
                paragraph = _FakeParagraph(_box(20, 100, 120, 112))
                instance = _FakeInstance(fit_scale=fit_scale)
                result = table_layout.find_optimal_scale_and_layout(
                    _fake_original, instance, paragraph, _page(), [],
                    apply_layout=True,
                    is_table_paragraph=lambda paragraph, page: True,
                )
                self.assertEqual(result[0], expected)
                self.assertEqual(paragraph.scale, expected)

    def test_overflow_at_0_9_still_applies_units_without_losing_text(self) -> None:
        events: list[tuple[str, dict]] = []
        paragraph = _FakeParagraph(_box(20, 100, 120, 112))
        box_before = paragraph.box
        # 模拟 render 前 composition 已被清空的真实路径。
        paragraph.pdf_paragraph_composition = []
        instance = _FakeInstance(fit_scale=None, layout_units=True)

        def original(instance, paragraph, page, units, initial_scale=1.0,
                     use_english_line_break=True, apply_layout=False):
            if not apply_layout:
                return 0.5, None
            paragraph.scale = float(initial_scale)
            layout_units, fits = instance._layout_typesetting_units(
                units, paragraph.box, initial_scale, 1.5, paragraph, True,
            )
            self.assertTrue(fits, "helper 必须把 0.9 的 units 注入为可应用")
            paragraph.pdf_paragraph_composition = list(layout_units or [])
            return float(initial_scale), layout_units

        result = table_layout.find_optimal_scale_and_layout(
            original, instance, paragraph, _page(), ["u1", "u2"],
            apply_layout=True,
            is_table_paragraph=lambda paragraph, page: True,
            on_event=lambda kind, fields: events.append((kind, fields)),
        )
        self.assertEqual(result[0], table_layout.MIN_TABLE_SCALE)
        self.assertTrue(paragraph.pdf_paragraph_composition, "composition 不得为空")
        self.assertEqual(paragraph.scale, table_layout.MIN_TABLE_SCALE)
        self.assertIs(paragraph.box, box_before, "原 box 不得被替换")
        overflow = [fields for kind, fields in events if kind == "table_layout_overflow"]
        self.assertEqual(len(overflow), 1)
        self.assertEqual(overflow[0]["applied_scale"], table_layout.MIN_TABLE_SCALE)
        self.assertNotIn("expanded", overflow[0])
        self.assertFalse(any(kind == "table_layout_patch_error" for kind, _ in events))

    def test_no_units_at_0_9_falls_back_and_reports_patch_error(self) -> None:
        events: list[tuple[str, dict]] = []
        paragraph = _FakeParagraph(_box(20, 100, 120, 112))
        box_before = paragraph.box
        instance = _FakeInstance(fit_scale=None, layout_units=False)

        result = table_layout.find_optimal_scale_and_layout(
            _fake_original, instance, paragraph, _page(), [],
            apply_layout=True,
            is_table_paragraph=lambda paragraph, page: True,
            on_event=lambda kind, fields: events.append((kind, fields)),
        )
        self.assertEqual(result[0], 0.5, "无 unit 时回退原实现")
        self.assertIs(paragraph.box, box_before, "原 box 不得被替换")
        error_events = [fields for kind, fields in events if kind == "table_layout_patch_error"]
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0]["reason"], "no_typeset_units_at_min_scale")
        self.assertFalse(any(kind == "table_layout_overflow" for kind, _ in events))

    def test_summary_message_separates_unavailable_reasons_error_and_overflow(self) -> None:
        helper_missing = table_layout.summary_message([
            {"kind": "table_layout_patch_unavailable", "reason": "helper_missing"},
        ])
        api_missing = table_layout.summary_message([
            {"kind": "table_layout_patch_unavailable", "reason": "typesetting_api_missing"},
        ])
        self.assertIn("辅助模块未部署", helper_missing)
        self.assertIn("typesetting API 缺失", api_missing)
        self.assertNotEqual(helper_missing, api_missing)
        error = table_layout.summary_message([
            {"kind": "table_layout_patch_error", "preview": "x"},
        ])
        self.assertIn("执行异常", error)
        self.assertIn("回退", error)
        self.assertIn("可能小于 0.9", error)
        overflow = table_layout.summary_message([{
            "kind": "table_layout_overflow",
            "page": 5,
            "table": "表 2",
            "preview": "Monday",
        }])
        self.assertIn("保持 0.9", overflow)
        self.assertIn("允许文字溢出单元格", overflow)
        self.assertIn("第 5 页", overflow)
        self.assertIn("表 2", overflow)


if __name__ == "__main__":
    unittest.main()
