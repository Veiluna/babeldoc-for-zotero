"""封存回归：旋转表头恢复契约（0.7.0 起不参与生产翻译）。

0.7.0 表格内容封存后，runner 不再翻译/重绘旋转表头；本测试直接验证
archive/ztf_table2.py 的 restore_rotated_headers 封存契约，不依赖 runner。
使用 unittest.TestCase 以便 python -m unittest discover 能发现。
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "addon" / "content" / "python" / "archive" / "ztf_table2.py"
SPEC = importlib.util.spec_from_file_location("ztf_table2_archived", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

HEADERS = [
    "Network Scans", "Service Scans", "WordPress Scan", "Dirb Scan",
    "Webshell Upload", "Password Cracking", "Reverse Shell",
    "Privilege Escalation", "Service Stop", "Data Exfiltration",
    "False positives", "Robustness Score", "Detection Score",
]
CHINESE = [
    "网络扫描", "服务扫描", "WordPress扫描", "Dirb扫描", "Webshell上传",
    "密码破解", "反向Shell", "权限提升", "服务停止", "数据窃取", "误报",
    "鲁棒性得分", "检测得分",
]
MIN_X = 40.0
STEP_X = 13.6
BOTTOM = 315.0
SIZE = 5.0
SOURCE_WIDTH = 300.0
OUTPUT_WIDTH = 600.0


def _write_headers(page: pymupdf.Page, offset_x: float, texts: list[str]) -> None:
    x = offset_x + MIN_X
    for text in texts:
        page.insert_text((x, BOTTOM), text, fontname="helv", fontsize=SIZE, rotate=90)
        x += STEP_X


def _make_pdfs(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "source.pdf"
    output = directory / "output.pdf"
    document = pymupdf.open()
    page = document.new_page(width=SOURCE_WIDTH, height=400)
    _write_headers(page, 0.0, HEADERS)
    document.save(str(source))
    document.close()
    document = pymupdf.open()
    page = document.new_page(width=OUTPUT_WIDTH, height=400)
    _write_headers(page, SOURCE_WIDTH, HEADERS)
    document.save(str(output))
    document.close()
    return source, output


def _cjk_count(page: pymupdf.Page, clip: pymupdf.Rect) -> int:
    count = 0
    for block in page.get_text("rawdict", clip=clip)["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if 0x3400 <= ord(char["c"]) <= 0x9FFF:
                        count += 1
    return count


class SealedRotatedHeaderTest(unittest.TestCase):
    def _run(self, translator):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source, output = _make_pdfs(Path(temporary.name))
        stats = helper.restore_rotated_headers(str(source), str(output), translator)
        return stats, output

    def test_restore_draws_vertical_chinese_when_translation_valid(self) -> None:
        stats, output = self._run(
            lambda headers: json.dumps(CHINESE, ensure_ascii=False)
        )
        self.assertEqual(stats["requested"], len(HEADERS), stats)
        self.assertGreaterEqual(stats.get("drawn", 0), len(CHINESE), stats)
        clip = pymupdf.Rect(SOURCE_WIDTH + MIN_X - 4, 250, SOURCE_WIDTH + 600, 330)
        document = pymupdf.open(str(output))
        try:
            self.assertGreaterEqual(_cjk_count(document[0], clip), len(CHINESE))
            for original in HEADERS:
                self.assertFalse(document[0].search_for(original, clip=clip), original)
            for block in document[0].get_text("dict", clip=clip)["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(span["text"] for span in line["spans"])
                    if not any(0x3400 <= ord(char) <= 0x9FFF for char in text):
                        continue
                    self.assertGreater(abs((line.get("dir") or (1.0, 0.0))[0]), 0.99, text)
        finally:
            document.close()

    def test_restore_keeps_english_when_translation_missing(self) -> None:
        stats, output = self._run(lambda headers: None)
        self.assertEqual(stats.get("skipped"), "translate_returned_none", stats)
        document = pymupdf.open(str(output))
        try:
            self.assertTrue(document[0].search_for(
                "Network Scans", clip=pymupdf.Rect(SOURCE_WIDTH, 0, 900, 400)
            ))
        finally:
            document.close()

    def test_restore_keeps_english_when_translation_is_invalid_json(self) -> None:
        stats, output = self._run(lambda headers: "not a json array")
        self.assertEqual(stats.get("skipped"), "parse_failed", stats)
        document = pymupdf.open(str(output))
        try:
            self.assertTrue(document[0].search_for(
                "Network Scans", clip=pymupdf.Rect(SOURCE_WIDTH, 0, 900, 400)
            ))
        finally:
            document.close()

    def test_restore_rejects_translation_count_mismatch(self) -> None:
        stats, output = self._run(lambda headers: ["网络扫描"])
        self.assertEqual(stats.get("skipped"), "translation_count_mismatch", stats)
        document = pymupdf.open(str(output))
        try:
            self.assertTrue(document[0].search_for(
                "Network Scans", clip=pymupdf.Rect(SOURCE_WIDTH, 0, 900, 400)
            ))
        finally:
            document.close()


if __name__ == "__main__":
    unittest.main()
