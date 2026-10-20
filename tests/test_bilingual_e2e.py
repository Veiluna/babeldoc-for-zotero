"""End-to-end bilingual PDF: side-by-side layout and per-side links.

Real BabelDOC 0.6.4 with a mock API; no real translation request is made.
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
import unittest

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
from mock_openai_server import MockOpenAIServer  # noqa: E402
from test_links_e2e import LinkHandler, write_fixture  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "addon/content/python/ztf_babeldoc_runner.py"
ARTIFACTS = ROOT / "build/bilingual"


def run_translation(source: Path):
    server = MockOpenAIServer(("127.0.0.1", 0), LinkHandler)
    server.inputs = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("ZTF_") and "API_KEY" not in key.upper()
    }
    environment.update({
        "ZTF_OPENAI_API_KEY": "mock-test-key",
        "ZTF_JOB_LOG_PATH": str(ARTIFACTS / "job.jsonl"),
        "PYTHONIOENCODING": "utf-8",
    })
    deployed = ARTIFACTS / "deployed"
    deployed.mkdir(exist_ok=True)
    shutil.copyfile(RUNNER, deployed / RUNNER.name)
    for helper in (
        "ztf_inline_formulas.py", "ztf_style_guards.py",
        "ztf_links.py", "ztf_table_headers.py",
    ):
        shutil.copyfile(ROOT / "addon/content/python" / helper, deployed / helper)
    command = [
        sys.executable, str(deployed / RUNNER.name),
        "--files", str(source),
        "--output", str(ARTIFACTS / "output"),
        "--working-dir", str(ARTIFACTS / "work"),
        "--openai", "--openai-model", "mock-model",
        "--openai-base-url", f"http://127.0.0.1:{server.server_address[1]}/v1",
        "--lang-in", "en", "--lang-out", "zh",
        "--no-auto-extract-glossary", "--disable-same-text-fallback",
        "--watermark-output-mode", "no_watermark", "--skip-scanned-detection",
    ]
    try:
        completed = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    (ARTIFACTS / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (ARTIFACTS / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    match = re.search(r"(?m)^ZTF_RESULT (\{.*\})\s*$", completed.stdout)
    return completed, json.loads(match.group(1)) if match else None


class BilingualOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for stale in ("output", "work"):
            shutil.rmtree(ARTIFACTS / stale, ignore_errors=True)
        cls.source = ARTIFACTS / "source.pdf"
        write_fixture(cls.source)
        cls.completed, cls.result = run_translation(cls.source)

    def test_dual_pdf_is_side_by_side_with_source_and_translation(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-5000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-5000:])
        self.assertTrue(self.result.get("mono"), "mono stays available for the mapping")
        dual_path = self.result.get("dual")
        self.assertTrue(dual_path and Path(dual_path).is_file(), dual_path)
        with pymupdf.open(self.source) as source, pymupdf.open(dual_path) as dual:
            self.assertEqual(len(dual), len(source))
            half = source[0].rect.width
            self.assertAlmostEqual(dual[0].rect.width, half * 2, delta=1.0)
            left_text = dual[0].get_text("text", clip=pymupdf.Rect(0, 0, half, dual[0].rect.height))
            right_text = dual[0].get_text(
                "text", clip=pymupdf.Rect(half, 0, dual[0].rect.width, dual[0].rect.height)
            )
            self.assertIn("Section A", left_text)
            self.assertRegex(right_text, "[\u3400-\u9fff]")

    def test_each_side_keeps_its_own_link_targets(self) -> None:
        mapping_path = Path(self.result["dual_links"]["mapping_path"])
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        restored = [item for item in mapping["links"] if item.get("status") == "restored"]
        self.assertEqual(len(restored), 12)
        self.assertEqual(sum(item["side"] == "left" for item in restored), 6)
        self.assertEqual(sum(item["side"] == "right" for item in restored), 6)
        stats = self.result["dual_links"]
        self.assertEqual(stats["source"], 7)
        self.assertEqual(stats["unresolved"], 1, "the deliberately bad destination is not fabricated")

        # The URI anchor text is [40]; check the URI annotations in the PDF itself.
        with pymupdf.open(self.result["dual"]) as dual:
            uri_links = [link for page in dual for link in page.get_links()
                         if link.get("kind") == pymupdf.LINK_URI]
        self.assertGreaterEqual(len(uri_links), 2, "the URI exists on both halves")
        for link in uri_links:
            self.assertEqual(link.get("uri"), "https://example.org/code")
        # Left targets stay on the left half, right targets on the right half.
        half = pymupdf.open(self.source)[0].rect.width
        with pymupdf.open(self.result["dual"]) as dual:
            gotos = [link for page in dual for link in page.get_links()
                     if link.get("kind") in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED)]
            left = [link for link in gotos if link["from"].x1 <= half]
            right = [link for link in gotos if link["from"].x0 >= half]
            # 5 internal GoTo links per side; the sixth restored link is the URI.
            self.assertEqual(len(left), 5)
            self.assertEqual(len(right), 5)
            for link in right:
                self.assertGreaterEqual(link["to"].x, half)


if __name__ == "__main__":
    unittest.main()
