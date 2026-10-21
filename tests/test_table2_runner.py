"""Runner-side regression for rotated table header restoration."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pymupdf


RUNNER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python"
    / "ztf_babeldoc_runner.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

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
        page.insert_text(
            (x, BOTTOM), text, fontname="helv", fontsize=SIZE, rotate=90
        )
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


class _FakeCompletions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls: list[dict] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        index = min(len(self.calls) - 1, len(self.contents) - 1)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.contents[index]),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(completion_tokens=42),
        )


class _FakeClient:
    def __init__(self, contents: list[str]) -> None:
        self.completions = _FakeCompletions(contents)
        self.chat = SimpleNamespace(completions=self.completions)


def _request_state(client: _FakeClient) -> dict:
    budget = runner.RequestBudget(None)
    return {
        "token_param": "max_completion_tokens",
        "request_budget": budget,
        "openai_translator": SimpleNamespace(client=client, model="mock-model"),
        "job_log_path": "",
    }


def _contains_cjk_in_region(page: pymupdf.Page, clip: pymupdf.Rect) -> int:
    count = 0
    for block in page.get_text("rawdict", clip=clip)["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    code = ord(char["c"])
                    if 0x3400 <= code <= 0x9FFF:
                        count += 1
    return count


class Table2RunnerTest(unittest.TestCase):
    def test_translates_headers_once_and_reuses_for_dual(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = _make_pdfs(Path(temporary))
            client = _FakeClient([json.dumps(CHINESE, ensure_ascii=False)])
            state = _request_state(client)

            runner._restore_figure_and_table_text(
                str(source), str(output), state, ""
            )

            self.assertEqual(len(client.completions.calls), 1,
                             "one small request per document")
            self.assertEqual(state["request_budget"].requests, 1)
            payload = client.completions.calls[0]["messages"][0]["content"]
            self.assertIn("Network Scans", payload)
            self.assertEqual(
                client.completions.calls[0]["model"], "mock-model"
            )

            clip = pymupdf.Rect(
                SOURCE_WIDTH + MIN_X - 4, 250, SOURCE_WIDTH + 600, 330
            )
            document = pymupdf.open(str(output))
            try:
                self.assertGreaterEqual(
                    _contains_cjk_in_region(document[0], clip), 13
                )
                for original in HEADERS:
                    self.assertFalse(
                        document[0].search_for(original, clip=clip), original
                    )
                for block in document[0].get_text("dict", clip=clip)["blocks"]:
                    for line in block.get("lines", []):
                        text = "".join(
                            span["text"] for span in line["spans"]
                        )
                        if not any(
                            0x3400 <= ord(char) <= 0x9FFF for char in text
                        ):
                            continue
                        direction = line.get("dir") or (1.0, 0.0)
                        self.assertGreater(
                            abs(direction[0]), 0.99, (text, direction)
                        )
            finally:
                document.close()

            dual = Path(temporary) / "dual.pdf"
            shutil.copyfile(output, dual)
            before = len(client.completions.calls)
            runner._restore_figure_and_table_text(
                str(source), str(dual), state, ""
            )
            self.assertEqual(len(client.completions.calls), before)
            audit = pymupdf.open(str(dual))
            try:
                self.assertGreaterEqual(
                    _contains_cjk_in_region(audit[0], clip), 13
                )
            finally:
                audit.close()

    def test_keeps_english_when_translation_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = _make_pdfs(Path(temporary))
            client = _FakeClient(["not a json array"])
            state = _request_state(client)
            runner._restore_figure_and_table_text(
                str(source), str(output), state, ""
            )
            document = pymupdf.open(str(output))
            try:
                self.assertTrue(
                    document[0].search_for(
                        "Network Scans",
                        clip=pymupdf.Rect(SOURCE_WIDTH, 0, 900, 400),
                    )
                )
            finally:
                document.close()
            self.assertEqual(state["request_budget"].requests, 1)


if __name__ == "__main__":
    unittest.main()
