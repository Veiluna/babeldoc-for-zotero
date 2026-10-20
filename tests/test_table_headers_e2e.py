"""End-to-end regressions for table header translation and body protection (#18).

Run with the installed BabelDOC 0.6.4 environment:

    python tests/test_table_headers_e2e.py

The fixture is synthetic; the mock API never calls a real provider. Diagnostic
artefacts live under build/table-headers/.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
import unittest

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
from mock_openai_server import (
    MockOpenAIHandler,
    MockOpenAIServer,
    _prompt_items,
    _synthetic_translation,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "addon/content/python/ztf_babeldoc_runner.py"
ARTIFACTS = ROOT / "build/table-headers"
FONT_ROMAN = r"C:\Windows\Fonts\times.ttf"
FONT_BOLD = r"C:\Windows\Fonts\timesbd.ttf"
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _text(page: pymupdf.Page, x: float, y: float, value: str, size: float = 9,
          bold: bool = False) -> None:
    page.insert_text(
        (x, y),
        value,
        fontname="times-bold" if bold else "times-roman",
        fontfile=FONT_BOLD if bold else FONT_ROMAN,
        fontsize=size,
    )


def _center_text(page: pymupdf.Page, box: tuple[float, float, float, float],
                 value: str, size: float = 9, bold: bool = False) -> None:
    width = pymupdf.get_text_length(value, fontname="times-roman", fontsize=size)
    x = box[0] + (box[2] - box[0] - width) / 2
    _text(page, x, box[3] - 5, value, size=size, bold=bold)


def _draw_grid(page: pymupdf.Page, left: float, top: float, widths: list[float],
               row_height: float, rows: int,
               merged: dict[int, list[tuple[int, int]]] | None = None) -> list[float]:
    total = sum(widths)
    ys = [top + index * row_height for index in range(rows + 1)]
    for y in ys:
        page.draw_line((left, y), (left + total, y), width=0.7)
    boundaries = [left]
    x = left
    for width in widths:
        x += width
        boundaries.append(x)
    for row in range(rows):
        spans = (merged or {}).get(row, [])
        for boundary_index, boundary in enumerate(boundaries):
            if any(start < boundary_index < end for start, end in spans):
                continue
            page.draw_line((boundary, ys[row]), (boundary, ys[row + 1]), width=0.7)
    return ys


def _table_one(page: pymupdf.Page) -> None:
    _text(page, 50, 60, "Table Header Study", size=17, bold=True)
    _text(page, 50, 88, "We compare three detectors on the SECOND dataset.", size=10)
    _text(page, 50, 112, "Table 1: Detection results on the SECOND dataset.", size=10)
    left, top = 50, 128
    widths = [95, 75, 75, 75, 75, 70, 65]
    row_height = 22
    ys = _draw_grid(
        page, left, top, widths, row_height, 6,
        merged={0: [(1, 5), (5, 7)]},
    )
    rows = [
        ["", "Detection Performance", "", "", "", "Efficiency", ""],
        ["Method", "Precision", "Recall", "F1", "Real-time", "Params", "FPS"],
        ["ChangeMamba", "0.912", "0.887", "0.899", "YES", "33M", "51"],
        ["SECOND", "0.874", "0.851", "0.862", "NO", "21M", "87"],
        ["Swin", "0.851", "0.832", "0.840", "YES", "27M", "64"],
        ["CenterPoint", "0.836", "0.815", "0.824", "YES", "18M", "75"],
    ]
    for row_index, row in enumerate(rows):
        x = left
        for column_index, value in enumerate(row):
            if value:
                x2 = x + widths[column_index]
                if row_index == 0 and column_index == 1:
                    x2 = left + sum(widths[0:5])
                if row_index == 0 and column_index == 5:
                    x2 = left + sum(widths[0:7])
                _center_text(
                    page,
                    (x, ys[row_index], x2, ys[row_index + 1]),
                    value,
                    size=9,
                    bold=row_index < 2,
                )
            x += widths[column_index]
    _text(
        page,
        50,
        330,
        "* Precision, Recall and F1 are percentages. ChangeMamba is a state space model.",
        size=9,
    )
    _text(
        page,
        50,
        346,
        "Note: YES and NO indicate whether the detector runs in real time.",
        size=9,
    )


def _table_two(page: pymupdf.Page) -> None:
    _text(page, 50, 80, "Table 2: Ablation study of the proposed modules.", size=10)
    left, top = 70, 96
    widths = [140, 110, 110, 110]
    row_height = 24
    ys = _draw_grid(page, left, top, widths, row_height, 6)
    rows = [
        ["Method", "ChangeMamba", "SECOND", "Swin"],
        ["Precision", "0.91", "0.87", "0.85"],
        ["Recall", "0.88", "0.85", "0.83"],
        ["F1", "0.89", "0.86", "0.84"],
        ["Params", "33M", "21M", "27M"],
        ["Real-time", "YES", "NO", "YES"],
    ]
    for row_index, row in enumerate(rows):
        x = left
        for column_index, value in enumerate(row):
            _center_text(
                page,
                (x, ys[row_index], x + widths[column_index], ys[row_index + 1]),
                value,
                size=9,
                bold=row_index == 0,
            )
            x += widths[column_index]
    _text(
        page,
        50,
        280,
        "The first column lists the metric and the remaining columns list the detectors.",
        size=9,
    )


def _table_three(page: pymupdf.Page) -> None:
    _text(page, 50, 80, "Table 3: Terms used in this study.", size=10)
    left, top = 90, 96
    widths = [150, 150]
    row_height = 24
    ys = _draw_grid(page, left, top, widths, row_height, 4)
    rows = [
        ["Precision", "Recall"],
        ["F1", "AP"],
        ["mAP", "IoU"],
        ["Params", "FPS"],
    ]
    for row_index, row in enumerate(rows):
        x = left
        for column_index, value in enumerate(row):
            _center_text(
                page,
                (x, ys[row_index], x + widths[column_index], ys[row_index + 1]),
                value,
                size=9,
            )
            x += widths[column_index]
    _text(
        page,
        50,
        230,
        "This glossary-like table has no data rows and no reliable header band.",
        size=9,
    )


def write_fixture(path: Path) -> None:
    document = pymupdf.open()
    for builder in (_table_one, _table_two, _table_three):
        page = document.new_page(width=595, height=842)
        builder(page)
    document.save(path)
    document.close()


class TableHeaderHandler(MockOpenAIHandler):
    """Record every item and optionally echo confirmed table headers unchanged."""

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        items = _prompt_items(payload)
        self.server.count_request()
        self.server.inputs.extend(items)
        outputs = []
        for index, item in enumerate(items):
            source = str(item.get("input", ""))
            if self.server.header_echo and item.get("layout_label") == "table_header":
                translated = source
            else:
                translated = _synthetic_translation(source, index)
            outputs.append({"id": item.get("id", index), "output": translated})
        self._send_json(
            200,
            {
                "id": "table-mock",
                "object": "chat.completion",
                "created": 0,
                "model": "mock-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(outputs, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def read_pdf_pages(pdf_path: Path) -> list[str]:
    pages = []
    with pymupdf.open(pdf_path) as document:
        for page in document:
            pages.append(page.get_text())
    return pages


def read_job_records(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TableHeaderIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.source = ARTIFACTS / "tables.pdf"
        write_fixture(cls.source)
        deployed = ARTIFACTS / "deployed"
        deployed.mkdir(exist_ok=True)
        shutil.copyfile(RUNNER, deployed / RUNNER.name)
        for helper in (
            "ztf_inline_formulas.py", "ztf_style_guards.py",
            "ztf_links.py", "ztf_table_headers.py",
        ):
            source = ROOT / "addon/content/python" / helper
            shutil.copyfile(source, deployed / helper)
        cls.completed, cls.result, cls.error, cls.inputs, cls.records, cls.request_count = cls.run_job()

    @classmethod
    def run_job(cls, *, header_echo: bool = False):
        server = MockOpenAIServer(("127.0.0.1", 0), TableHeaderHandler)
        server.inputs = []
        server.header_echo = header_echo
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        destination = ARTIFACTS / ("header-echo" if header_echo else "main")
        destination.mkdir(parents=True, exist_ok=True)
        for stale in (destination / "job.jsonl",):
            if stale.is_file():
                stale.unlink()
        for stale_dir in (destination / "output", destination / "work"):
            if stale_dir.is_dir():
                shutil.rmtree(stale_dir, ignore_errors=True)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ZTF_") and "API_KEY" not in key.upper()
        }
        environment.update(
            {
                "ZTF_OPENAI_API_KEY": "mock-test-key",
                "ZTF_MAX_OUTPUT_TOKENS": "8192",
                "ZTF_REPAIR_FAILED": "1",
                "ZTF_TRANSLATE_REFERENCES": "0",
                "ZTF_JOB_LOG_PATH": str(destination / "job.jsonl"),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        deployed_runner = ARTIFACTS / "deployed" / RUNNER.name
        command = [
            sys.executable,
            str(deployed_runner),
            "--files", str(cls.source),
            "--output", str(destination / "output"),
            "--working-dir", str(destination / "work"),
            "--openai", "--openai-model", "mock-model",
            "--openai-base-url", f"http://127.0.0.1:{server.server_address[1]}/v1",
            "--lang-in", "en", "--lang-out", "zh", "--no-dual",
            "--no-auto-extract-glossary", "--disable-same-text-fallback",
            "--watermark-output-mode", "no_watermark", "--skip-scanned-detection",
        ]
        try:
            completed = subprocess.run(
                command, cwd=ROOT, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
        (destination / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (destination / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
        (destination / "inputs.json").write_text(
            json.dumps(server.inputs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result_match = re.search(r"(?m)^ZTF_RESULT (\{.*\})\s*$", completed.stdout)
        result = json.loads(result_match.group(1)) if result_match else None
        warning_match = re.search(r"(?m)^ZTF_WARNING (\{.*\})\s*$", completed.stdout)
        warning = json.loads(warning_match.group(1)) if warning_match else None
        error_match = re.search(r"(?m)^ZTF_ERROR (\{.*\})\s*$", completed.stdout)
        error = json.loads(error_match.group(1)) if error_match else None
        records = read_job_records(destination / "job.jsonl")
        return completed, result, error, server.inputs, records, server.request_count

    def test_clear_headers_are_translated_and_body_kept(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-5000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-5000:])
        self.assertIsNone(self.error, self.error)

        header_items = [
            item for item in self.inputs
            if item.get("layout_label") == "table_header"
        ]
        header_texts = {str(item.get("input", "")) for item in header_items}
        for expected in (
            "Method", "Precision", "Recall", "F1", "Real-time", "Params", "FPS",
            "Detection Performance", "Efficiency",
        ):
            self.assertIn(expected, header_texts)
        self.assertTrue(all(item.get("table_role") for item in header_items))
        self.assertTrue(all(item.get("table_context") for item in header_items))

        all_inputs = {str(item.get("input", "")) for item in self.inputs}
        for body_value in (
            "ChangeMamba", "SECOND", "Swin", "CenterPoint",
            "0.912", "0.887", "YES", "NO", "33M", "51",
        ):
            self.assertNotIn(body_value, all_inputs, "表体不应送译")
        self.assertFalse(
            [
                item for item in self.inputs
                if (item.get("table_context") or {}).get("table") == "表 3"
            ],
            "不确定表格不应有表头送译",
        )

        warning_match = re.search(r"(?m)^ZTF_WARNING (\{.*\})\s*$", self.completed.stdout)
        self.assertIsNotNone(warning_match, self.completed.stdout[-5000:])
        warning = json.loads(warning_match.group(1))
        self.assertEqual(warning["failed_paragraphs"], 0)
        self.assertEqual(len(warning["tables"]), 1)
        self.assertEqual(warning["tables"][0]["page"], 3)
        self.assertIn("3", warning["tables"][0]["table"])

        analysis_records = [
            record for record in self.records if record.get("event") == "table_analysis"
        ]
        self.assertTrue(analysis_records, self.records)
        statuses = {
            table["table"]: table["status"]
            for table in analysis_records[-1]["tables"]
        }
        self.assertEqual(statuses.get("表 1"), "clear")
        self.assertEqual(statuses.get("表 2"), "clear")
        self.assertEqual(statuses.get("表 3"), "uncertain")
        finished = [
            record for record in self.records if record.get("event") == "job_finished"
        ][-1]
        self.assertEqual(finished["outcome"], "warning")
        self.assertEqual(finished["table_warnings"][0]["page"], 3)

        self.assertEqual(self.request_count, 1, "表格表头应保持在单文档批次中")
        self.assertEqual(self.result["requests"], 1)
        self.assertEqual(self.result["repair_request_count"], 0)

        pages = [_compact(text) for text in read_pdf_pages(Path(self.result["mono"]))]
        self.assertEqual(len(pages), 3)
        for source_label in (
            "Method", "Precision", "Recall", "F1", "Real-time",
            "Params", "FPS", "DetectionPerformance", "Efficiency",
        ):
            self.assertNotIn(source_label, pages[0])
        for source_label in ("Method", "Precision", "Recall", "F1", "Params", "Real-time"):
            self.assertNotIn(source_label, pages[1])
        for source_value in (
            "ChangeMamba", "SECOND", "Swin", "CenterPoint",
            "0.912", "0.887", "0.899", "YES", "NO", "33M", "51",
        ):
            self.assertIn(source_value, pages[0], "表体必须保持原文")
        for source_value in (
            "ChangeMamba", "SECOND", "Swin", "0.91", "0.87", "YES", "NO", "33M",
        ):
            self.assertIn(source_value, pages[1], "表体必须保持原文")
        for source_label in ("Precision", "Recall", "F1", "AP", "mAP", "IoU", "Params", "FPS"):
            self.assertIn(source_label, pages[2], "不确定表格应整表保留")
        self.assertNotIn("Detectionresults", pages[0], "表外题注必须翻译")
        self.assertNotIn("percentages", pages[0], "表外附注必须翻译")
        self.assertNotIn("Ablationstudy", pages[1], "表外题注必须翻译")
        self.assertNotIn("remainingcolumns", pages[1], "表外附注必须翻译")
        self.assertNotIn("Termsused", pages[2], "表外题注必须翻译")
        self.assertTrue(CJK_RE.search(pages[0]))
        self.assertTrue(CJK_RE.search(pages[1]))
        self.assertTrue(CJK_RE.search(pages[2]))

    def test_clear_header_failure_is_not_imported(self) -> None:
        completed, result, error, inputs, records, request_count = self.run_job(
            header_echo=True
        )
        self.assertIsNone(result, "已识别表头仍失败时不得返回 ZTF_RESULT")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNotNone(error, completed.stdout[-5000:])
        self.assertEqual(error.get("kind"), "translation_failed")
        self.assertTrue(error.get("failed_paragraphs", 0) > 0)
        self.assertIn("页", error.get("message", ""))
        header_items = [
            item for item in inputs if item.get("layout_label") == "table_header"
        ]
        self.assertTrue(header_items)
        self.assertGreaterEqual(request_count, 2, "失败表头应进入合并补译")
        failures = [
            record for record in records if record.get("event") == "item_outcome"
        ]
        self.assertTrue(failures)
        self.assertTrue(
            any(
                "未翻译" in str(record.get("reason", ""))
                or "原文" in str(record.get("reason", ""))
                for record in failures
            ),
            failures,
        )
        self.assertFalse(
            any(
                record.get("event") == "job_finished"
                and record.get("outcome") == "success"
                for record in records
            ),
            records,
        )


if __name__ == "__main__":
    unittest.main()
