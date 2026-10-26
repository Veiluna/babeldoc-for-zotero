"""用 mock 子进程验证端到端驱动的 JSONL 日志、英文报告字段及错误脱敏。"""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import run_split_mock_e2e as driver


class SplitMockDriverTests(unittest.TestCase):
    def setUp(self):
        temporary_root = driver.ROOT / "build/s7/unit"
        temporary_root.mkdir(parents=True, exist_ok=True)
        # Windows 沙箱令牌不兼容 Python 3.12 的 0700 专用 ACL，沿用父目录 ACL。
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode: mkdir(path, 0o777)):
            self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.pdf"
        self.source.write_bytes(b"synthetic PDF")
        self.stdout = io.StringIO()

    def assert_english_keys(self, value):
        if isinstance(value, dict):
            for key, child in value.items():
                self.assertTrue(key.isascii(), key)
                self.assert_english_keys(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_english_keys(child)

    def logs(self):
        lines = self.stdout.getvalue().splitlines()
        self.assertTrue(lines)
        records = [json.loads(line) for line in lines]
        for record in records:
            self.assertTrue({"event", "reason", "count"} <= record.keys(), record)
            self.assert_english_keys(record)
        return records

    def test_missing_outputs_are_reported_as_jsonl_with_english_result_keys(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        with patch.object(driver.sys, "stdout", self.stdout), \
                patch.object(driver.subprocess, "Popen", return_value=process):
            result = driver.run_case(Path(sys.executable), "P05", self.source,
                                     "http://127.0.0.1:1/v1", {},
                                     SimpleNamespace(request_count=0), 30, self.root)
        self.assertEqual(result["status"], "FAIL")
        self.assert_english_keys(result)
        records = self.logs()
        self.assertTrue(any(r["reason"] == "missing_unique_outputs" for r in records))
        saved = json.loads((self.root / "P05/result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, result)

    def test_case_exception_and_missing_sample_never_expose_exception_text(self):
        self.stdout.reconfigure = Mock()
        server = Mock(server_port=1234, request_count=0)
        server_module = SimpleNamespace(MockOpenAIHandler=object(),
                                        MockOpenAIServer=Mock(return_value=server))
        thread = Mock()
        thread.is_alive.return_value = False
        secret = "sk-sensitive-token PRIVATE DOCUMENT CONTENT " * 30
        with patch.object(driver.sys, "stdout", self.stdout), \
                patch.object(driver.sys, "argv", ["driver", "--python", sys.executable]), \
                patch.object(driver, "OUTPUT", self.root / "mock"), \
                patch.object(driver, "isolated_environment", return_value={"TIKTOKEN_CACHE_DIR": "unused"}), \
                patch.object(driver, "find_samples", return_value=[("P05", self.source)]), \
                patch.dict(sys.modules, {"mock_openai_server": server_module}), \
                patch.object(driver.threading, "Thread", return_value=thread), \
                patch.object(driver.subprocess, "Popen", side_effect=RuntimeError(secret)), \
                patch.dict(os.environ):
            exit_code = driver.main()
        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, self.stdout.getvalue())
        summary = json.loads((self.root / "mock/latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("sk-sensitive-token", json.dumps(summary))
        self.assert_english_keys(summary)
        self.assertEqual(summary["cases"][0]["error"], "RuntimeError")
        records = self.logs()
        skipped = next(r for r in records if r["event"] == "sample_skipped")
        self.assertEqual((skipped["reason"], skipped["count"]), ("sample_not_found", 1))
        self.assertTrue(any(r["event"] == "case_failed" and r["reason"] == "RuntimeError"
                            for r in records))
        self.assertTrue(summary["mock_closed"])

    def test_quality_report_is_consumed_without_logging_evidence_or_raw_diagnostics(self):
        server = SimpleNamespace(request_count=0)
        process = Mock(returncode=0)
        process.poll.return_value = 0
        evidence = "全文内容仅供质量报告 " * 30
        report = {"source": "source.pdf", "dual": "dual.pdf", "mono": "mono.pdf",
                  "checks": [{"id": "page_count", "name": "页数", "status": "PASS",
                              "evidence": {"detail": evidence}}], "status": "PASS"}

        def launch(*args, **kwargs):
            directory = kwargs["cwd"]
            (directory / "out/test.mono.pdf").touch()
            (directory / "out/test.dual.pdf").touch()
            (directory / "runner.jsonl").write_text(
                json.dumps({"event": "split_composed", "count": 1}) + "\n", encoding="utf-8")
            server.request_count = 2
            return process

        completed = subprocess.CompletedProcess([], 0, json.dumps(report), "sk-private-token " + evidence)
        with patch.object(driver.sys, "stdout", self.stdout), \
                patch.object(driver.subprocess, "Popen", side_effect=launch) as popen, \
                patch.object(driver.subprocess, "run", return_value=completed):
            result = driver.run_case(Path(sys.executable), "P05", self.source,
                                     "http://127.0.0.1:1/v1", {}, server, 30, self.root)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["checks"], [{"id": "page_count", "status": "PASS"}])
        self.assertEqual(result["mock_requests"], 2)
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assert_english_keys(result)
        self.logs()
        self.assertNotIn(evidence, self.stdout.getvalue())
        directory = self.root / "P05"
        self.assertEqual(json.loads((directory / "quality.json").read_text(encoding="utf-8")), report)
        for path in directory.glob("*.jsonl"):
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn("sk-private-token", contents)
            self.assertNotIn(evidence, contents)
            for line in contents.splitlines():
                self.assert_english_keys(json.loads(line))
        self.assertFalse(list(directory.glob("*.log")))

    def test_timeout_has_structured_reason_and_failure_result(self):
        process = Mock(returncode=1)
        process.poll.side_effect = [None, None, 0]
        process.wait.side_effect = [subprocess.TimeoutExpired(["runner"], 30), 1]
        with patch.object(driver.sys, "stdout", self.stdout), \
                patch.object(driver.subprocess, "Popen", return_value=process), \
                patch.object(driver.time, "monotonic", side_effect=[0, 2, 3, 4]):
            result = driver.run_case(Path(sys.executable), "P05", self.source,
                                     "http://127.0.0.1:1/v1", {},
                                     SimpleNamespace(request_count=0), 1, self.root)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any(r["event"] == "case_timeout" and r["reason"] == "deadline_exceeded"
                            for r in self.logs()))

    def test_setup_exception_is_type_only_in_log_and_summary(self):
        self.stdout.reconfigure = Mock()
        secret = "sk-private-token private file content"
        with patch.object(driver.sys, "stdout", self.stdout), \
                patch.object(driver.sys, "argv", ["driver", "--python", sys.executable]), \
                patch.object(driver, "OUTPUT", self.root / "mock"), \
                patch.object(driver, "isolated_environment", side_effect=OSError(secret)):
            exit_code = driver.main()
        self.assertEqual(exit_code, 1)
        summary = json.loads((self.root / "mock/latest.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["error"], "OSError")
        self.assert_english_keys(summary)
        self.assertNotIn(secret, json.dumps(summary))
        self.assertNotIn(secret, self.stdout.getvalue())
        self.assertTrue(any(r["event"] == "run_failed" and r["reason"] == "OSError"
                            for r in self.logs()))

    def test_invalid_quality_stdout_is_not_saved_as_a_report(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        secret = "sk-private-token private file content"

        def launch(*args, **kwargs):
            out = kwargs["cwd"] / "out"
            (out / "test.mono.pdf").touch()
            (out / "test.dual.pdf").touch()
            return process

        completed = subprocess.CompletedProcess([], 1, secret, secret)
        with patch.object(driver.sys, "stdout", self.stdout), \
                patch.object(driver.subprocess, "Popen", side_effect=launch), \
                patch.object(driver.subprocess, "run", return_value=completed):
            with self.assertRaises(json.JSONDecodeError):
                driver.run_case(Path(sys.executable), "P05", self.source,
                                "http://127.0.0.1:1/v1", {},
                                SimpleNamespace(request_count=1), 30, self.root)
        self.assertFalse((self.root / "P05/quality.json").exists())
        for path in (self.root / "P05").glob("*.jsonl"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
