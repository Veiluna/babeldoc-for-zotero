"""End-to-end regression: 0.7.0 表格封存 + 多行题注完整翻译。

用户定案：表格内容保留原文、仅题注/表外正文翻译；本 e2e 专门覆盖“多行
题注”场景（现有合成 fixture 原先只有单行题注）：BabelDOC 的 fallback_line
阶段容易把一条多行题注拆成 "Tabl" + 其余文本 + "." 碎片，若未合并会出现
中英混杂。真实 BabelDOC + 本地 mock 接口，不消耗真实额度。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path

import pymupdf


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
RUNNER_JS = ROOT / "addon" / "content" / "modules" / "runner.js"
HELPER_DIR = ROOT / "addon" / "content" / "python"
ARTIFACTS = ROOT / "build" / "table-headers"
CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
FIXED_TRANSLATION = "多行题注应完整翻译"
BODY_TEXT = "We describe the dataset collection methodology in detail."
CAPTION_LINE_1 = "Table 1: Performance of the proposed method"
CAPTION_LINE_2 = "on the benchmark dataset."
OUTSIDE_TEXT = "The table above summarizes the results."
NOTE_LINE_1 = "* Values accurate at time of publication."
NOTE_LINE_2 = (
    "For the full table (including corruptions), "
    "see minor residual corrections in the online appendix."
)

sys.path.insert(0, str(Path(__file__).parent))
from mock_openai_server import MockOpenAIHandler, MockOpenAIServer, _prompt_items  # noqa: E402


def _runner_js_helpers() -> list[str]:
    source = RUNNER_JS.read_text(encoding="utf-8")
    block = re.search(r"var pythonHelpers = \[(.*?)\];", source, re.S)
    assert block is not None, "runner.js 中未找到 pythonHelpers 复制清单"
    helpers = re.findall(r'\["([^"]+\.py)"', block.group(1))
    assert helpers
    return helpers


def _deploy(destination: Path) -> Path:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    deployed_runner = destination / RUNNER.name
    shutil.copyfile(RUNNER, deployed_runner)
    for helper in _runner_js_helpers():
        shutil.copyfile(HELPER_DIR / helper, destination / helper)
    return deployed_runner


def _draw_table(page: pymupdf.Page) -> None:
    left, top = 90, 170
    widths = [90, 90, 90]
    row_height = 26
    rows = 3
    for index in range(rows + 1):
        y = top + index * row_height
        page.draw_line((left, y), (left + sum(widths), y), width=0.7)
    x = left
    for width in widths:
        page.draw_line((x, top), (x, top + rows * row_height), width=0.7)
        x += width
    page.draw_line((x, top), (x, top + rows * row_height), width=0.7)
    cells = [
        ["Method", "Precision", "Recall"],
        ["Swin", "0.91", "0.87"],
        ["Monday", "* p < 0.05", "**p<0.01"],
    ]
    for row_index, row in enumerate(cells):
        x = left
        for column_index, value in enumerate(row):
            page.insert_text(
                (x + 6, top + row_index * row_height + 17),
                value,
                fontname="times-roman",
                fontfile=r"C:\Windows\Fonts\times.ttf",
                fontsize=10,
            )
            x += widths[column_index]


def _write_fixture(path: Path) -> None:
    document = pymupdf.open()
    body_page = document.new_page(width=595, height=842)
    body_page.insert_text((60, 100), BODY_TEXT, fontname="times-roman",
                          fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
    table_page = document.new_page(width=595, height=842)
    table_page.insert_text((90, 140), CAPTION_LINE_1, fontname="times-roman",
                           fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
    table_page.insert_text((90, 152), CAPTION_LINE_2, fontname="times-roman",
                           fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
    _draw_table(table_page)
    table_page.insert_text((90, 262), NOTE_LINE_1, fontname="times-roman",
                           fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
    table_page.insert_text((90, 274), NOTE_LINE_2, fontname="times-roman",
                           fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
    table_page.insert_text((90, 330), OUTSIDE_TEXT, fontname="times-roman",
                           fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
    document.save(path)
    document.close()


class _FixedChineseHandler(MockOpenAIHandler):
    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        items = _prompt_items(payload)
        self.server.count_request()
        self.server.inputs.extend(items)
        outputs = [
            {"id": item.get("id", index), "output": FIXED_TRANSLATION}
            for index, item in enumerate(items)
        ]
        self._send_json(200, {
            "id": "table-headers-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(outputs, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def _run_runner(runner_path: Path, source: Path, destination: Path, timeout: int = 600):
    server = MockOpenAIServer(("127.0.0.1", 0), _FixedChineseHandler)
    server.inputs = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("ZTF_") and "API_KEY" not in key.upper()
    }
    environment.update({
        "ZTF_OPENAI_API_KEY": "mock-test-key",
        "ZTF_MAX_OUTPUT_TOKENS": "8192",
        "ZTF_MAX_REQUESTS": "20",
        "ZTF_REPAIR_FAILED": "1",
        "ZTF_TRANSLATE_REFERENCES": "0",
        "ZTF_JOB_LOG_PATH": str(destination / "job.jsonl"),
        "PYTHONIOENCODING": "utf-8",
    })
    command = [
        sys.executable, str(runner_path),
        "--files", str(source),
        "--output", str(destination / "output"),
        "--working-dir", str(destination / "work"),
        "--openai", "--openai-model", "mock-model",
        "--openai-base-url", f"http://127.0.0.1:{server.server_address[1]}/v1",
        "--lang-in", "en", "--lang-out", "zh",
        "--no-dual", "--no-auto-extract-glossary",
        "--disable-same-text-fallback",
        "--watermark-output-mode", "no_watermark",
        "--skip-scanned-detection",
    ]
    try:
        completed = subprocess.run(
            command, cwd=str(ROOT), env=environment, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    (destination / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (destination / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    result = None
    for line in completed.stdout.splitlines():
        if line.startswith("ZTF_RESULT "):
            result = json.loads(line[len("ZTF_RESULT "):])
    return completed, result, server.request_count, list(server.inputs)


def _records(destination: Path) -> list[dict]:
    path = destination / "job.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TableHeadersE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.source = ARTIFACTS / "tables.pdf"
        _write_fixture(cls.source)
        cls.deployed_runner = _deploy(ARTIFACTS / "deployed")
        cls.destination = ARTIFACTS / "run"
        (cls.completed, cls.result, cls.requests, cls.inputs) = _run_runner(
            cls.deployed_runner, cls.source, cls.destination,
        )

    def test_multiline_caption_is_one_complete_translation(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-3000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-3000:])
        # 合并成功后，完整的两行题注必须作为同一个 item 送译；若被拆成
        # "Tabl" + 其余文本 + "." 碎片，就不可能有 item 同时包含两行。
        sent = [str(item.get("input", "")) for item in self.inputs]
        self.assertTrue(
            any("Table 1:" in text and "benchmark" in text for text in sent),
            "多行题注必须作为同一段送译：" + repr(sent)[:800],
        )
        self.assertFalse(
            any(text.strip() in ("Tabl", "Table", ".") for text in sent),
            "题注不得以碎片 item 送译：" + repr(sent)[:800],
        )
        document = pymupdf.open(Path(self.result["mono"]))
        try:
            caption = document[1].get_text("text", clip=pymupdf.Rect(85, 125, 540, 158))
        finally:
            document.close()
        self.assertIn(FIXED_TRANSLATION, caption, caption)
        self.assertIsNone(LATIN_RE.search(caption), "题注译文不得残留拉丁字母：" + repr(caption))
        self.assertNotIn("Tabl", caption)

    def test_marker_cells_inside_table_are_not_sent_for_translation(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-3000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-3000:])
        sent = "\n".join(str(item.get("input", "")) for item in self.inputs)
        for marker in ("* p < 0.05", "**p<0.01"):
            self.assertNotIn(marker, sent, "表内 marker 单元格不得进入请求：" + marker)
        document = pymupdf.open(Path(self.result["mono"]))
        try:
            table_text = document[1].get_text("text", clip=pymupdf.Rect(85, 155, 450, 250))
        finally:
            document.close()
        for marker in ("* p < 0.05", "**p<0.01"):
            self.assertIn(marker, table_text, "表内 marker 单元格必须保留原文：" + marker)

    def test_table_cells_keep_source_english(self) -> None:
        document = pymupdf.open(Path(self.result["mono"]))
        try:
            text = document[1].get_text()
        finally:
            document.close()
        for value in ("Method", "Precision", "Recall", "Swin", "0.91", "Monday"):
            self.assertIn(value, text, f"表格内容必须保留原文：{value}")

    def test_table_note_is_preserved_verbatim(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-3000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-3000:])
        sent = [str(item.get("input", "")) for item in self.inputs]
        all_sent = "\n".join(sent)
        # 方案 A：表注保留原文，不进入请求/补译。
        self.assertNotIn("Values accurate", all_sent)
        self.assertNotIn("minor residual corrections", all_sent)
        events = [record.get("event") for record in _records(self.destination)]
        self.assertNotIn("table_note_merged", events)
        self.assertNotIn("table_note_merge_warning", events)
        # 输出表注区域保留英文原 glyph，无中文、无固定译文。
        document = pymupdf.open(Path(self.result["mono"]))
        try:
            note_text = document[1].get_text("text", clip=pymupdf.Rect(85, 250, 540, 305))
        finally:
            document.close()
        normalized = re.sub(r"\s+", " ", note_text).strip()
        self.assertIn("Values accurate", normalized, "表注必须保留原文：" + repr(note_text))
        self.assertIn("minor residual corrections", normalized, "表注必须保留原文：" + repr(note_text))
        self.assertIsNone(CJK_RE.search(note_text), "表注不得出现中文：" + repr(note_text))
        self.assertNotIn(FIXED_TRANSLATION, note_text)

    def test_caption_and_outside_text_are_translated_without_table_events(self) -> None:
        records = _records(self.destination)
        events = [record.get("event") for record in records]
        self.assertIn("table_translation_suspended", events)
        for forbidden in ("table_analysis", "table2_restore", "table_layout_patch"):
            self.assertNotIn(forbidden, events)
        self.assertEqual(self.requests, 1)
        sent = "\n".join(str(item.get("input", "")) for item in self.inputs)
        self.assertIn(BODY_TEXT, sent)
        self.assertIn(OUTSIDE_TEXT, sent)
        self.assertNotIn("Precision", sent)


if __name__ == "__main__":
    unittest.main()
