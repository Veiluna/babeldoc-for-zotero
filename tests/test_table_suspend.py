"""生产行为回归：0.7.0 表格翻译封存（只翻译题注/正文）。

用户定案：表格内容（表头/表体/旋转表头）一律保留原文，仅题注走普通翻译；
4 个表格 helper 不部署、不加载。本测试用真实 BabelDOC + 本地 mock 接口验证
生产路径，不消耗真实 API 额度。
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
ARTIFACTS = ROOT / "build" / "table-suspend"
CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
FIXED_TRANSLATION = "表格内容封存后仅题注翻译"
BODY_TEXT = "We describe the dataset collection methodology in detail."
CAPTION_TEXT = "Table 1: Performance of the proposed method."
OUTSIDE_TEXT = "The table above summarizes the results."

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
    left, top = 90, 160
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
        ["Monday", "0.88", "0.85"],
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
    table_page.insert_text((90, 140), CAPTION_TEXT, fontname="times-roman",
                           fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
    _draw_table(table_page)
    table_page.insert_text((90, 300), OUTSIDE_TEXT, fontname="times-roman",
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
            "id": "table-suspend-mock",
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


class TableSuspendBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.source = ARTIFACTS / "table-suspend.pdf"
        _write_fixture(cls.source)
        cls.deployed_runner = _deploy(ARTIFACTS / "deployed")
        cls.destination = ARTIFACTS / "run"
        (cls.completed, cls.result, cls.requests, cls.inputs) = _run_runner(
            cls.deployed_runner, cls.source, cls.destination,
        )

    def test_deployed_directory_has_no_archived_table_helpers(self) -> None:
        deployed = sorted(path.name for path in (ARTIFACTS / "deployed").glob("*.py"))
        for archived in (
            "ztf_table_text.py", "ztf_table_headers.py",
            "ztf_table2.py", "ztf_table_layout.py",
        ):
            self.assertNotIn(archived, deployed)

    def test_table_cells_keep_source_english(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-3000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-3000:])
        document = pymupdf.open(Path(self.result["mono"]))
        try:
            page = document[1]
            text = page.get_text()
        finally:
            document.close()
        for value in ("Method", "Precision", "Recall", "Swin", "0.91", "Monday"):
            self.assertIn(value, text, f"表格内容必须保留原文：{value}")
        # 表内单元格不应被换成 mock 中文。
        table_region = pymupdf.open(Path(self.result["mono"]))
        try:
            clip = pymupdf.Rect(85, 155, 450, 250)
            table_text = table_region[1].get_text("text", clip=clip)
        finally:
            table_region.close()
        self.assertFalse(CJK_RE.search(table_text), "表格内容不得被翻译成中文")

    def test_caption_and_outside_text_are_translated(self) -> None:
        document = pymupdf.open(Path(self.result["mono"]))
        try:
            caption = document[1].get_text("text", clip=pymupdf.Rect(85, 125, 520, 150))
            outside = document[1].get_text("text", clip=pymupdf.Rect(85, 285, 520, 315))
            body = document[0].get_text()
        finally:
            document.close()
        self.assertIn(FIXED_TRANSLATION, caption, "表格题注必须走普通翻译")
        # 题注合并回归：不得出现 "Tabl" 等英文碎片/中英混杂。
        self.assertIsNone(LATIN_RE.search(caption), "题注译文不得残留拉丁字母：" + repr(caption))
        self.assertNotIn("Tabl", caption)
        self.assertIn(FIXED_TRANSLATION, outside, "表外正文必须走普通翻译")
        self.assertIn(FIXED_TRANSLATION, body, "正文必须走普通翻译")

    def test_no_table_warnings_or_table2_requests(self) -> None:
        records = _records(self.destination)
        events = [record.get("event") for record in records]
        self.assertIn("table_translation_suspended", events)
        for forbidden in (
            "table_analysis", "table2_restore", "table2_translate_error",
            "table_layout_patch", "table_layout_overflow",
        ):
            self.assertNotIn(forbidden, events, f"封存后不应再出现 {forbidden}")
        warnings = [
            record for record in records
            if record.get("event") == "job_finished"
        ]
        self.assertEqual(warnings[-1].get("table_warnings"), [])
        self.assertEqual(self.result.get("table_warnings"), [])
        self.assertEqual(self.requests, 1, "mock 单批请求数应为 1")
        # 请求输入只含题注/正文，不含表格单元格。
        sent = "\n".join(str(item.get("input", "")) for item in self.inputs)
        self.assertIn(CAPTION_TEXT, sent)
        self.assertIn(BODY_TEXT, sent)
        self.assertNotIn("Swin", sent)
        self.assertNotIn("Precision", sent)


if __name__ == "__main__":
    unittest.main()
