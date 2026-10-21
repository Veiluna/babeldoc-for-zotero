"""Progress stage regression tests for the runner's local request hooks."""

from __future__ import annotations

import importlib.util
import io
import contextlib
import unittest
from pathlib import Path


RUNNER_PATH = (
    Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class _Usage:
    completion_tokens = 7


class _Choice:
    finish_reason = "stop"


class _Response:
    usage = _Usage()
    choices = [_Choice()]


class _Completions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _Response()


class _Client:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _Completions()})()


class ProgressStageTest(unittest.TestCase):
    def test_request_hooks_run_around_the_single_provider_call(self) -> None:
        events: list[str] = []
        state = {
            "notify_request_start": lambda: events.append("start"),
            "notify_request_finish": lambda: events.append("finish"),
            "request_budget": runner.RequestBudget(None, lambda requests, planned: None),
            "token_param": "max_tokens",
        }
        client = _Client()
        with contextlib.redirect_stdout(io.StringIO()):
            response = runner.create_completion_adapting(
                client,
                state,
                50000,
                request_metadata={"phase": "main", "item_count": 1},
                api_key="",
            )
        self.assertIsInstance(response, _Response)
        self.assertEqual(client.chat.completions.calls, 1)
        self.assertEqual(events, ["start", "finish"])
        self.assertEqual(
            state["request_budget"].requests,
            0,
            "progress hooks must not count extra requests",
        )

    def test_missing_request_hooks_do_not_break_translation(self) -> None:
        state = {
            "request_budget": runner.RequestBudget(None, lambda requests, planned: None),
            "token_param": "max_tokens",
        }
        client = _Client()
        with contextlib.redirect_stdout(io.StringIO()):
            response = runner.create_completion_adapting(
                client,
                state,
                50000,
                request_metadata={"phase": "main", "item_count": 1},
                api_key="",
            )
        self.assertIsInstance(response, _Response)
        self.assertEqual(client.chat.completions.calls, 1)


if __name__ == "__main__":
    unittest.main()
