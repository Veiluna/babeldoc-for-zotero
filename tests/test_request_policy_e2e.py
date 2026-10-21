"""通过真实进程、PDF、协议及本机 HTTP 边界验证票据 1。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import pymupdf

from mock_openai_server import (
    MockOpenAIHandler, MockOpenAIServer, _prompt_items, _synthetic_translation,
)

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "addon/content/python/ztf_babeldoc_runner.py"


class PolicyHandler(MockOpenAIHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.retry_headers.append(self.headers.get("x-stainless-retry-count"))
        items = _prompt_items(payload)
        self.server.attempts.append((time.monotonic(), payload, items))
        index = len(self.server.attempts) - 1
        action = self.server.actions[min(index, len(self.server.actions) - 1)]
        if isinstance(action, int):
            self._send_json(action, {"error": {"message": "temporary failure", "code": "rate_limit_exceeded"}})
            return
        if action == "adapt":
            self._send_json(400, {"error": {"message": "Unsupported parameter: max_tokens", "param": "max_tokens", "code": "unsupported_parameter"}})
            return
        if action == "limit":
            self._send_json(400, {"error": {"message": "max_tokens must be <= 8192", "param": "max_tokens", "code": "invalid_value"}})
            return
        if action == "balance":
            self._send_json(429, {"error": {"message": "Insufficient balance", "code": "insufficient_quota"}})
            return
        if action == "disconnect":
            self.close_connection = True
            return
        if action == "timeout":
            time.sleep(1)
            self.close_connection = True
            return
        if action == "bad_all":
            outputs = [{"id": item["id"], "output": ""} for item in items]
        else:
            outputs = [
                {"id": item["id"], "output": "" if action == "bad" and i == 0 else item["input"] if action == "echo" and i == 0 else _synthetic_translation(item["input"], i)}
                for i, item in enumerate(items)
            ]
        # 每次只确认一个段落，迫使后续请求超过旧的 20 次上限。
        if action == "prefix" and len(outputs) > 1:
            outputs = outputs[:2]
        result = {
            "id": "policy-mock", "object": "chat.completion", "created": 0,
            "model": "mock-model", "choices": [{"index": 0, "message": {
                "role": "assistant", "content": json.dumps(outputs, ensure_ascii=False),
            }, "finish_reason": "length" if action == "prefix" and len(items) > 1 else "stop"}],
        }
        if action != "no_usage":
            result["usage"] = {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}
        self._send_json(200, result)


class RequestPolicyE2E(unittest.TestCase):
    def run_job(self, actions, *, paragraphs=3, cancel=False, heading=False, short_timeout=False):
        server = MockOpenAIServer(("127.0.0.1", 0), PolicyHandler)
        server.actions, server.attempts = actions, []
        server.retry_headers = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="ztf-policy-") as name:
                directory = Path(name)
                pdf = directory / "synthetic.pdf"
                with pymupdf.open() as doc:
                    for page_index in range((paragraphs + 2) // 3):
                        page = doc.new_page()
                        if heading:
                            page.insert_textbox((72, 120, 520, 180), "Experimental Results", fontsize=18)
                        for row in range(min(3, paragraphs - 3 * page_index)):
                            text = (
                                f"Experiment {page_index * 3 + row + 1} examines reliable document translation. "
                                "The proposed method preserves scientific meaning and evaluates the complete paragraph. "
                                "These observations provide independent evidence for the reported experimental results."
                            )
                            self.assertGreater(page.insert_textbox((72, 200 + row * 150, 520, 320 + row * 150), text, fontsize=11), 0)
                    doc.save(pdf)
                log = directory / "job.jsonl"
                env = {key: value for key, value in os.environ.items() if not key.startswith("ZTF_") and "API_KEY" not in key.upper()}
                env.update({"ZTF_OPENAI_API_KEY": "mock-policy-key", "ZTF_MAX_OUTPUT_TOKENS": "1024",
                            "ZTF_MAX_REQUESTS": "1", "ZTF_REPAIR_FAILED": "0", "ZTF_JOB_LOG_PATH": str(log)})
                command = [sys.executable, str(RUNNER), "--files", str(pdf), "--output", str(directory / "out"),
                           "--working-dir", str(directory / "work"), "--openai", "--openai-model", "mock-model",
                           "--openai-base-url", f"http://127.0.0.1:{server.server_port}/v1", "--lang-in", "en",
                           "--lang-out", "zh", "--no-dual", "--watermark-output-mode", "no_watermark"]
                if short_timeout:
                    # 只缩短外部 HTTP 边界的超时；实际网络与 SDK 异常保持真实。
                    driver = "import openai,runpy,sys; original=openai.OpenAI; openai.OpenAI=lambda **kw: original(**dict(kw,timeout=0.5)); path=sys.argv.pop(1); runpy.run_path(path,run_name='__main__')"
                    command[1:1] = ["-c", driver]
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
                try:
                    if cancel:
                        deadline = time.monotonic() + 90
                        while not server.attempts and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(server.attempts, "进程未发出首个 mock 请求")
                        time.sleep(0.2)
                        started = time.monotonic()
                        process.terminate()
                    stdout, stderr = process.communicate(timeout=180)
                    elapsed = time.monotonic() - started if cancel else None
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                protocol = [(line.split(" ", 1)[0], json.loads(line.split(" ", 1)[1])) for line in stdout.splitlines() if line.startswith(("ZTF_RESULT ", "ZTF_ERROR ", "ZTF_WARNING "))]
                events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
                text = ""
                for kind, value in protocol:
                    if kind == "ZTF_RESULT":
                        with pymupdf.open(value["mono"]) as output:
                            text = "".join(page.get_text() for page in output)
                self.assertTrue(all(value in (None, "0") for value in server.retry_headers), server.retry_headers)
                return process.returncode, protocol, events, list(server.attempts), text, stderr[-2500:], elapsed
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def assert_success(self, job):
        self.assertEqual(job[0], 0, job[5])
        self.assertEqual([kind for kind, _ in job[1]], ["ZTF_RESULT"], job[1])
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in job[4]), job[4])

    def test_fixed_policy_ignores_legacy_environment(self):
        job = self.run_job(["ok"])
        self.assert_success(job)
        self.assertEqual(job[3][0][1].get("max_tokens"), 50000)
        started = next(event for event in job[2] if event["event"] == "job_started")
        self.assertTrue(started["repair_failed"])
        self.assertIsNone(started["max_requests"])

    def test_temporary_errors_retry_twice_and_count_each_network_attempt(self):
        job = self.run_job([503, 503, "ok"])
        self.assert_success(job)
        self.assertEqual(len(job[3]), 3)
        self.assertEqual(job[1][0][1]["requests"], 3)
        self.assertGreaterEqual(job[3][1][0] - job[3][0][0], 1.9)
        self.assertGreaterEqual(job[3][2][0] - job[3][1][0], 3.9)
        requests = [event for event in job[2] if event["event"] == "request"]
        self.assertEqual([event["request_index"] for event in requests], [1, 2, 3])
        self.assertEqual([event["actual_output_tokens"] for event in requests], [None, None, 30])

    def test_partial_translation_after_repair_is_imported_with_warning(self):
        job = self.run_job(["bad"])
        # 补译后仍有一段保留原文；PDF 已产出，因此降级导入而不是丢弃。
        self.assertEqual(job[0], 0, job[5])
        self.assertTrue(any(kind == "ZTF_RESULT" for kind, _ in job[1]), job[1])
        warning = next(value for kind, value in job[1] if kind == "ZTF_WARNING")
        self.assertIs(warning.get("degraded"), True, warning)
        self.assertEqual(len(job[3]), 2, "即使旧偏好关闭补译也必须执行一轮")
        self.assertTrue(warning["failures"])
        self.assertEqual(warning["failures"][0]["page"], 1)
        self.assertGreaterEqual(warning["failures"][0]["paragraph"], 1)
        self.assertTrue(warning["failures"][0]["reason"])
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in job[4]))

    def test_no_translated_paragraph_fails_even_when_babeldoc_emits_a_pdf(self):
        # 全部段落都保留原文时，即便 BabelDOC 仍写出了 PDF，也不得导入：
        # 0 段真正翻译成功属于硬失败边界。
        job = self.run_job(["bad_all", "bad_all"])
        self.assertNotEqual(job[0], 0, job[5])
        self.assertFalse(any(kind == "ZTF_RESULT" for kind, _ in job[1]), job[1])
        if job[1]:
            self.assertEqual(job[1][0][0], "ZTF_ERROR", job[1])

    def test_prose_echo_requires_repair_even_when_other_paragraphs_succeed(self):
        job = self.run_job(["echo", "ok"])
        self.assert_success(job)
        self.assertEqual(len(job[3]), 2)
        self.assertEqual(job[1][0][1]["repair_request_count"], 1)

    def test_identified_heading_echo_requires_translation(self):
        job = self.run_job(["echo", "ok"], heading=True)
        self.assert_success(job)
        self.assertEqual(len(job[3]), 2)

    def test_parameter_adaptation_is_a_separate_counted_attempt_at_50000(self):
        job = self.run_job(["adapt", "ok"])
        self.assert_success(job)
        self.assertEqual(job[3][0][1].get("max_tokens"), 50000)
        self.assertEqual(job[3][1][1].get("max_completion_tokens"), 50000)
        self.assertNotIn("max_tokens", job[3][1][1])
        self.assertEqual(job[1][0][1]["requests"], 2)
        self.assertEqual(len([event for event in job[2] if event["event"] == "request"]), 2)

    def test_exhausted_retries_stop_after_three_attempts(self):
        job = self.run_job([503])
        self.assertNotEqual(job[0], 0)
        self.assertEqual(len(job[3]), 3)
        self.assertEqual([kind for kind, _ in job[1]], ["ZTF_ERROR"])
        self.assertEqual(job[1][0][1]["requests"], 3)

    def test_nonretryable_errors_and_unsupported_50000_stop_immediately(self):
        for action in (401, 403, 404, 422, "balance", "limit"):
            with self.subTest(action=action):
                job = self.run_job([action])
                self.assertNotEqual(job[0], 0)
                self.assertEqual(len(job[3]), 1)
                self.assertEqual([kind for kind, _ in job[1]], ["ZTF_ERROR"])
                self.assertEqual(job[3][0][1].get("max_tokens"), 50000)

    def test_temporary_status_and_connection_errors_recover(self):
        for action in (408, 409, 429, 500, "disconnect", "timeout"):
            with self.subTest(action=action):
                job = self.run_job([action, "ok"], short_timeout=action == "timeout")
                self.assert_success(job)
                self.assertEqual(len(job[3]), 2)
                self.assertEqual(job[1][0][1]["requests"], 2)

    def test_missing_usage_stays_unknown_and_does_not_fail_valid_translation(self):
        job = self.run_job(["no_usage"])
        self.assert_success(job)
        requests = [event for event in job[2] if event["event"] == "request"]
        self.assertEqual(len(requests), 1)
        self.assertIsNone(requests[0]["actual_output_tokens"])

    def test_no_twenty_request_cap_and_no_resending_completed_prefix(self):
        job = self.run_job(["prefix"], paragraphs=24)
        self.assert_success(job)
        self.assertGreater(len(job[3]), 20)
        confirmed = set()
        for _, payload, items in job[3]:
            self.assertEqual(payload.get("max_tokens"), 50000)
            self.assertTrue(confirmed.isdisjoint(item["input"] for item in items))
            confirmed.add(items[0]["input"])
        self.assertEqual(job[1][0][1]["requests"], len(job[3]))

    def test_retry_does_not_resend_a_completed_batch(self):
        job = self.run_job(["prefix", 503, "ok"])
        self.assert_success(job)
        self.assertEqual(len(job[3]), 3)
        first_source = job[3][0][2][0]["input"]
        self.assertNotIn(first_source, [item["input"] for item in job[3][1][2]])
        self.assertEqual(job[3][1][2], job[3][2][2])

    def test_repair_api_failure_reports_paragraph_locations(self):
        # 主批次有一项不可用、补译请求返回 401：降级警告仍须给出页码和
        # 段落，不能只剩服务商异常；已有译文的 PDF 照常导入。
        job = self.run_job(["bad", 401])
        self.assertEqual(job[0], 0, job[5])
        self.assertTrue(any(kind == "ZTF_RESULT" for kind, _ in job[1]), job[1])
        warning = next(value for kind, value in job[1] if kind == "ZTF_WARNING")
        self.assertIs(warning.get("degraded"), True, warning)
        failures = warning.get("failures") or []
        located = [item for item in failures if item.get("page") is not None]
        self.assertTrue(located, warning)
        self.assertEqual(located[0]["page"], 1)
        self.assertGreaterEqual(located[0]["paragraph"], 1)
        self.assertTrue(located[0]["reason"])

    def test_cancel_interrupts_retry_wait_without_another_attempt(self):
        job = self.run_job([503], cancel=True)
        self.assertNotEqual(job[0], 0)
        self.assertLess(job[6], 1.5)
        self.assertEqual(len(job[3]), 1)
        self.assertFalse(any(kind == "ZTF_RESULT" for kind, _ in job[1]))


if __name__ == "__main__":
    unittest.main()
