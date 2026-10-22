"""Integration regression for linked-fragment repair through the real runner.

Run this after the ordinary tests in an environment that has BabelDOC 0.6.4 and
the read-only ``ztest/samples/liu2022/original.pdf`` fixture:

    python tests/test_fragment_repair_e2e.py

Set ``ZTF_FRAGMENT_REPAIR_PDF`` to the Liu PDF path and
``ZTF_BABELDOC_PYTHON`` to the installed BabelDOC environment's Python path
when either is not in one of the standard locations. Missing inputs fail this
explicitly invoked regression with the expected path in the diagnostic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import unicodedata
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from mock_openai_server import MockOpenAIHandler, MockOpenAIServer


def _compact(text: str) -> str:
    """Whitespace-free, NFKC-normalized text for comparing PDF output.

    BabelDOC's embedded CJK font maps some glyphs back to compatibility
    ideographs (e.g. 不 extracts as U+F967); NFKC folds them to the
    unified code points the model actually returned.
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


REPO_ROOT = Path(__file__).parents[1]
RUNNER_PATH = REPO_ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
_DEFAULT_INPUTS = [
    REPO_ROOT / "ztest" / "samples" / "liu2022" / "original.pdf",
    Path(r"G:\ProgramData\zotero-translate-files\ztest\samples\liu2022\original.pdf"),
]
INPUT_PDF = Path(os.environ["ZTF_FRAGMENT_REPAIR_PDF"]) if os.environ.get(
    "ZTF_FRAGMENT_REPAIR_PDF"
) else next((path for path in _DEFAULT_INPUTS if path.is_file()), _DEFAULT_INPUTS[0])
_PYTHON_CANDIDATES = [
    Path(os.environ["ZTF_BABELDOC_PYTHON"])
    if os.environ.get("ZTF_BABELDOC_PYTHON")
    else Path(""),
    REPO_ROOT / "babeldoc-env" / "python.exe",
    REPO_ROOT.parents[2] / "zotero-translate-files" / "babeldoc-env" / "python.exe",
]
BABELDOC_PYTHON = next((path for path in _PYTHON_CANDIDATES if path.is_file()), None)


class LinkedFragmentRepairIntegrationTest(unittest.TestCase):
    def _run_job(
        self,
        *,
        repair_enabled: bool,
        bad_fragment_repairs: bool = False,
        echo_first_fragment: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None, list[dict[str, object]], int, str | None, list[str]]:
        if BABELDOC_PYTHON is None:
            self.fail(
                "BabelDOC Python not found; set ZTF_BABELDOC_PYTHON to the installed environment's python.exe"
            )
        if not INPUT_PDF.is_file():
            self.fail(
                f"Liu fragment-repair PDF not found at {INPUT_PDF}; set ZTF_FRAGMENT_REPAIR_PDF"
            )

        server = MockOpenAIServer(
            ("127.0.0.1", 0),
            MockOpenAIHandler,
            bad_fragments=True,
            bad_fragment_repairs=bad_fragment_repairs,
            echo_first_fragment=echo_first_fragment,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"

        try:
            with tempfile.TemporaryDirectory(prefix="ztf-fragment-repair-") as temp_name:
                temp_dir = Path(temp_name)
                output_dir = temp_dir / "output"
                working_dir = temp_dir / "work"
                log_path = temp_dir / "job.jsonl"
                environment = os.environ.copy()
                environment.update(
                    {
                        "ZTF_OPENAI_API_KEY": "mock-test-key",
                        "ZTF_MAX_OUTPUT_TOKENS": "8192",
                        "ZTF_MAX_REQUESTS": "20",
                        "ZTF_REPAIR_FAILED": "1" if repair_enabled else "0",
                        "ZTF_TRANSLATE_REFERENCES": "0",
                        "ZTF_JOB_LOG_PATH": str(log_path),
                    }
                )
                command = [
                    str(BABELDOC_PYTHON),
                    str(RUNNER_PATH),
                    "--files",
                    str(INPUT_PDF),
                    "--output",
                    str(output_dir),
                    "--working-dir",
                    str(working_dir),
                    "--openai",
                    "--openai-model",
                    "mock-model",
                    "--openai-base-url",
                    endpoint,
                    "--lang-in",
                    "en",
                    "--lang-out",
                    "zh",
                    "--no-dual",
                    "--no-auto-extract-glossary",
                    "--disable-same-text-fallback",
                    "--watermark-output-mode",
                    "no_watermark",
                ]
                completed = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=600,
                    check=False,
                )
                result_match = re.search(
                    r"(?m)^ZTF_RESULT (\{.*\})\s*$", completed.stdout
                )
                result = json.loads(result_match.group(1)) if result_match else None
                records = (
                    [
                        json.loads(line)
                        for line in log_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if log_path.is_file()
                    else []
                )
                text_blocks: list[str] = []
                if result and result.get("mono"):
                    output_pdf = Path(str(result["mono"]))
                    self.assertTrue(output_pdf.is_file(), output_pdf)
                    # ASCII JSON: the child's stdout uses the console code page
                    # (GBK on Chinese Windows), which cannot encode every glyph.
                    # `import fitz` prints a deprecation notice on stdout; use
                    # pymupdf and read only the final line as JSON.
                    extract_script = (
                        "import pymupdf,json,sys; d=pymupdf.open(sys.argv[1]); "
                        "print(json.dumps([b[4] for p in d for b in p.get_text('blocks')]))"
                    )
                    extracted = subprocess.run(
                        [str(BABELDOC_PYTHON), "-c", extract_script, str(output_pdf)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=60,
                        check=True,
                    ).stdout
                    text_blocks = [
                        _compact(block)
                        for block in json.loads(extracted.strip().splitlines()[-1])
                    ]
                return (
                    completed,
                    result,
                    records,
                    server.request_count,
                    server.bad_fragment_group,
                    text_blocks,
                )
        finally:
            server.shutdown()
            server_thread.join(timeout=2)
            server.server_close()

    def test_bad_fragment_response_repairs_both_boxes_in_one_bounded_pass(self) -> None:
        completed, result, records, request_count, selected_group, text_blocks = self._run_job(
            repair_enabled=True
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"runner failed\nstdout:\n{completed.stdout[-5000:]}\nstderr:\n{completed.stderr[-5000:]}",
        )
        self.assertIsNotNone(result, completed.stdout[-5000:])
        self.assertTrue(selected_group)
        self.assertTrue(result.get("mono"))
        main_items = [
            record
            for record in records
            if record.get("event") == "item_outcome"
            and record.get("phase") == "main"
            and record.get("fragment_id") == selected_group
            and record.get("outcome") == "needs_repair"
        ]
        repaired_items = [
            record
            for record in records
            if record.get("event") == "item_outcome"
            and record.get("phase") == "repair"
            and record.get("fragment_id") == selected_group
        ]
        self.assertEqual(len(main_items), 2, main_items)
        self.assertEqual(
            len(repaired_items),
            2,
            {
                "repair_records": [record for record in records if record.get("phase") == "repair"],
                "result": result,
                "main_group": [
                    record
                    for record in records
                    if record.get("phase") == "main"
                    and record.get("fragment_id") == selected_group
                ],
                "stdout": completed.stdout[-1500:],
            },
        )
        self.assertTrue(all(item["outcome"] == "repaired" for item in repaired_items))
        self.assertEqual(result["repair_request_count"], 1)
        self.assertLessEqual(result["requests"], result["main_minimum_requests"] + 1)
        self.assertEqual(request_count, result["requests"])

        repair_outputs = [item.get("output_preview", "") for item in repaired_items]
        self.assertTrue(all(repair_outputs), repaired_items)
        self.assertEqual(len(set(repair_outputs)), 2, repair_outputs)
        matching_blocks = []
        for repaired_output in repair_outputs:
            snippet = _compact(repaired_output[:16])
            matches = [
                index for index, block in enumerate(text_blocks) if snippet in block
            ]
            self.assertTrue(matches, f"repaired output missing from PDF: {snippet}")
            matching_blocks.append(matches[0])
        self.assertNotEqual(
            matching_blocks[0],
            matching_blocks[1],
            "each fragment repair must be rendered in its own text box",
        )
        compact_pdf_text = "".join(text_blocks)
        for main_item in main_items:
            source = _compact(main_item.get("preview", ""))
            if source:
                self.assertNotIn(source, compact_pdf_text)

    def test_malformed_repair_response_still_exports_the_pdf_with_a_warning(self) -> None:
        completed, result, records, request_count, selected_group, text_blocks = self._run_job(
            repair_enabled=True, bad_fragment_repairs=True,
        )
        # 补译仍失败的两段保留原文，但 BabelDOC 已产出 PDF：降级导入并警告。
        self.assertEqual(completed.returncode, 0, completed.stderr[-5000:])
        self.assertIsNotNone(result, completed.stdout[-5000:])
        self.assertTrue(text_blocks)
        repair_items = [record for record in records if record.get("event") == "item_outcome"
                        and record.get("phase") == "repair" and record.get("fragment_id") == selected_group]
        self.assertEqual(len(repair_items), 2, repair_items)
        self.assertTrue(all(item.get("outcome") == "retained_source" for item in repair_items))
        warning_match = re.search(r"(?m)^ZTF_WARNING (\{.*\})\s*$", completed.stdout)
        self.assertIsNotNone(warning_match, completed.stdout[-5000:])
        warning = json.loads(warning_match.group(1))
        self.assertIs(warning.get("degraded"), True, warning)
        self.assertGreaterEqual(warning["failed_paragraphs"], 2)
        self.assertTrue(all(item.get("page") and item.get("paragraph") and item.get("reason") for item in warning["failures"]))
        self.assertEqual(request_count, warning["requests"])
        self.assertEqual(result["requests"], request_count)
        summary = next(record for record in records if record.get("event") == "repair_summary")
        self.assertEqual(summary["repair_request_count"], 1)
        finished = next(record for record in records if record.get("event") == "job_finished")
        self.assertEqual(finished["outcome"], "warning")
        self.assertIs(finished.get("degraded"), True)
        self.assertEqual(warning["failed_paragraphs"], 2)
        self.assertEqual(len(finished["failures"]), 2, finished["failures"])
        self.assertTrue(
            all(item.get("page") and item.get("paragraph") for item in finished["failures"])
        )
        self.assertTrue(
            any(re.search(r"[\u4e00-\u9fff]", block) for block in text_blocks),
            "degraded PDF must still contain translated text",
        )

    def test_legacy_repair_disabled_still_repairs_the_group(self) -> None:
        completed, result, records, request_count, selected_group, text_blocks = self._run_job(
            repair_enabled=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-5000:])
        self.assertIsNotNone(result, completed.stdout[-5000:])
        self.assertEqual(result["repair_request_count"], 1)
        self.assertEqual(request_count, result["requests"])
        repaired = [record for record in records if record.get("event") == "item_outcome"
                    and record.get("phase") == "repair" and record.get("fragment_id") == selected_group]
        self.assertEqual(len(repaired), 2, repaired)
        self.assertTrue(all(item.get("outcome") == "repaired" for item in repaired))
        self.assertTrue(text_blocks)
        # #18/#37：不确定表格的提示为“表头无法识别时报告，表体按内容规则处理
        # （模型回声时保留原文）”；该警告不得代表段落失败。
        for line in completed.stdout.splitlines():
            if not line.startswith("ZTF_WARNING "):
                continue
            warning = json.loads(line[len("ZTF_WARNING "):])
            self.assertEqual(warning.get("failed_paragraphs"), 0, warning)
            self.assertIn("tables", warning, warning)

    def test_untranslated_prose_fragment_is_not_a_protected_name(self) -> None:
        completed, result, records, request_count, selected_group, text_blocks = self._run_job(
            repair_enabled=True, bad_fragment_repairs=True, echo_first_fragment=True,
        )
        # 未翻译的正文片段仍被判失败并保留原文，但 PDF 降级导入。
        self.assertEqual(completed.returncode, 0, completed.stderr[-5000:])
        self.assertIsNotNone(result, completed.stdout[-5000:])
        self.assertTrue(text_blocks)
        main_items = [record for record in records if record.get("event") == "item_outcome"
                      and record.get("phase") == "main" and record.get("fragment_id") == selected_group]
        self.assertFalse(any(item.get("outcome") == "kept_verbatim" for item in main_items), main_items)
        summary = next(record for record in records if record.get("event") == "repair_summary")
        self.assertGreaterEqual(summary["failed_items"], 2, summary)
        self.assertEqual(summary["repair_request_count"], 1)
        warning = json.loads(
            re.search(r"(?m)^ZTF_WARNING (\{.*\})\s*$", completed.stdout).group(1)
        )
        self.assertIs(warning.get("degraded"), True, warning)
        self.assertEqual(warning["failed_paragraphs"], 2)
        self.assertEqual(request_count, warning["requests"])
        self.assertTrue(
            any(re.search(r"[\u4e00-\u9fff]", block) for block in text_blocks),
            "degraded PDF must still contain translated text",
        )


if __name__ == "__main__":
    unittest.main()
