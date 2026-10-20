"""End-to-end regressions for local-style repair and citation colour (#16).

Run with the installed BabelDOC 0.6.4 environment:

    python tests/test_style_repair_e2e.py

The fixture is synthetic; the mock API never calls a real provider. Diagnostic
artefacts live under build/style-repair/.
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
    _MOCK_PLACEHOLDER_TOKEN_RE,
    _prompt_items,
    _synthetic_translation,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "addon/content/python/ztf_babeldoc_runner.py"
ARTIFACTS = ROOT / "build/style-repair"
STYLE_OPEN_RE = re.compile(r"<style id='(?P<id>[^']+)'>")
STYLE_TAG_RE = re.compile(r"</?style\b[^>]*>", re.I)
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _translate_run(text: str, seed: int) -> str:
    """Translate one plain-text run, keeping punctuation-only runs intact."""
    if not re.search(r"[A-Za-z]", text):
        return text
    return _synthetic_translation(text, seed)


def translate_placeholders(source: str, seed: int = 0) -> str:
    """Replace each plain run with Chinese while keeping every marker in place."""
    parts: list[str] = []
    position = 0
    for match in _MOCK_PLACEHOLDER_TOKEN_RE.finditer(source):
        parts.append(_translate_run(source[position:match.start()], seed + position))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_translate_run(source[position:], seed + position))
    return "".join(parts)


def drop_style_tags(source: str, seed: int = 0) -> str:
    """Reproduce a model that translates the text but omits all style markup."""
    return _synthetic_translation(_MOCK_PLACEHOLDER_TOKEN_RE.sub("", source), seed)


def swap_span_contents(text: str, first: str, second: str) -> str:
    """Move the contents of two style spans, preserving both tags and counts."""
    pattern_first = re.compile(
        r"(<style id='" + re.escape(first) + r"'>)(.*?)(</style>)", re.S
    )
    pattern_second = re.compile(
        r"(<style id='" + re.escape(second) + r"'>)(.*?)(</style>)", re.S
    )
    first_match = pattern_first.search(text)
    second_match = pattern_second.search(text)
    if not first_match or not second_match:
        raise AssertionError(f"style spans {first!r}/{second!r} missing in {text!r}")
    swapped = text[:first_match.start()] + first_match.group(1) + second_match.group(2) + first_match.group(3) + text[first_match.end():]
    # Re-find the second span after the first replacement (offsets may move).
    second_match = pattern_second.search(swapped)
    assert second_match is not None
    return swapped[:second_match.start()] + second_match.group(1) + first_match.group(2) + second_match.group(3) + swapped[second_match.end():]


def expand_citation_span(text: str, citation_id: str, prefix: str = "所示，") -> str:
    pattern = re.compile(r"(<style id='" + re.escape(citation_id) + r"'>)(.*?)(</style>)", re.S)
    match = pattern.search(text)
    if not match:
        raise AssertionError(f"style span {citation_id!r} missing in {text!r}")
    return text[:match.start()] + match.group(1) + prefix + match.group(2) + match.group(3) + text[match.end():]


class StyleRepairHandler(MockOpenAIHandler):
    """Deterministic model behaviour for the #16 reproduction."""

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        items = _prompt_items(payload)
        self.server.count_request()
        self.server.inputs.extend(items)
        outputs = []
        for index, item in enumerate(items):
            source = str(item.get("input", ""))
            is_repair = bool(item.get("repair_reason"))
            if "This paper studies" in source:
                translated = drop_style_tags(source, index)
            elif "detector" in source:
                if is_repair and getattr(self.server, "citation_repair_broken", False):
                    translated = expand_citation_span(
                        translate_placeholders(source, index), "3"
                    )
                elif is_repair:
                    translated = translate_placeholders(source, index)
                else:
                    translated = swap_span_contents(
                        translate_placeholders(source, index), "3", "5"
                    )
            else:
                translated = translate_placeholders(source, index)
            if is_repair and getattr(self.server, "formula_repair_broken", False):
                # Simulate a model that still loses the formula marker.
                translated = re.sub(r"\{v\d+\}", "", translated)
            outputs.append({"id": item.get("id", index), "output": translated})
        self._send_json(
            200,
            {
                "id": "style-mock",
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


def write_fixture(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 70), "Quality Styles Study", fontname="times-bold",
                     fontfile=r"C:\Windows\Fonts\timesbd.ttf", fontsize=16)
    page.insert_text((60, 100), "Abstract", fontname="times-bold",
                     fontfile=r"C:\Windows\Fonts\timesbd.ttf", fontsize=11)
    lines = [
        [("This paper studies how ", "tiro"), ("local styles", "tiit"), (" survive", "tiro")],
        [("a full document translation and how ", "tiro"), ("restoration", "tibo"), (" behaves", "tiro")],
        [("when a model keeps only part of the markup in the ", "tiro"), ("abstract", "mono"), (" text.", "tiro")],
    ]
    y = 118
    for line in lines:
        x = 60
        for text, font in line:
            kwargs = {"fontname": "tiro" if font == "tiro" else "embedded", "fontsize": 10}
            if font == "tiit":
                kwargs["fontfile"] = r"C:\Windows\Fonts\timesi.ttf"
            elif font == "tibo":
                kwargs["fontfile"] = r"C:\Windows\Fonts\timesbd.ttf"
            elif font == "mono":
                kwargs["fontfile"] = r"C:\Windows\Fonts\cour.ttf"
            page.insert_text((x, y), text, **kwargs)
            x += pymupdf.get_text_length(text, fontname="tiro", fontsize=10)
        y += 14
    page.insert_text((60, 200), "The detector shown in Fig. ", fontname="tiro", fontsize=10)
    x = 60 + pymupdf.get_text_length("The detector shown in Fig. ", fontname="tiro", fontsize=10)
    page.insert_text((x, 200), "6", fontname="tiro", fontsize=10, color=(0, 0, 1))
    x += pymupdf.get_text_length("6", fontname="tiro", fontsize=10)
    page.insert_text((x, 200), " reaches the best accuracy", fontname="tiro", fontsize=10)
    page.insert_text((60, 214), "in our experiments, as reported in ", fontname="tiro", fontsize=10)
    x = 60 + pymupdf.get_text_length("in our experiments, as reported in ", fontname="tiro", fontsize=10)
    page.insert_text((x, 214), "[40]", fontname="tiro", fontsize=10, color=(0, 0, 1))
    x += pymupdf.get_text_length("[40]", fontname="tiro", fontsize=10)
    page.insert_text((x, 214), " by the same group.", fontname="tiro", fontsize=10)
    page.insert_text((60, 260), "The method uses a compact representation for every observation.",
                     fontname="tiro", fontsize=10)
    page.insert_text((60, 274), "We choose a formula n2+1 to describe the overall complexity.",
                     fontname="tiro", fontsize=10)
    doc.save(path)
    doc.close()


def read_pdf_spans(pdf_path: Path) -> list[dict[str, object]]:
    spans = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        spans.append(
                            {
                                "text": span["text"],
                                "color": int(span["color"]),
                                "size": float(span["size"]),
                            }
                        )
    return spans


def read_job_records(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class StyleRepairIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.source = ARTIFACTS / "source.pdf"
        write_fixture(cls.source)
        deployed = ARTIFACTS / "deployed"
        deployed.mkdir(exist_ok=True)
        shutil.copyfile(RUNNER, deployed / RUNNER.name)
        for helper in ("ztf_inline_formulas.py", "ztf_style_guards.py", "ztf_table_headers.py"):
            source = ROOT / "addon/content/python" / helper
            if source.is_file():
                shutil.copyfile(source, deployed / helper)

    def run_job(self, *, citation_repair_broken: bool = False, formula_repair_broken: bool = False):
        server = MockOpenAIServer(("127.0.0.1", 0), StyleRepairHandler)
        server.inputs = []
        server.citation_repair_broken = citation_repair_broken
        server.formula_repair_broken = formula_repair_broken
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        destination = ARTIFACTS / (
            "citation-broken" if citation_repair_broken
            else "formula-broken" if formula_repair_broken
            else "main"
        )
        destination.mkdir(parents=True, exist_ok=True)
        # Keep each reproduction isolated so job.jsonl never mixes runs.
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
                "ZTF_MAX_REQUESTS": "20",
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
            "--files", str(self.source),
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
        error_match = re.search(r"(?m)^ZTF_ERROR (\{.*\})\s*$", completed.stdout)
        error = json.loads(error_match.group(1)) if error_match else None
        return completed, result, error, server.inputs, read_job_records(destination / "job.jsonl")

    def test_abstract_styles_are_simplified_and_citation_colour_is_bounded(self) -> None:
        completed, result, error, inputs, records = self.run_job()
        self.assertEqual(completed.returncode, 0, completed.stderr[-5000:])
        self.assertIsNotNone(result, completed.stdout[-5000:])
        self.assertIsNone(error, error)
        self.assertEqual(result["repair_request_count"], 1, result)

        abstract_inputs = [item for item in inputs if "This paper studies" in item.get("input", "")]
        self.assertTrue(abstract_inputs)
        self.assertGreaterEqual(len(STYLE_TAG_RE.findall(abstract_inputs[0]["input"])), 6)
        repair_items = [item for item in inputs if item.get("repair_reason")]
        abstract_repairs = [item for item in repair_items if "This paper studies" in item.get("input", "")]
        self.assertEqual(len(abstract_repairs), 1, repair_items)
        self.assertNotRegex(
            abstract_repairs[0]["input"],
            r"<style\b",
            "合并补译应把普通局部样式简化为段落正文样式",
        )

        outcomes = [record for record in records if record.get("event") == "item_outcome"]
        main_abstract = [
            record for record in outcomes
            if record.get("phase") == "main" and "This paper" in record.get("preview", "")
        ]
        repair_abstract = [
            record for record in outcomes
            if record.get("phase") == "repair" and "This paper" in record.get("preview", "")
        ]
        self.assertEqual(len(main_abstract), 1, outcomes)
        self.assertEqual(main_abstract[0]["outcome"], "needs_repair")
        self.assertTrue(main_abstract[0]["placeholder_issues"])
        self.assertEqual(len(repair_abstract), 1, outcomes)
        self.assertEqual(repair_abstract[0]["outcome"], "repaired")

        main_body = [
            record for record in outcomes
            if record.get("phase") == "main" and "detector" in record.get("preview", "")
        ]
        repair_body = [
            record for record in outcomes
            if record.get("phase") == "repair" and "detector" in record.get("preview", "")
        ]
        self.assertEqual(len(main_body), 1, outcomes)
        self.assertEqual(main_body[0]["outcome"], "needs_repair")
        self.assertTrue(
            any(
                issue.get("reason") == "citation_span_changed"
                for issue in main_body[0].get("placeholder_issues", [])
            ),
            main_body[0],
        )
        self.assertEqual(len(repair_body), 1, outcomes)
        self.assertEqual(repair_body[0]["outcome"], "repaired")

        self.assertTrue(result.get("mono"))
        spans = read_pdf_spans(Path(result["mono"]))
        blue = [span for span in spans if span["color"] == 0x0000FF]
        self.assertTrue(blue, "引用应保留原来的蓝色")
        for span in blue:
            self.assertFalse(
                CJK_RE.search(str(span["text"])),
                f"蓝色不应覆盖普通译文：{span['text']!r}",
            )
            self.assertIn(str(span["text"]).strip(), {"6", "[40]"}, span)
        compact_text = "".join(_compact(str(span["text"])) for span in spans)
        self.assertNotIn(_compact("This paper studies how"), compact_text)
        self.assertNotIn(_compact("local styles"), compact_text)
        self.assertIn("6", compact_text)
        self.assertIn("40", compact_text)

    def test_citation_boundary_still_broken_after_repair_fails_the_document(self) -> None:
        completed, result, error, _inputs, records = self.run_job(citation_repair_broken=True)
        self.assertIsNone(result, "引用边界仍损坏时不得返回 ZTF_RESULT")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNotNone(error, completed.stdout[-5000:])
        self.assertIn("引用", error.get("message", ""))
        failures = [record for record in records if record.get("event") == "quality_failure"]
        self.assertTrue(failures, records)
        self.assertIn("页", failures[0].get("location", ""))

    def test_formula_placeholder_still_broken_after_repair_fails_the_document(self) -> None:
        completed, result, error, _inputs, records = self.run_job(formula_repair_broken=True)
        self.assertIsNone(result, "公式占位符仍损坏时不得返回 ZTF_RESULT")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNotNone(error, completed.stdout[-5000:])
        self.assertIn("占位符", error.get("message", ""))
        failures = [record for record in records if record.get("event") == "quality_failure"]
        self.assertTrue(failures, records)


if __name__ == "__main__":
    unittest.main()
