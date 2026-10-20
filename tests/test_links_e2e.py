"""End-to-end link restoration with real BabelDOC and a mock API.

The fixture is synthetic.  It deliberately gives two citations the same
visible digit/sequence and moves the citations during translation so the test
checks the actual annotation rectangles and destinations, not just counts.
Diagnostic artefacts live under build/links/.
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
from mock_openai_server import (
    MockOpenAIHandler,
    MockOpenAIServer,
    _prompt_items,
    _synthetic_translation_in_place,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "addon/content/python/ztf_babeldoc_runner.py"
ARTIFACTS = ROOT / "build/links"
TOKENS = {
    "[46]": {"kind": "goto", "page": 0, "label": "figure"},
    "[47]": {"kind": "goto", "page": 1, "label": "table"},
    "[40]": {"kind": "goto", "page": 1, "label": "reference"},
    "[42]": {"kind": "goto", "page": 1, "label": "equation"},
}
URI = "https://example.org/code"


class LinkHandler(MockOpenAIHandler):
    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        items = _prompt_items(payload)
        self.server.count_request()
        self.server.inputs.extend(items)
        outputs = []
        for index, item in enumerate(items):
            source = str(item.get("input", ""))
            translated = _synthetic_translation_in_place(source, index)
            if any(token in source for token in ("[46]", "[47]", "[40]", "[42]")):
                # Force the citations to move inside the re-typeset paragraph.
                translated = "额外增加的中文前缀" + translated
            outputs.append({"id": item.get("id", index), "output": translated})
        self._send_json(200, {
            "id": "links-mock", "object": "chat.completion", "created": 0,
            "model": "mock-model",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": json.dumps(outputs, ensure_ascii=False),
            }, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def write_fixture(path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    first, second = doc[0], doc[1]
    rects: dict[str, pymupdf.Rect] = {}

    def line(page: pymupdf.Page, y: float, parts: list[tuple[str, str]]) -> None:
        x = 60.0
        for text, token in parts:
            page.insert_text((x, y), text, fontname="tiro", fontsize=10)
            width = pymupdf.get_text_length(text, fontname="tiro", fontsize=10)
            if token:
                rects[token] = pymupdf.Rect(x, y - 9.5, x + width, y + 3.0)
            x += width

    line(first, 100, [("Section A describes the general problem in detail.", "")])
    line(first, 130, [("The detector shown in ", ""), ("[46]", "figure"),
                      (" reaches high accuracy.", "")])
    line(first, 160, [("We compare with the baseline in ", ""), ("[47]", "table"),
                      (" for all datasets.", "")])
    line(first, 190, [("Earlier work ", ""), ("[40]", "ref1"),
                      (" reported the same effect, and ", ""), ("[40]", "ref2"),
                      (" also appears in the appendix.", "")])
    line(first, 220, [("The formulation in ", ""), ("[42]", "eq"),
                      (" can be rewritten as follows.", "")])
    line(first, 250, [("The dataset is released at [48] for reuse.", "")])
    line(first, 280, [("A broken reference ", ""), ("[99]", "bad"),
                      (" points nowhere.", "")])
    for index, text in enumerate((
        "This additional paragraph gives the translation enough material to move the citations.",
        "It contains several ordinary words and numbers such as 2024 and 2025 without links.",
        "The quick brown fox jumps over the lazy dog while the typesetter reflows the page.",
    )):
        line(first, 320 + index * 30, [(text, "")])

    second.insert_text(
        (60, 300), "[40] A. Author, A reference entry for the duplicate citation.",
        fontname="tiro", fontsize=10,
    )
    second.insert_text((60, 330), "[41] B. Author, Another reference entry.",
                       fontname="tiro", fontsize=10)
    second.insert_text((60, 400), "(11)", fontname="tiro", fontsize=12)
    second.insert_text((60, 500), "Table 6. Results for all datasets.",
                       fontname="tiro", fontsize=10)
    second.insert_text((60, 620), "Fig. 6. Pipeline of the detector.",
                       fontname="tiro", fontsize=10)

    first_xref = doc.page_xref(0)
    second_xref = doc.page_xref(1)
    destinations = {
        "figure.6": f"[{first_xref} 0 R /XYZ 60 742 0]",   # top y = 100
        "table.6": f"[{second_xref} 0 R /XYZ 60 512 0]",   # top y = 330
        "bibr:ref40": f"[{second_xref} 0 R /XYZ 60 542 0]",  # top y = 300
        "tag:11": f"[{second_xref} 0 R /XYZ 60 442 0]",    # top y = 400
    }
    doc.xref_set_key(
        doc.pdf_catalog(), "Dests",
        "<< " + " ".join(f"/{name} {value}" for name, value in destinations.items()) + " >>",
    )
    first = doc[0]
    for token, name in (("figure", "figure.6"), ("table", "table.6"),
                        ("ref1", "bibr:ref40"), ("ref2", "bibr:ref40"),
                        ("eq", "tag:11"), ("bad", "missing.dest")):
        first.insert_link({
            "kind": pymupdf.LINK_NAMED, "from": rects[token], "nameddest": name,
        })
    uri_rect = pymupdf.Rect(
        60, 290, 60 + pymupdf.get_text_length("[40]", fontname="tiro", fontsize=10), 303,
    )
    second.insert_link({"kind": pymupdf.LINK_URI, "from": uri_rect, "uri": URI})
    doc.save(path)
    doc.close()


def run_translation(source: Path) -> tuple[subprocess.CompletedProcess, dict | None]:
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
        "--lang-in", "en", "--lang-out", "zh", "--no-dual",
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
    (ARTIFACTS / "inputs.json").write_text(
        json.dumps(server.inputs, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    match = re.search(r"(?m)^ZTF_RESULT (\{.*\})\s*$", completed.stdout)
    return completed, json.loads(match.group(1)) if match else None


class LinkIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for stale in ("output", "work"):
            shutil.rmtree(ARTIFACTS / stale, ignore_errors=True)
        if (ARTIFACTS / "job.jsonl").exists():
            (ARTIFACTS / "job.jsonl").unlink()
        cls.source = ARTIFACTS / "source.pdf"
        write_fixture(cls.source)
        cls.completed, cls.result = run_translation(cls.source)

    def output(self) -> pymupdf.Document:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-5000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-5000:])
        return pymupdf.open(self.result["mono"])

    def mapping(self) -> dict:
        path = Path(self.result["links"]["mapping_path"])
        self.assertTrue(path.is_file(), path)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_result_reports_and_warns_about_the_bad_destination(self) -> None:
        links = self.result["links"]
        self.assertEqual(links["source"], 7)
        self.assertEqual(links["restored"], 6)
        self.assertEqual(links["unresolved"], 1)
        self.assertEqual(links["unsupported"], 0)
        self.assertFalse(links["split_layout"])
        self.assertIn("ZTF_WARNING ", self.completed.stdout)
        self.assertIn("1 个未能在译文中定位", self.completed.stdout)
        log = (ARTIFACTS / "job.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event": "links_restore"', log)
        self.assertIn('"event": "link_restore_failure"', log)
        self.assertIn("named_destination_unresolved", log)

    def test_annotations_sit_on_moved_translated_text_and_keep_targets(self) -> None:
        mapping = self.mapping()
        self.assertTrue(mapping["paragraphs"], "layout mapping for #19 must be kept")
        self.assertEqual(mapping["stats"]["restored"], 6)
        with self.output() as translated:
            output_links = [
                link for page in translated for link in page.get_links()
            ]
            self.assertEqual(len(output_links), 6)
            moved = 0
            for record in mapping["links"]:
                if record["status"] != "restored":
                    continue
                page = translated[record["output"]["page"]]
                link = next(
                    item for item in output_links
                    if item.get("xref") == record["output"]["xref"]
                )
                visible = pymupdf.Rect(link["from"])
                unrotated = visible * page.derotation_matrix
                for actual, expected in zip(
                    (unrotated.x0, unrotated.y0, unrotated.x1, unrotated.y1),
                    record["output"]["from"],
                ):
                    self.assertAlmostEqual(actual, expected, delta=0.6)
                # The clickable area must cover the actual re-typeset token.
                token_rects = page.search_for(record["anchor"]["text"])
                self.assertTrue(token_rects, record["anchor"])
                nearest = min(
                    token_rects,
                    key=lambda rect: abs(rect.x0 - unrotated.x0) + abs(rect.y0 - unrotated.y0),
                )
                self.assertLess(
                    abs(nearest.x0 - unrotated.x0) + abs(nearest.y0 - unrotated.y0),
                    2.0,
                    (record["anchor"], nearest, unrotated),
                )
                source_rect = pymupdf.Rect(record["source_from"])
                if (abs(source_rect.x0 - unrotated.x0) > 4
                        or abs(source_rect.y0 - unrotated.y0) > 4):
                    moved += 1
                if record["kind"] == "uri":
                    self.assertEqual(link["uri"], record["destination"]["uri"])
                else:
                    self.assertEqual(link["page"], record["output"]["target_page"])
                    to = pymupdf.Point(link["to"]) * translated[link["page"]].derotation_matrix
                    for actual, expected in zip(
                        (to.x, to.y), record["output"]["target_to"]
                    ):
                        self.assertAlmostEqual(actual, expected, delta=1.0)
            self.assertGreaterEqual(
                moved, 1, "the fixture must move at least one citation during reflow"
            )

    def test_bad_destination_is_not_fabricated(self) -> None:
        mapping = self.mapping()
        bad = [record for record in mapping["links"]
               if record["anchor"].get("text") == "[99]"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["status"], "unresolved")
        self.assertEqual(bad[0]["reason"], "named_destination_unresolved")
        self.assertEqual(bad[0].get("output"), {})
        with self.output() as translated:
            for record in mapping["links"]:
                if record["status"] != "restored":
                    continue
                xref = record["output"]["xref"]
                self.assertTrue(any(
                    link.get("xref") == xref
                    for page in translated for link in page.get_links()
                ))


if __name__ == "__main__":
    unittest.main()
