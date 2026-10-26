"""Regression tests for rotated table-header translation and vertical redraw."""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

import pymupdf


# 0.7.0 表格翻译封存：只验证封存实现的旋转表头契约，不依赖生产接线。
HELPER_PATH = (
    Path(__file__).parents[1]
    / "addon" / "content" / "python" / "archive" / "ztf_table2.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_table2", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
table2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(table2)


HEADERS = [
    "Network Scans",
    "Service Scans",
    "WordPress Scan",
    "Dirb Scan",
    "Webshell Upload",
    "Password Cracking",
    "Reverse Shell",
    "Privilege Escalation",
    "Service Stop",
    "Data Exfiltration",
    "False positives",
    "Robustness Score",
    "Detection Score",
]
TRANSLATIONS = [
    "网络扫描",
    "服务扫描",
    "WordPress 扫描",
    "Dirb 扫描",
    "Webshell 上传",
    "密码破解",
    "反弹 Shell",
    "权限提升",
    "服务停止",
    "数据窃取",
    "误报",
    "鲁棒性评分",
    "检测评分",
]
SOURCE_WIDTH = 612.0
PAGE_HEIGHT = 400.0
HEADER_X = 100.0
HEADER_SPACING = 13.6
HEADER_BASELINE = 315.0
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _write_source(path: Path) -> None:
    document = pymupdf.open()
    try:
        page = document.new_page(width=SOURCE_WIDTH, height=PAGE_HEIGHT)
        for index, text in enumerate(HEADERS):
            page.insert_text(
                (HEADER_X + index * HEADER_SPACING, HEADER_BASELINE),
                text,
                fontname="helv",
                fontsize=5,
                rotate=90,
            )
        document.save(str(path))
    finally:
        document.close()


def _write_output(
    path: Path,
    skip_index: int | None = None,
    include_source_half: bool = False,
) -> None:
    document = pymupdf.open()
    try:
        page = document.new_page(
            width=SOURCE_WIDTH * 2, height=PAGE_HEIGHT
        )
        if include_source_half:
            for index, text in enumerate(HEADERS):
                page.insert_text(
                    (HEADER_X + index * HEADER_SPACING, HEADER_BASELINE),
                    text,
                    fontname="helv",
                    fontsize=5,
                    rotate=90,
                )
        for index, text in enumerate(HEADERS):
            x = SOURCE_WIDTH + HEADER_X + index * HEADER_SPACING
            if index == skip_index:
                page.insert_text(
                    (x, HEADER_BASELINE),
                    "网络扫描",
                    fontname="china-s",
                    fontsize=5,
                )
            else:
                page.insert_text(
                    (x, HEADER_BASELINE),
                    text,
                    fontname="helv",
                    fontsize=5,
                    rotate=90,
                )
        document.save(str(path))
    finally:
        document.close()


def _cjk_chars(page: pymupdf.Page, clip: pymupdf.Rect) -> list[dict]:
    result = []
    data = page.get_text("rawdict", clip=clip)
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    value = char.get("c") or ""
                    if not CJK_RE.match(value):
                        continue
                    rect = pymupdf.Rect(char["bbox"])
                    result.append(
                        {
                            "char": value,
                            "rect": rect,
                            "cx": (rect.x0 + rect.x1) / 2.0,
                            "cy": (rect.y0 + rect.y1) / 2.0,
                        }
                    )
    return result


class RotatedHeaderGroupTest(unittest.TestCase):
    def test_finds_one_group_and_ignores_single_or_diagonal_labels(self) -> None:
        document = pymupdf.open()
        try:
            page = document.new_page(width=SOURCE_WIDTH, height=PAGE_HEIGHT)
            for index, text in enumerate(HEADERS):
                page.insert_text(
                    (HEADER_X + index * HEADER_SPACING, HEADER_BASELINE),
                    text,
                    fontname="helv",
                    fontsize=5,
                    rotate=90,
                )
            page.insert_text(
                (50, 100), "Solo Label", fontname="helv",
                fontsize=5, rotate=90,
            )
            point = pymupdf.Point(300, 100)
            page.insert_text(
                (300, 100), "Diagonal Label", fontname="helv",
                fontsize=5, morph=(point, pymupdf.Matrix(45)),
            )
            groups = table2.find_rotated_header_groups(document)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["page_index"], 0)
            self.assertEqual(
                [header["text"] for header in groups[0]["headers"]],
                HEADERS,
            )
            self.assertEqual(len(groups[0]["headers"]), len(HEADERS))
            for header in groups[0]["headers"]:
                self.assertAlmostEqual(header["angle"], 90.0, delta=0.5)
                self.assertAlmostEqual(header["size"], 5.0, delta=0.01)
        finally:
            document.close()


class Rotated270DrawTest(unittest.TestCase):
    def test_draws_clockwise_headers_from_top_to_bottom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clockwise.pdf"
            document = pymupdf.open()
            try:
                page = document.new_page(
                    width=SOURCE_WIDTH, height=PAGE_HEIGHT
                )
                for index, text in enumerate(HEADERS):
                    page.insert_text(
                        (HEADER_X + index * HEADER_SPACING, 100.0),
                        text,
                        fontname="helv",
                        fontsize=5,
                        rotate=270,
                    )
                groups = table2.find_rotated_header_groups(document)
                self.assertEqual(len(groups), 1)
                self.assertEqual(len(groups[0]["headers"]), len(HEADERS))
                for header in groups[0]["headers"]:
                    self.assertAlmostEqual(header["angle"], 270.0, delta=0.5)
                document.save(str(path))
            finally:
                document.close()

            stats = table2.apply_translations(
                str(path), groups, TRANSLATIONS
            )
            self.assertEqual(stats["drawn"], len(HEADERS))

            document = pymupdf.open(str(path))
            try:
                page = document[0]
                chars = _cjk_chars(
                    page, pymupdf.Rect(0, 0, SOURCE_WIDTH, PAGE_HEIGHT)
                )
                rect = groups[0]["headers"][0]["rect"]
                center_x = (rect.x0 + rect.x1) / 2.0
                near = [
                    char
                    for char in chars
                    if abs(char["cx"] - center_x) < 1.5
                ]
                near.sort(key=lambda char: char["cy"])
                expected = "".join(
                    char for char in TRANSLATIONS[0] if CJK_RE.match(char)
                )
                self.assertEqual(
                    "".join(char["char"] for char in near), expected
                )
                self.assertAlmostEqual(near[0]["rect"].y0, rect.y0, delta=0.6)
                self.assertGreater(near[-1]["cy"], near[0]["cy"])
            finally:
                document.close()


class UntranslatedHeaderTest(unittest.TestCase):
    def test_detects_bilingual_english_group_and_skips_cjk_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "source.pdf"
            output_path = root / "output.pdf"
            cjk_path = root / "output-cjk.pdf"
            _write_source(source_path)
            _write_output(output_path, include_source_half=True)
            _write_output(cjk_path, skip_index=2, include_source_half=True)

            groups = table2.untranslated_headers(
                str(source_path), str(output_path)
            )
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["page_index"], 0)
            self.assertEqual(
                [header["text"] for header in groups[0]["headers"]],
                HEADERS,
            )
            for header in groups[0]["headers"]:
                self.assertGreaterEqual(header["rect"].x0, SOURCE_WIDTH)
            self.assertGreater(
                min(header["rect"].x0 for header in groups[0]["headers"]),
                SOURCE_WIDTH,
            )
            self.assertAlmostEqual(
                groups[0]["headers"][0]["rect"].x0,
                SOURCE_WIDTH + HEADER_X - 5.38,
                delta=1.0,
            )

            self.assertEqual(
                table2.untranslated_headers(str(source_path), str(cjk_path)),
                [],
            )


class ResponseParsingTest(unittest.TestCase):
    def test_parses_valid_fenced_and_explained_arrays(self) -> None:
        payload = json.dumps(
            ["网络扫描", "服务扫描"], ensure_ascii=False
        )
        self.assertEqual(
            table2.parse_response(payload, 2), ["网络扫描", "服务扫描"]
        )
        fenced = "```json\n%s\n```" % payload
        self.assertEqual(
            table2.parse_response(fenced, 2),
            ["网络扫描", "服务扫描"],
        )
        self.assertEqual(
            table2.parse_response(
                "翻译结果如下：\n%s\n以上。" % payload, 2
            ),
            ["网络扫描", "服务扫描"],
        )

    def test_rejects_bad_counts_and_non_json(self) -> None:
        self.assertIsNone(
            table2.parse_response('["网络扫描", "服务扫描"]', 3)
        )
        self.assertIsNone(table2.parse_response("网络扫描、服务扫描", 2))
        self.assertIsNone(table2.parse_response('["网络扫描", ""]', 2))
        self.assertIsNone(table2.parse_response('["网络扫描", 1]', 2))


class TranslationValidationTest(unittest.TestCase):
    def test_accepts_only_real_chinese(self) -> None:
        self.assertTrue(
            table2.is_valid_translation("Network Scans", "网络扫描")
        )
        self.assertTrue(
            table2.is_valid_translation("Network Scans", "网络 Scans")
        )
        self.assertFalse(
            table2.is_valid_translation("Network Scans", "Network Scans")
        )
        self.assertFalse(
            table2.is_valid_translation("Network Scans", "NETWORK SCANS")
        )
        self.assertFalse(
            table2.is_valid_translation("Test 标题", "test 标题")
        )
        self.assertFalse(table2.is_valid_translation("Network Scans", ""))
        self.assertFalse(table2.is_valid_translation("Network Scans", "   "))
        self.assertFalse(table2.is_valid_translation("Network Scans", "12345"))


class ApplyTranslationsTest(unittest.TestCase):
    def test_redacts_english_and_draws_upright_vertical_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "source.pdf"
            output_path = root / "output.pdf"
            _write_source(source_path)
            _write_output(output_path)

            groups = table2.untranslated_headers(
                str(source_path), str(output_path)
            )
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]["headers"]), len(HEADERS))
            centers = [
                (header["rect"].x0 + header["rect"].x1) / 2.0
                for header in groups[0]["headers"]
            ]

            stats = table2.apply_translations(
                str(output_path), groups, TRANSLATIONS
            )
            self.assertEqual(stats["groups"], 1)
            self.assertEqual(stats["redacted"], len(HEADERS))
            self.assertEqual(stats["drawn"], len(HEADERS))
            self.assertEqual(stats["failed"], [])

            document = pymupdf.open(str(output_path))
            try:
                page = document[0]
                right = pymupdf.Rect(
                    SOURCE_WIDTH, 0, SOURCE_WIDTH * 2, PAGE_HEIGHT
                )
                remaining = page.get_text("text", clip=right)
                for title in HEADERS:
                    self.assertNotIn(title, remaining)

                chars = _cjk_chars(page, right)
                for index, translation in enumerate(TRANSLATIONS):
                    expected = "".join(
                        char for char in translation if CJK_RE.match(char)
                    )
                    near = [
                        char
                        for char in chars
                        if abs(char["cx"] - centers[index]) < 1.5
                    ]
                    near.sort(key=lambda char: -char["cy"])
                    self.assertEqual(
                        "".join(char["char"] for char in near),
                        expected,
                        translation,
                    )
                    self.assertGreaterEqual(len(near), 1)
                    for char in near:
                        rect = char["rect"]
                        ratio = rect.width / rect.height
                        self.assertGreaterEqual(ratio, 0.6)
                        self.assertLessEqual(ratio, 1.6)
                        self.assertLess(
                            abs(char["cx"] - centers[index]), 1.5
                        )
                    if len(near) > 1:
                        self.assertGreater(
                            near[0]["cy"],
                            near[-1]["cy"],
                            "angle=90 must read from bottom to top",
                        )
            finally:
                document.close()


class RestoreRotatedHeadersTest(unittest.TestCase):
    def test_skips_without_translation_and_applies_with_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "source.pdf"
            skip_path = root / "skip.pdf"
            apply_path = root / "apply.pdf"
            _write_source(source_path)
            _write_output(skip_path)
            _write_output(apply_path)

            before = skip_path.read_bytes()
            skipped = table2.restore_rotated_headers(
                str(source_path), str(skip_path), lambda headers: None
            )
            self.assertTrue(skipped.get("skipped"))
            self.assertEqual(skip_path.read_bytes(), before)

            document = pymupdf.open(str(skip_path))
            try:
                page = document[0]
                right = pymupdf.Rect(
                    SOURCE_WIDTH, 0, SOURCE_WIDTH * 2, PAGE_HEIGHT
                )
                text = page.get_text("text", clip=right)
                for title in HEADERS:
                    self.assertIn(title, text)
            finally:
                document.close()

            requested = []

            def translate(headers):
                requested.append(list(headers))
                return list(TRANSLATIONS)

            stats = table2.restore_rotated_headers(
                str(source_path), str(apply_path), translate
            )
            self.assertEqual(requested, [HEADERS])
            self.assertEqual(stats["requested"], len(HEADERS))
            self.assertEqual(stats["translated"], len(HEADERS))
            self.assertEqual(stats["redacted"], len(HEADERS))
            self.assertEqual(stats["drawn"], len(HEADERS))
            self.assertEqual(stats["failed"], [])

            document = pymupdf.open(str(apply_path))
            try:
                page = document[0]
                right = pymupdf.Rect(
                    SOURCE_WIDTH, 0, SOURCE_WIDTH * 2, PAGE_HEIGHT
                )
                text = page.get_text("text", clip=right)
                for title in HEADERS:
                    self.assertNotIn(title, text)
                compact = "".join(
                    page.get_text("text", clip=right).split()
                )
                self.assertIn("网络扫描", compact)
                self.assertTrue(_cjk_chars(page, right))
            finally:
                document.close()


if __name__ == "__main__":
    unittest.main()
