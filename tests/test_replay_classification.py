"""Replay regression: which paragraphs are translated and which keep the original.

Runs the real runner with BabelDOC 0.6.4 against the local mock endpoint for
fixed sample papers, and compares each paragraph's final decision
("translated" = model output written back, "kept" = original glyphs and
layout) with the committed baseline in tests/fixtures/replay/. No real API
requests are made.

    python tests/test_replay_classification.py

Set ZTF_UPDATE_REPLAY_BASELINE=1 to rewrite the baselines after an intended
change. ZTF_BABELDOC_PYTHON overrides the BabelDOC environment's python.exe and
ZTF_REPLAY_SAMPLES the directory holding the (uncommitted) sample PDFs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from mock_openai_server import MockOpenAIHandler, MockOpenAIServer


REPO_ROOT = Path(__file__).parents[1]
RUNNER_PATH = REPO_ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
BASELINE_DIR = Path(__file__).parent / "fixtures" / "replay"
SAMPLES_DIR = Path(
    os.environ.get("ZTF_REPLAY_SAMPLES")
    or r"G:\ProgramData\zotero-translate-files\ztest\samples"
)
_PYTHON_CANDIDATES = [
    Path(os.environ["ZTF_BABELDOC_PYTHON"]) if os.environ.get("ZTF_BABELDOC_PYTHON") else Path(""),
    REPO_ROOT.parents[2] / "zotero-translate-files" / "babeldoc-env" / "python.exe",
]
BABELDOC_PYTHON = next((path for path in _PYTHON_CANDIDATES if path.is_file()), None)
UPDATE_BASELINE = os.environ.get("ZTF_UPDATE_REPLAY_BASELINE") == "1"
BASELINE_PREVIEW_CHARS = 24

# Models return numeric table cells (counts, percentages) unchanged; the mock
# echoes every input without Latin letters, so the replay covers echoed paragraphs.
ECHO_PATTERN = r"^[^A-Za-z]+$"

SAMPLES = {
    "liu2022": SAMPLES_DIR / "liu2022" / "original.pdf",
    "p01": SAMPLES_DIR / "P01.pdf",
}


def run_replay(pdf: Path) -> tuple[list[dict[str, object]], int, int]:
    server = MockOpenAIServer(
        ("127.0.0.1", 0),
        MockOpenAIHandler,
        echo_pattern=ECHO_PATTERN,
        synthetic=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        with tempfile.TemporaryDirectory(prefix="ztf-replay-") as temp_name:
            temp_dir = Path(temp_name)
            log_path = temp_dir / "job.jsonl"
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
                    "ZTF_JOB_LOG_PATH": str(log_path),
                }
            )
            command = [
                str(BABELDOC_PYTHON),
                str(RUNNER_PATH),
                "--files", str(pdf),
                "--output", str(temp_dir / "output"),
                "--working-dir", str(temp_dir / "work"),
                "--openai",
                "--openai-model", "mock-model",
                "--openai-base-url", endpoint,
                "--lang-in", "en",
                "--lang-out", "zh",
                "--no-dual",
                "--no-auto-extract-glossary",
                "--disable-same-text-fallback",
                "--watermark-output-mode", "no_watermark",
            ]
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                check=False,
            )
            if completed.returncode != 0:
                raise AssertionError(
                    f"runner exited {completed.returncode}:\n{completed.stderr[-3000:]}"
                )
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
    finally:
        server.shutdown()
        server.server_close()
    classifications = [event for event in events if event.get("event") == "paragraph_classification"]
    if len(classifications) != 1:
        raise AssertionError(f"expected one paragraph_classification event, got {len(classifications)}")
    return classifications[0]["paragraphs"], server.request_count, server.echo_count


def describe_differences(expected: list[dict], actual: list[dict]) -> str:
    expected_by_key = {(row["page"], row["index"]): row for row in expected}
    actual_by_key = {(row["page"], row["index"]): row for row in actual}
    lines = []
    for key in sorted(set(expected_by_key) | set(actual_by_key)):
        before, after = expected_by_key.get(key), actual_by_key.get(key)
        if before == after:
            continue
        label = f"p{key[0]}#{key[1]}"
        if before is None:
            lines.append(f"+ {label} {after['decision']}: {after['preview']}")
        elif after is None:
            lines.append(f"- {label} {before['decision']}: {before['preview']}")
        else:
            lines.append(
                f"~ {label} {before['decision']} -> {after['decision']}: "
                f"{before['preview']!r} -> {after['preview']!r}"
            )
    return "\n".join(lines)


class ReplayClassificationTest(unittest.TestCase):
    def check_sample(self, name: str) -> list[dict]:
        if BABELDOC_PYTHON is None:
            self.fail("BabelDOC Python not found; set ZTF_BABELDOC_PYTHON to the environment's python.exe")
        pdf = SAMPLES[name]
        if not pdf.is_file():
            self.fail(f"sample PDF not found at {pdf}; set ZTF_REPLAY_SAMPLES")

        paragraphs, request_count, echo_count = run_replay(pdf)
        # Baselines keep only a short prefix of each paragraph: enough to read
        # a diff, without committing the sample papers' text.
        actual = [{**row, "preview": row["preview"][:BASELINE_PREVIEW_CHARS]} for row in paragraphs]
        print(f"{name}: {request_count} requests, {echo_count} echoed inputs", file=sys.stderr)
        self.assertGreater(request_count, 0, "the replay must go through the mock endpoint")
        baseline_path = BASELINE_DIR / f"{name}.json"
        if UPDATE_BASELINE:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(actual, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        expected = json.loads(baseline_path.read_text(encoding="utf-8"))
        differences = describe_differences(expected, actual)
        self.assertFalse(
            differences,
            f"{name}: paragraph decisions differ from {baseline_path.name}:\n{differences}",
        )
        return actual

    def test_liu2022(self) -> None:
        paragraphs = self.check_sample("liu2022")
        # 0.2.6 rule: the first-page author block keeps its original layout.
        author_block = [
            row for row in paragraphs
            if row["page"] == 1 and "Liu" in row["preview"] and row["index"] < 6
        ]
        self.assertTrue(author_block, "Liu 2022 author block not found on page 1")
        self.assertTrue(all(row["decision"] == "kept" for row in author_block), author_block)

    def test_p01(self) -> None:
        self.check_sample("p01")


if __name__ == "__main__":
    unittest.main()
