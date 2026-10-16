r"""Pure tests for whole-document request packing and request limits.

Run the unit checks with `python tests/test_min_requests.py`.

End-to-end check with BabelDOC installed:
1. In one PowerShell window, run
   `python tests/mock_openai_server.py --port 8765 --count-file $env:TEMP\ztf-request-count.txt`.
2. In another window, set `ZTF_OPENAI_API_KEY=mock`, `ZTF_MAX_OUTPUT_TOKENS=8192`,
   and `ZTF_MAX_REQUESTS=20`, then invoke the installed environment's python.exe:
   `& '<babeldoc-env>\python.exe' addon/content/python/ztf_babeldoc_runner.py --files '<one-page-searchable.pdf>' --output '<output-dir>' --working-dir '<work-dir>' --openai --openai-model mock-model --openai-base-url http://127.0.0.1:8765/v1 --lang-in en --lang-out zh --no-dual --no-auto-extract-glossary --disable-same-text-fallback --watermark-output-mode no_watermark`.
   The runner reports `ZTF_RESULT`; verify the output PDF exists and the count file
   contains `1` when the page fits one request. Set a smaller output limit or use
   longer text to exercise continuation and zero-prefix splitting. Start the server with
   `--status 429` to verify that only one failing request is made.

These tests deliberately do not import BabelDOC, so the packing, continuation,
no-cache, no-retry, and cap rules run in a regular Python installation.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any
import urllib.error
import urllib.request


RUNNER_PATH = (
    Path(__file__).parents[1]
    / "addon"
    / "content"
    / "python"
    / "ztf_babeldoc_runner.py"
)
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

MOCK_SERVER_PATH = Path(__file__).with_name("mock_openai_server.py")
MOCK_SERVER_SPEC = importlib.util.spec_from_file_location(
    "mock_openai_server", MOCK_SERVER_PATH
)
assert MOCK_SERVER_SPEC is not None and MOCK_SERVER_SPEC.loader is not None
mock_server = importlib.util.module_from_spec(MOCK_SERVER_SPEC)
MOCK_SERVER_SPEC.loader.exec_module(mock_server)


def test_output_estimate_and_minimum_greedy_packing() -> None:
    assert runner.estimate_output_tokens(100) == 180

    source_token_counts = [250, 250, 250, 250]
    packed = runner.pack_items_by_output_budget(
        source_token_counts,
        1000,
        lambda source_tokens: source_tokens,
    )
    assert packed == [[250, 250], [250, 250]]
    assert all(
        sum(runner.estimate_output_tokens(item) for item in batch) <= 900
        for batch in packed
    )

    # Three estimated 660-token items cannot fit two at a time in 900 tokens.
    packed_large = runner.pack_items_by_output_budget(
        [400, 400, 400], 1000, lambda source_tokens: source_tokens
    )
    assert packed_large == [[400], [400], [400]]


def test_preprocess_filter_runs_before_packing() -> None:
    items = [
        {"name": "first", "raw": "short", "prepared": "a" * 200},
        {"name": "vertical", "raw": "vertical text", "prepared": None},
        {"name": "last", "raw": "short", "prepared": "b" * 200},
    ]
    prepare_calls: list[str] = []

    def prepare(item: dict[str, str | None]) -> tuple[str | None, None]:
        prepare_calls.append(str(item["name"]))
        return item["prepared"], None

    eligible, packed = runner.prepare_and_pack_items(
        items,
        1024,
        prepare,
        lambda text: len(text),
    )
    assert prepare_calls == ["first", "vertical", "last"]
    assert [item["name"] for item in eligible] == ["first", "last"]
    assert len(packed) == 1, "skipped paragraphs must not inflate the request plan"
    assert [item["name"] for item in packed[0]] == ["first", "last"]


def test_babeldoc_translation_cache_is_bypassed_between_jobs() -> None:
    class Cache:
        def __init__(self) -> None:
            self.reads = 0
            self.writes = 0

        def get(self, _text: str) -> str:
            self.reads += 1
            return "stale original-text fallback"

        def set(self, _text: str, _value: str) -> None:
            self.writes += 1

    class Translator:
        def __init__(self) -> None:
            self.cache = Cache()
            self.calls = 0

        def do_llm_translate(self, _text: str, _rate_limit_params: None = None) -> str:
            self.calls += 1
            return f"fresh translation {self.calls}"

    translator = Translator()
    first_job = runner.call_translator_without_cache(
        translator, "same input", llm=True
    )
    second_job = runner.call_translator_without_cache(
        translator, "same input", llm=True
    )

    assert [first_job, second_job] == ["fresh translation 1", "fresh translation 2"]
    assert translator.calls == 2
    assert translator.cache.reads == 0
    assert translator.cache.writes == 0


def test_runner_always_disables_extra_llm_calls() -> None:
    options = runner.ensure_runner_options(["runner.py", "--openai"])
    assert "--no-auto-extract-glossary" in options
    assert "--disable-same-text-fallback" in options
    prompt_index = options.index("--custom-system-prompt")
    prompt = options[prompt_index + 1]
    assert "technical terms" in prompt
    assert "first occurrence" in prompt
    assert "placeholders unchanged" in prompt
    assert "JSON array" in prompt
    assert "fragment_part" in prompt

    # An older plugin build passing its own prompt must not drop the fragment rules.
    stale = runner.ensure_runner_options(
        ["runner.py", "--openai", "--custom-system-prompt", "old prompt without fragments"]
    )
    assert stale.count("--custom-system-prompt") == 1
    assert "old prompt without fragments" not in stale
    assert stale[stale.index("--custom-system-prompt") + 1] == runner.CONSISTENCY_SYSTEM_PROMPT


def test_input_heading_inside_document_text_is_not_a_prompt_delimiter() -> None:
    items = [
        {
            "id": 0,
            "input": "The paper includes the literal example ## Here is the input:",
        }
    ]
    prompt = "## Here is the input:\n\n" + json.dumps(
        items, ensure_ascii=False, indent=2
    )

    parsed_prompt = runner._extract_json_array_prompt(prompt)
    assert parsed_prompt is not None
    assert parsed_prompt[1] == items

    payload = {"messages": [{"role": "user", "content": prompt}]}
    assert mock_server._prompt_items(payload) == items


def test_request_once_does_not_retry_errors() -> None:
    budget = runner.RequestBudget(max_requests=20)
    budget.set_planned(1)
    calls = 0

    def fail_with_429() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("HTTP 429 Too Many Requests")

    try:
        runner.request_once(budget, fail_with_429)
    except RuntimeError as error:
        assert "429" in str(error)
    else:
        raise AssertionError("the API failure should be propagated")

    assert calls == 1
    assert budget.requests == 1


def test_truncated_batches_continue_after_complete_prefix() -> None:
    items = [{"id": index, "input": f"paragraph {index}"} for index in range(4)]
    call_sizes: list[int] = []
    observations: list[tuple[list[int], list[int]]] = []
    budget = runner.RequestBudget(max_requests=20)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, str]]) -> tuple[str, str]:
        call_sizes.append(len(batch))
        if len(call_sizes) == 1:
            prefix = [
                {"id": item["id"], "output": "译：" + item["input"]}
                for item in batch[:2]
            ]
            return json.dumps(prefix, ensure_ascii=False)[:-1] + ',{"id":', "length"
        output = [
            {"id": item["id"], "output": "译：" + item["input"]}
            for item in batch
        ]
        return json.dumps(output, ensure_ascii=False), "stop"

    def observe(
        observed_inputs: list[dict[str, str]],
        observed_outputs: list[dict[str, str]],
    ) -> None:
        observations.append(
            (
                [item["id"] for item in observed_inputs],
                [item["id"] for item in observed_outputs],
            )
        )

    outputs = runner.translate_json_items_with_splitting(
        items, fake_request, budget, on_truncation=observe
    )
    assert [item["id"] for item in outputs] == [0, 1, 2, 3]
    assert [item["output"] for item in outputs] == [
        "译：paragraph 0",
        "译：paragraph 1",
        "译：paragraph 2",
        "译：paragraph 3",
    ]
    assert call_sizes == [4, 3], "the final unclosed item and remaining suffix are re-sent"
    assert observations == [([0], [0]), ([1, 2, 3], [1, 2, 3])]
    assert budget.requests == 2
    assert budget.planned == 2


def test_document_queue_uses_minimum_greedy_requests_without_truncation() -> None:
    items = [
        {"id": index, "input": f"p{index}", "source_tokens": 250}
        for index in range(6)
    ]
    pack = lambda pending: runner.pack_items_by_output_budget(
        pending, 1024, lambda item: item["source_tokens"]
    )
    expected = [[0, 1], [2, 3], [4, 5]]
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=3)
    budget.set_planned(len(expected))

    def fake_request(batch: list[dict[str, Any]]) -> tuple[str, str]:
        ids = [item["id"] for item in batch]
        calls.append(ids)
        return json.dumps(
            [{"id": item["id"], "output": "译：" + item["input"]} for item in batch],
            ensure_ascii=False,
        ), "stop"

    outputs = runner.translate_json_items_with_splitting(
        items, fake_request, budget, repack_remaining=pack
    )

    assert len(expected) == 3
    assert calls == expected
    assert [item["id"] for item in outputs] == list(range(6))
    assert budget.requests == len(expected)


def test_truncation_continuation_repacks_the_remaining_queue() -> None:
    items = [
        {"id": index, "input": f"p{index}", "source_tokens": 10}
        for index in range(6)
    ]
    pack = lambda pending: runner.pack_items_by_output_budget(
        pending, 8192, lambda item: item["source_tokens"]
    )
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=2)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, Any]]) -> tuple[str, str]:
        ids = [item["id"] for item in batch]
        calls.append(ids)
        if len(calls) == 1:
            # The third object is the final object in a truncated response and
            # must be resent along with the rest of the queue.
            prefix = [
                {"id": item["id"], "output": "译：" + item["input"]}
                for item in batch[:3]
            ]
            return json.dumps(prefix, ensure_ascii=False), "length"
        return json.dumps(
            [{"id": item["id"], "output": "译：" + item["input"]} for item in batch],
            ensure_ascii=False,
        ), "stop"

    outputs = runner.translate_json_items_with_splitting(
        items, fake_request, budget, repack_remaining=pack
    )

    assert calls == [list(range(6)), [2, 3, 4, 5]]
    assert [item["id"] for item in outputs] == list(range(6))
    assert not any(item.get("_ztf_failed") for item in outputs)
    assert budget.requests == 2


def test_nested_zero_prefix_truncation_halves_the_next_request_only() -> None:
    items = [{"id": index, "input": f"p{index}"} for index in range(7)]
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=4)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, Any]]) -> tuple[str, str]:
        ids = [item["id"] for item in batch]
        calls.append(ids)
        if len(calls) <= 2:
            return "[{\"id\":", "length"
        return json.dumps(
            [{"id": item["id"], "output": "译：" + item["input"]} for item in batch],
            ensure_ascii=False,
        ), "stop"

    outputs = runner.translate_json_items_with_splitting(items, fake_request, budget)

    assert calls == [list(range(7)), [0, 1, 2], [0], [1, 2, 3, 4, 5, 6]]
    assert [item["id"] for item in outputs] == list(range(7))
    assert not any(item.get("_ztf_failed") for item in outputs)
    assert budget.requests == 4


def test_nested_split_requeues_left_suffix_before_unsent_right_side() -> None:
    """The document executor's one queue completes [0..5] in three calls."""
    items = [
        {
            "id": index,
            "input": f"p{index}",
            "source_tokens": 1,
            "paragraph": object(),
            "position": (0, index),
        }
        for index in range(6)
    ]
    pending = items[:]
    calls: list[list[int]] = []
    translations: dict[int, str] = {}
    next_item_limit: int | None = None
    budget = runner.RequestBudget(max_requests=3)
    budget.set_planned(1)

    while pending:
        request_item_limit = next_item_limit
        next_item_limit = None
        packed_batches = runner.pack_items_by_output_budget(
            pending,
            1024,
            lambda item: item["source_tokens"],
            max_items=request_item_limit,
        )
        packed_batch = packed_batches[0]
        pending = pending[len(packed_batch) :]
        json_items = [
            {"id": item["id"], "input": item["input"]}
            for item in packed_batch
        ]
        items_by_id = {item["id"]: item for item in packed_batch}
        deferred_items: list[dict[str, Any]] = []

        def fake_request(batch: list[dict[str, Any]]) -> tuple[str, str]:
            calls.append([item["id"] for item in batch])
            if len(calls) == 1:
                return "[{\"id\":", "length"
            if len(calls) == 2:
                # Item 1 is the final parsed item, so only item 0 is confirmed.
                return json.dumps(
                    [
                        {"id": batch[0]["id"], "output": "译：" + batch[0]["input"]},
                        {"id": batch[1]["id"], "output": "译：" + batch[1]["input"]},
                    ],
                    ensure_ascii=False,
                ), "length"
            return json.dumps(
                [
                    {"id": item["id"], "output": "译：" + item["input"]}
                    for item in batch
                ],
                ensure_ascii=False,
            ), "stop"

        def defer_unconfirmed(
            unresolved: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            results = []
            for item in unresolved:
                deferred_items.append(items_by_id[item["id"]])
                results.append(
                    {
                        "id": item["id"],
                        "output": item["input"],
                        "_ztf_deferred": True,
                    }
                )
            return results

        def halve_next_request(previous_count: int) -> None:
            nonlocal next_item_limit
            next_item_limit = max(1, previous_count // 2)

        outputs = runner.translate_json_items_with_splitting(
            json_items,
            fake_request,
            budget,
            defer_remaining=defer_unconfirmed,
            on_zero_prefix=halve_next_request,
        )
        translations.update(
            {
                output["id"]: output["output"]
                for output in outputs
                if not output.get("_ztf_deferred")
                and not output.get("_ztf_failed")
            }
        )
        pending = runner.merge_document_pending_items(pending, deferred_items)

    assert calls == [list(range(6)), [0, 1, 2], [1, 2, 3, 4, 5]]
    assert translations == {index: "译：p" + str(index) for index in range(6)}
    assert budget.requests == 3
    assert budget.planned == 3


def test_truncated_duplicate_ids_are_not_treated_as_confirmed() -> None:
    items = [
        {"id": 0, "input": "paragraph 0"},
        {"id": 1, "input": "paragraph 1"},
    ]
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=10)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, str]]) -> tuple[str, str]:
        ids = [int(item["id"]) for item in batch]
        calls.append(ids)
        if len(calls) == 1:
            # A duplicate in the unconfirmed final slot must also invalidate
            # the earlier occurrence of that same ID.
            return json.dumps(
                [
                    {"id": 0, "output": "unconfirmed first copy"},
                    {"id": 0, "output": "unconfirmed duplicate"},
                ]
            ), "length"
        return json.dumps(
            [
                {"id": item["id"], "output": "译：" + item["input"]}
                for item in batch
            ],
            ensure_ascii=False,
        ), "stop"

    outputs = runner.translate_json_items_with_splitting(items, fake_request, budget)

    assert calls == [[0, 1], [0], [1]]
    assert [item["output"] for item in outputs] == [
        "译：paragraph 0",
        "译：paragraph 1",
    ]
    assert not any(item.get("_ztf_failed") for item in outputs)


def test_truncated_unicode_escape_is_recognized_and_retried() -> None:
    items = [
        {"id": 0, "input": "paragraph 0"},
        {"id": 1, "input": "paragraph 1"},
    ]
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=10)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, str]]) -> tuple[str, str]:
        calls.append([int(item["id"]) for item in batch])
        if len(calls) == 1:
            return (
                '[{"id":0,"output":"译：paragraph 0"},'
                '{"id":1,"output":"\\u4e',
                "stop",
            )
        return json.dumps(
            [
                {"id": item["id"], "output": "译：" + item["input"]}
                for item in batch
            ],
            ensure_ascii=False,
        ), "stop"

    outputs = runner.translate_json_items_with_splitting(items, fake_request, budget)

    assert calls == [[0, 1], [0], [1]]
    assert [item["output"] for item in outputs] == [
        "译：paragraph 0",
        "译：paragraph 1",
    ]
    assert not any(item.get("_ztf_failed") for item in outputs)


def test_truncated_unclosed_array_retries_its_last_item() -> None:
    items = [{"id": index, "input": f"paragraph {index}"} for index in range(4)]
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=10)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, str]]) -> tuple[str, str]:
        ids = [int(item["id"]) for item in batch]
        calls.append(ids)
        outputs = [
            {"id": item["id"], "output": "译：" + item["input"]}
            for item in batch
        ]
        content = json.dumps(outputs, ensure_ascii=False)
        if len(calls) == 1:
            # Every item object parses, but the array was cut off before `]`.
            return content[:-1], "length"
        return content, "stop"

    outputs = runner.translate_json_items_with_splitting(items, fake_request, budget)

    assert calls == [[0, 1, 2, 3], [3]]
    assert [output["output"] for output in outputs] == [
        "译：paragraph 0",
        "译：paragraph 1",
        "译：paragraph 2",
        "译：paragraph 3",
    ]
    assert not any(output.get("_ztf_failed") for output in outputs)
    assert budget.requests == 2


def test_single_item_still_truncated_after_continuation_keeps_source() -> None:
    items = [{"id": index, "input": f"paragraph {index}"} for index in range(4)]
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=10)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, str]]) -> tuple[str, str]:
        ids = [int(item["id"]) for item in batch]
        calls.append(ids)
        outputs = [
            {"id": item["id"], "output": "译：" + item["input"]}
            for item in batch
        ]
        content = json.dumps(outputs, ensure_ascii=False)
        if len(calls) == 1:
            # All four values parse, but the array never closes.
            return content[:-1], "length"
        # The item object parses, but this one-item response is still cut off.
        return content[:-1], "length"

    outputs = runner.translate_json_items_with_splitting(items, fake_request, budget)

    assert calls == [[0, 1, 2, 3], [3]]
    assert [output["output"] for output in outputs[:3]] == [
        "译：paragraph 0",
        "译：paragraph 1",
        "译：paragraph 2",
    ]
    assert outputs[3]["output"] == "paragraph 3"
    assert outputs[3]["_ztf_failed"]
    assert budget.requests == 2


def test_length_finish_reason_with_closed_array_retries_last_item() -> None:
    item = {"id": 0, "input": "paragraph 0"}
    budget = runner.RequestBudget(max_requests=5)
    budget.set_planned(1)

    output = runner.translate_json_items_with_splitting(
        [item],
        lambda _batch: (
            json.dumps([{"id": 0, "output": "译：paragraph 0"}], ensure_ascii=False),
            "length",
        ),
        budget,
    )

    assert output[0]["output"] == "paragraph 0"
    assert output[0]["_ztf_failed"]
    assert budget.requests == 1

    items = [
        {"id": 0, "input": "paragraph 0"},
        {"id": 1, "input": "paragraph 1"},
    ]
    calls: list[list[int]] = []
    continuation_budget = runner.RequestBudget(max_requests=5)
    continuation_budget.set_planned(1)

    def closed_prefix_then_suffix(
        batch: list[dict[str, str]],
    ) -> tuple[str, str]:
        calls.append([int(item["id"]) for item in batch])
        content = json.dumps(
            [
                {"id": item["id"], "output": "译：" + item["input"]}
                for item in batch
            ],
            ensure_ascii=False,
        )
        return content, "length" if len(calls) == 1 else "stop"

    continued = runner.translate_json_items_with_splitting(
        items, closed_prefix_then_suffix, continuation_budget
    )
    assert calls == [[0, 1], [1]]
    assert [item["output"] for item in continued] == [
        "译：paragraph 0",
        "译：paragraph 1",
    ]
    assert continuation_budget.requests == 2


def test_continuation_repackages_suffix_from_observed_expansion_factor() -> None:
    items = [
        {"id": index, "input": f"source {index}", "source_tokens": 10}
        for index in range(10)
    ]
    calls: list[list[int]] = []
    budget = runner.RequestBudget(max_requests=10)
    budget.set_planned(1)
    expansion_factor = runner.OUTPUT_EXPANSION_FACTOR

    def fake_request(batch: list[dict[str, Any]]) -> tuple[str, str]:
        ids = [int(item["id"]) for item in batch]
        calls.append(ids)
        if len(calls) == 1:
            # Item 1 is the last parsed item, so only item 0 is confirmed.
            prefix = [
                {"id": item["id"], "output": "y" * 100}
                for item in batch[:2]
            ]
            return json.dumps(prefix), "length"
        outputs = [
            {"id": item["id"], "output": "translated " + item["input"]}
            for item in batch
        ]
        return json.dumps(outputs), "stop"

    def observe(
        input_items: list[dict[str, Any]],
        output_items: list[dict[str, Any]],
    ) -> None:
        nonlocal expansion_factor
        input_tokens = sum(item["source_tokens"] for item in input_items)
        output_tokens = sum(len(item["output"]) for item in output_items)
        expansion_factor = runner.update_expansion_factor(
            expansion_factor, input_tokens, output_tokens
        )

    def repack(
        remaining_items: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        return runner.pack_items_by_output_budget(
            remaining_items,
            500,
            lambda item: item["source_tokens"],
            expansion_factor=expansion_factor,
        )

    outputs = runner.translate_json_items_with_splitting(
        items,
        fake_request,
        budget,
        on_truncation=observe,
        repack_remaining=repack,
    )

    assert expansion_factor == 10
    assert calls == [
        list(range(10)),
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
    ]
    assert [item["id"] for item in outputs] == list(range(10))
    assert not any(item.get("_ztf_failed") for item in outputs)
    assert budget.requests == 4
    assert budget.planned == 4


def test_truncated_suffix_repackages_with_unsent_document_items() -> None:
    """A/B/C confirmation leaves D/E, which must pack with unsent F."""
    document_items = [
        {
            "id": index,
            "input": chr(ord("A") + index),
            "source_tokens": 100,
            "paragraph": object(),
            "position": (0, index),
        }
        for index in range(6)
    ]
    initial_batches = runner.pack_items_by_output_budget(
        document_items,
        1024,
        lambda item: item["source_tokens"],
    )
    assert [[item["input"] for item in batch] for batch in initial_batches] == [
        list("ABCDE"),
        ["F"],
    ]

    calls: list[list[str]] = []
    translated: dict[str, str] = {}
    budget = runner.RequestBudget(max_requests=2)
    budget.set_planned(len(initial_batches))
    expansion_factor = runner.OUTPUT_EXPANSION_FACTOR
    pending = initial_batches[1][:]

    current_batch = initial_batches[0]
    current_json = [
        {"id": item["id"], "input": item["input"]} for item in current_batch
    ]
    rich_items_by_id = {item["id"]: item for item in current_batch}
    deferred_items: list[dict[str, Any]] = []

    def first_request(
        batch: list[dict[str, Any]],
    ) -> tuple[str, str]:
        calls.append([item["input"] for item in batch])
        # D is a complete JSON item but is the last one returned, so truncation
        # makes it unconfirmed. Only A/B/C count as translated.
        return json.dumps(
            [
                {"id": item["id"], "output": "Y" * 200}
                for item in batch[:4]
            ]
        ), "length"

    def observe(
        input_items: list[dict[str, Any]],
        output_items: list[dict[str, Any]],
    ) -> None:
        nonlocal expansion_factor
        input_tokens = sum(
            rich_items_by_id[item["id"]]["source_tokens"] for item in input_items
        )
        output_tokens = sum(len(item["output"]) for item in output_items)
        expansion_factor = runner.update_expansion_factor(
            expansion_factor, input_tokens, output_tokens
        )

    def defer(
        remaining: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        outputs = []
        for item in remaining:
            deferred_items.append(rich_items_by_id[item["id"]])
            outputs.append(
                {
                    "id": item["id"],
                    "output": item["input"],
                    "_ztf_deferred": True,
                }
            )
        return outputs

    first_outputs = runner.translate_json_items_with_splitting(
        current_json,
        first_request,
        budget,
        on_truncation=observe,
        defer_remaining=defer,
    )
    assert [
        output["id"] for output in first_outputs if not output.get("_ztf_deferred")
    ] == [0, 1, 2]
    translated.update(
        {
            "ABCDE"[output["id"]]: output["output"]
            for output in first_outputs
            if not output.get("_ztf_deferred")
        }
    )

    pending = runner.merge_document_pending_items(pending, deferred_items)
    next_batches = runner.pack_items_by_output_budget(
        pending,
        1024,
        lambda item: item["source_tokens"],
        expansion_factor=expansion_factor,
    )
    assert [[item["input"] for item in batch] for batch in next_batches] == [
        list("DEF")
    ]
    budget.set_planned(budget.requests + len(next_batches))

    second_batch = next_batches[0]
    second_json = [
        {"id": item["id"], "input": item["input"]} for item in second_batch
    ]

    def second_request(batch: list[dict[str, Any]]) -> tuple[str, str]:
        calls.append([item["input"] for item in batch])
        return json.dumps(
            [
                {"id": item["id"], "output": "译：" + item["input"]}
                for item in batch
            ]
        ), "stop"

    second_outputs = runner.translate_json_items_with_splitting(
        second_json, second_request, budget
    )
    translated.update(
        {"ABCDEF"[output["id"]]: output["output"] for output in second_outputs}
    )

    assert expansion_factor == 2
    assert calls == [list("ABCDE"), list("DEF")]
    assert translated == {
        "A": "Y" * 200,
        "B": "Y" * 200,
        "C": "Y" * 200,
        "D": "译：D",
        "E": "译：E",
        "F": "译：F",
    }
    assert budget.requests == 2
    assert budget.planned == 2


def test_single_item_truncation_is_not_requeued_by_document_executor() -> None:
    item = {"id": 0, "input": "one paragraph"}
    calls = 0
    deferred: list[dict[str, Any]] = []
    budget = runner.RequestBudget(max_requests=5)
    budget.set_planned(1)

    def fake_request(_batch: list[dict[str, Any]]) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return "[{\"id\":", "length"

    def defer(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deferred.extend(items)
        return []

    outputs = runner.translate_json_items_with_splitting(
        [item], fake_request, budget, defer_remaining=defer
    )

    assert calls == 1
    assert not deferred
    assert outputs[0]["output"] == item["input"]
    assert outputs[0]["_ztf_failed"]
    assert budget.requests == 1


def test_observed_output_ratio_updates_future_batch_estimate() -> None:
    factor = runner.update_expansion_factor(
        current_factor=1.6,
        input_tokens=100,
        output_tokens=220,
    )
    assert factor == 2.2
    assert runner.update_expansion_factor(factor, 100, 100) == factor
    remaining = [250, 250, 250]
    packed = runner.pack_items_by_output_budget(
        remaining, 1024, lambda source_tokens: source_tokens,
        expansion_factor=factor,
    )
    assert packed == [[250], [250], [250]]


def test_cut_off_json_is_split_and_single_paragraph_is_bounded() -> None:
    items = [{"id": index, "input": f"p{index}"} for index in range(2)]
    calls = 0
    observed_after_split: list[int] = []
    budget = runner.RequestBudget(max_requests=10)
    budget.set_planned(1)

    def cut_off_once(batch: list[dict[str, str]]) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if len(batch) > 1:
            return "[{\"id\": 0,", "stop"
        return json.dumps([{"id": batch[0]["id"], "output": "ok"}]), "stop"

    outputs = runner.translate_json_items_with_splitting(
        items,
        cut_off_once,
        budget,
        on_truncation=lambda _inputs, output_items: observed_after_split.extend(
            item["id"] for item in output_items
        ),
    )
    assert len(outputs) == 2
    assert calls == 3
    assert budget.requests == 3
    assert budget.planned == 3
    assert observed_after_split == [0, 1]

    single_budget = runner.RequestBudget(max_requests=5)
    single_budget.set_planned(1)
    single_outputs = runner.translate_json_items_with_splitting(
        [items[0]], lambda _batch: ('[{"id":', "length"), single_budget
    )
    assert single_outputs[0]["_ztf_failed"]
    assert single_budget.requests == 1

    malformed_calls = 0
    malformed_budget = runner.RequestBudget(max_requests=5)
    malformed_budget.set_planned(1)

    def malformed_json(_batch: list[dict[str, str]]) -> tuple[str, str]:
        nonlocal malformed_calls
        malformed_calls += 1
        return "[{bad syntax}]", "stop"

    malformed_outputs = runner.translate_json_items_with_splitting(
        items, malformed_json, malformed_budget
    )
    assert all(output["_ztf_failed"] for output in malformed_outputs)
    assert malformed_calls == 1
    assert malformed_budget.requests == 1


def test_empty_and_refused_responses_fail_without_retrying() -> None:
    items = [{"id": index, "input": f"source paragraph {index}"} for index in range(8)]
    for response in [("", "stop"), ("", "content_filter"), ("refused", "stop", "policy")]:
        calls = 0
        budget = runner.RequestBudget(max_requests=10)
        budget.set_planned(1)

        def fake_request(_batch: list[dict[str, str]]) -> tuple[str, str] | tuple[str, str, str]:
            nonlocal calls
            calls += 1
            return response

        output = runner.translate_json_items_with_splitting(items, fake_request, budget)
        assert calls == 1
        assert budget.requests == 1
        assert len(output) == len(items)
        assert all(result["output"] == items[index]["input"] for index, result in enumerate(output))
        assert all(result["_ztf_failed"] for result in output)


def test_bad_leaf_does_not_discard_successful_siblings() -> None:
    items = [{"id": index, "input": f"p{index}"} for index in range(4)]
    calls: list[int] = []
    budget = runner.RequestBudget(max_requests=20)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, str]]) -> tuple[str, str]:
        ids = [item["id"] for item in batch]
        calls.append(len(batch))
        if len(batch) > 1:
            return "[{\"id\":", "length"
        if ids[0] == 0:
            return "[{broken}]", "stop"
        return json.dumps([{"id": ids[0], "output": "译：" + batch[0]["input"]}], ensure_ascii=False), "stop"

    outputs = runner.translate_json_items_with_splitting(items, fake_request, budget)
    assert calls == [4, 2, 1, 3, 1, 2, 1, 1]
    assert outputs[0]["_ztf_failed"]
    assert outputs[0]["output"] == "p0"
    assert outputs[1]["output"] == "译：p1"
    assert outputs[2]["output"] == "译：p2"
    assert outputs[3]["output"] == "译：p3"


def test_partial_output_validation_and_original_composition_restore() -> None:
    items = [{"id": 0, "input": "first"}, {"id": 1, "input": "second"}]
    outputs = runner._align_json_items(items, [{"id": 0, "output": "译：first"}])
    assert outputs[0]["output"] == "译：first"
    assert "_ztf_failed" not in outputs[0]
    assert outputs[1]["output"] == "second"
    assert outputs[1]["_ztf_failed"]

    class Paragraph:
        def __init__(self, unicode: str, composition: list[str]) -> None:
            self.unicode = unicode
            self.pdf_paragraph_composition = composition

    original_composition = ["original composition"]
    failed = Paragraph("source text", original_composition)
    successful = Paragraph("source sibling", ["source sibling composition"])
    snapshots = {
        id(failed): (failed, failed.unicode, failed.pdf_paragraph_composition),
        id(successful): (successful, successful.unicode, successful.pdf_paragraph_composition),
    }
    failed.unicode = ["source text", "source sibling"]
    failed.pdf_paragraph_composition = ["bad mutation"]
    successful.unicode = "译文 sibling"
    runner.restore_paragraph_snapshots(snapshots, {id(failed)})
    assert failed.unicode == "source text"
    assert failed.pdf_paragraph_composition is original_composition
    assert successful.unicode == "译文 sibling"


def test_html_api_error_is_summarized_without_markup() -> None:
    html_error = (
        'HTTP 429: <!DOCTYPE html><html><head><title>Request Limit Exceeded</title></head>'
        '<body><div id="requestId">rq-1792116551</div>'
        '<script>document.getElementById("requestId").textContent=t</script></body></html>'
    )
    summary = runner.sanitize_provider_error(html_error)
    assert summary == "HTTP 429：Request Limit Exceeded（requestId: rq-1792116551）"
    assert "<script" not in summary
    assert "1792116551" in summary

    script_only_id = (
        '<html><head><title>Provider Error</title></head><script>'
        'var r=document.getElementById("requestId").textContent; n("1792116551")'
        "</script></html>"
    )
    script_summary = runner.sanitize_provider_error(script_only_id)
    assert "requestId: 1792116551" in script_summary
    assert "<script" not in script_summary

    encoded_markup = runner.sanitize_provider_error(
        "<html><title>&lt;script&gt;raw&lt;/script&gt;</title></html>"
    )
    assert "<script" not in encoded_markup

    class BodyOnlyHTTPError(RuntimeError):
        status_code = 429
        body = html_error.split("HTTP 429: ", 1)[1]

        def __str__(self) -> str:
            return "upstream request failed"

    body_summary = runner.sanitize_provider_error(BodyOnlyHTTPError())
    assert body_summary == summary, "HTML response bodies must be inspected even if exception text omits them"


def test_fragment_validation_and_placeholder_integrity() -> None:
    source = (
        "While both methods are model agnostic, this paragraph contains many words "
        "that should normally trigger the output length and edit distance checks."
    )
    fragment_result = runner.validate_translation_output(
        source,
        "短译文",
        {"continues_in": "next-fragment"},
        len,
    )
    assert fragment_result["usable"], "short, non-empty fragment translations skip length checks"

    empty_result = runner.validate_translation_output(
        source,
        "",
        {"continues_in": "next-fragment"},
        len,
    )
    assert not empty_result["usable"] and not empty_result["checks"]["non_empty"]

    identical_result = runner.validate_translation_output(source, source, {}, len)
    assert not identical_result["usable"]
    assert identical_result["checks"]["identical_english"]

    placeholder_item = {"required_placeholders": [{"token": "{v1}", "count": 1}]}
    missing = runner.validate_translation_output("formula {v1}", "公式", placeholder_item, len)
    assert not missing["usable"]
    assert missing["checks"]["placeholder_issues"]
    duplicated = runner.validate_translation_output(
        "formula {v1}", "公式{v1}{v1}", placeholder_item, len
    )
    assert not duplicated["usable"]
    assert duplicated["checks"]["placeholder_issues"][0]["actual"] == 2

    active_tokenizer = SimpleNamespace(calc_token_count=lambda value: len(value) + 3)
    assert runner._active_translator_token_counter(
        {"active_llm_translator": active_tokenizer}
    )("text") == 7
    assert runner._active_translator_token_counter({})("text") == 4

    style_pair_a = SimpleNamespace(
        placeholder=None,
        left_placeholder="<style id='span-a'>",
        right_placeholder="</style>",
    )
    style_pair_b = SimpleNamespace(
        placeholder=None,
        left_placeholder="<style id='span-b'>",
        right_placeholder="</style>",
    )
    translate_input = SimpleNamespace(
        original_placeholder_tokens={
            "<style id='span-a'>": 1,
            "<style id='span-b'>": 1,
            "</style>": 2,
        },
        placeholders=[style_pair_a, style_pair_b],
    )
    assert runner._expected_placeholder_counts(translate_input)["</style>"] == 2
    style_item = {
        "continues_in": "next-fragment",
        "required_placeholders": [
            {"token": "<style id='span-a'>", "count": 1},
            {"token": "<style id='span-b'>", "count": 1},
            {"token": "</style>", "count": 2},
        ],
    }
    valid_style_output = runner.validate_translation_output(
        "A fragment with two rich text style spans.",
        "璇戞枃<style id='span-a'>鐢茬敳< / STYLE ><style id='span-b'>涔欎箼</ STYLE >",
        style_item,
        len,
    )
    assert valid_style_output["usable"], valid_style_output

    unsupported_style_output = runner.validate_translation_output(
        "A fragment with a style span.",
        '璇戞枃<style id="span-a">鐢茬敳</style>',
        {
            "continues_in": "next-fragment",
            "required_placeholders": [
                {"token": "<style id='span-a'>", "count": 1},
                {"token": "</style>", "count": 1},
            ],
        },
        len,
    )
    assert not unsupported_style_output["usable"]
    assert any(
        issue.get("reason") == "unsupported_style_opener"
        for issue in unsupported_style_output["checks"]["placeholder_issues"]
    ), "unsupported openers must be rejected before BabelDOC can restore them into PDF text"

    unknown_style = runner.validate_translation_output(
        "A source fragment that should be translated.",
        "中文<style id='unknown'>未配对</style>",
        {"continues_in": "next-fragment"},
        len,
    )
    assert not unknown_style["usable"]
    assert unknown_style["checks"]["placeholder_issues"]

    missing_shared_close = runner.validate_translation_output(
        "A fragment with two style spans.",
        "<style id='span-a'>甲</style><style id='span-b'>乙",
        style_item,
        len,
    )
    assert not missing_shared_close["usable"]
    assert any(
        issue["token"] == "</style>" and issue["actual"] == 1
        for issue in missing_shared_close["checks"]["placeholder_issues"]
    )

    merged_group = [
        {
            "id": 0,
            "input": "Several interesting observations can be made from this first fragment.",
            "fragment_id": "g1",
            "continues_in": "p2",
        },
        {
            "id": 1,
            "input": "Finally, while both methods remain model agnostic, this is the continuation.",
            "fragment_id": "g1",
            "continued_from": "p1",
        },
    ]
    bad_group_outputs = [
        {
            "id": 0,
            "output": mock_server._synthetic_translation(
                " ".join(item["input"] for item in merged_group), 0
            ),
        },
        {"id": 1, "output": ""},
    ]
    assert "g1" in runner._fragment_group_output_issues(
        merged_group, bad_group_outputs, len, {"g1": 2}
    )

    short_fragment_group = [
        {
            "id": 0,
            "input": "A short first.",
            "fragment_id": "short-g",
            "continues_in": "short-p2",
        },
        {
            "id": 1,
            "input": "Short next.",
            "fragment_id": "short-g",
            "continued_from": "short-p1",
        },
    ]
    long_chinese_outputs = [
        "第一段的中文译文较长，并用完整句子说明原文的内容与上下文。" * 8,
        "后续片段的中文译文较长，并用另一组完整句子说明其内容。" * 8,
    ]
    short_group_outputs = [
        {"id": index, "output": output}
        for index, output in enumerate(long_chinese_outputs)
    ]
    for item, output in zip(short_fragment_group, long_chinese_outputs):
        assert runner.validate_translation_output(item["input"], output, item, len)[
            "usable"
        ]
    assert not runner._fragment_group_output_issues(
        short_fragment_group, short_group_outputs, len, {"short-g": 2}
    ), "valid short fragments may have long translations"

    group_outcomes: dict[str, dict[str, tuple[dict[str, Any], dict[str, Any]]]] = {}
    first_batch = [{**merged_group[0], "id": 0}]
    second_batch = [{**merged_group[1], "id": 0}]
    assert not runner._fragment_group_output_issues(
        first_batch,
        [{"id": 0, "output": mock_server._synthetic_translation(first_batch[0]["input"], 0)}],
        len,
        {"g1": 2},
        group_outcomes,
        {"0": "paragraph-a"},
    )
    assert len(group_outcomes["g1"]) == 1
    assert not runner._fragment_group_output_issues(
        second_batch,
        [{"id": 0, "output": mock_server._synthetic_translation(second_batch[0]["input"], 1)}],
        len,
        {"g1": 2},
        group_outcomes,
        {"0": "paragraph-b"},
    ), "a valid linked pair split across requests must not be repaired"
    assert len(group_outcomes["g1"]) == 2

    failed_group_outcomes: dict[str, dict[str, tuple[dict[str, Any], dict[str, Any]]]] = {}
    assert not runner._fragment_group_output_issues(
        first_batch,
        [{"id": 0, "output": mock_server._synthetic_translation(first_batch[0]["input"], 0)}],
        len,
        {"g1": 2},
        failed_group_outcomes,
        {"0": "paragraph-a"},
    )
    empty_sibling_issue = runner._fragment_group_output_issues(
        second_batch,
        [{"id": 0, "output": ""}],
        len,
        {"g1": 2},
        failed_group_outcomes,
        {"0": "paragraph-b"},
    )
    assert "g1" in empty_sibling_issue, "an invalid split sibling must invalidate the whole group"


def test_final_fragment_group_rollback_counts_prior_request_successes() -> None:
    first = SimpleNamespace(
        unicode="first source",
        pdf_paragraph_composition=["first original composition"],
    )
    second = SimpleNamespace(
        unicode="second source",
        pdf_paragraph_composition=["second original composition"],
    )
    first_composition = first.pdf_paragraph_composition
    second_composition = second.pdf_paragraph_composition
    snapshots = {
        id(first): (first, first.unicode, first_composition),
        id(second): (second, second.unicode, second_composition),
    }
    first.unicode = "first translated"
    group_members = [{"paragraph": first}, {"paragraph": second}]
    request_state = {
        "document_paragraph_snapshots": snapshots,
        "translated_paragraph_ids": {id(first)},
    }
    policy = runner.TranslationPolicy()
    policy._record_attempt(first)
    policy._record_attempt(second)

    linked_items = [
        {
            "id": 0,
            "input": "Several observations can be made from the first paragraph fragment.",
            "fragment_id": "split-group",
            "continues_in": "second-box",
        },
        {
            "id": 1,
            "input": "Finally, while both methods remain model agnostic, this is the continuation.",
            "fragment_id": "split-group",
            "continued_from": "first-box",
        },
    ]
    outcomes: dict[str, dict[str, tuple[dict[str, Any], dict[str, Any]]]] = {}
    first_request = [linked_items[0]]
    assert not runner._fragment_group_output_issues(
        first_request,
        [{"id": 0, "output": mock_server._synthetic_translation(first_request[0]["input"])}],
        len,
        {"split-group": 2},
        outcomes,
        {"0": str(id(first))},
    )
    assert request_state["translated_paragraph_ids"] == {id(first)}

    second_request = [linked_items[1]]
    issues = runner._fragment_group_output_issues(
        second_request,
        [{"id": 0, "output": ""}],
        len,
        {"split-group": 2},
        outcomes,
        {"0": str(id(second))},
    )
    assert "split-group" in issues

    restored_ids = runner.rollback_fragment_group(
        policy, request_state, group_members, issues["split-group"]
    )
    assert restored_ids == {id(first), id(second)}
    assert first.unicode == "first source" and second.unicode == "second source"
    assert first.pdf_paragraph_composition is first_composition
    assert second.pdf_paragraph_composition is second_composition
    assert not request_state["translated_paragraph_ids"]
    assert request_state["final_fragment_failure_ids"] == restored_ids
    assert policy.failed_paragraphs == 2
    assert policy.decision()["kind"] == "translation_failed"


def test_references_margins_and_body_font_scale_policy() -> None:
    body = SimpleNamespace(layout_label="text", unicode="A body paragraph")
    heading = SimpleNamespace(layout_label="title", unicode="References")
    reference = SimpleNamespace(layout_label="reference_entry", unicode="[1] Author, title")
    document = SimpleNamespace(
        page=[SimpleNamespace(pdf_paragraph=[body, heading, reference])]
    )
    reference_ids = runner.collect_reference_paragraph_ids(document, False)
    assert id(body) not in reference_ids
    assert id(heading) in reference_ids and id(reference) in reference_ids
    assert runner.collect_reference_paragraph_ids(document, True) == set()

    appendix = SimpleNamespace(
        layout_label="title", unicode="Appendix A: Supplementary results"
    )
    appendix_body = SimpleNamespace(layout_label="text", unicode="Additional results")
    after_references = SimpleNamespace(
        page=[
            SimpleNamespace(
                pdf_paragraph=[
                    SimpleNamespace(layout_label="title", unicode="References"),
                    SimpleNamespace(layout_label="reference_entry", unicode="[1] Author"),
                    appendix,
                    appendix_body,
                ]
            )
        ]
    )
    appendix_reference_ids = runner.collect_reference_paragraph_ids(after_references, False)
    assert id(appendix) not in appendix_reference_ids
    assert id(appendix_body) not in appendix_reference_ids

    page = SimpleNamespace(
        cropbox=SimpleNamespace(box=SimpleNamespace(y=0, y2=1000)),
        mediabox=None,
    )
    header = SimpleNamespace(layout_label="header", unicode="Journal title")
    license_stamp = SimpleNamespace(
        layout_label="text",
        unicode="Authorized licensed use limited to: Example University",
    )
    page_number = SimpleNamespace(layout_label="text", unicode="Page 3 of 10")
    margin_doi = SimpleNamespace(
        layout_label="text",
        unicode="DOI: 10.1000/example",
        box=SimpleNamespace(y=10, y2=20),
    )
    regular_body = SimpleNamespace(
        layout_label="text",
        unicode="A body paragraph with DOI: 10.1000/example",
        box=SimpleNamespace(y=400, y2=430),
    )
    assert runner._paragraph_is_marginal_or_stamp(header, page)
    assert runner._paragraph_is_marginal_or_stamp(license_stamp, page)
    assert runner._paragraph_is_marginal_or_stamp(page_number, page)
    assert runner._paragraph_is_marginal_or_stamp(margin_doi, page)
    assert not runner._paragraph_is_marginal_or_stamp(regular_body, page)

    common, final_scales, overflow_count = runner.body_font_scale_plan([0.94, 0.9, 0.87])
    assert common == 0.87 and final_scales == [0.87, 0.87, 0.87]
    assert overflow_count == 0
    common, final_scales, overflow_count = runner.body_font_scale_plan([0.92, 0.65, 0.84])
    # Only individually overflowing paragraphs (raw optimum below the 0.8 floor)
    # are exempt; all other paragraphs use the class's common floor scale.
    assert common == 0.8 and final_scales == [0.8, 0.65, 0.8]
    assert overflow_count == 1

    inline_formula = SimpleNamespace(
        layout_label="text",
        pdf_paragraph_composition=[
            SimpleNamespace(pdf_formula=None),
            SimpleNamespace(pdf_formula=object()),
        ],
    )
    independent_formula = SimpleNamespace(
        layout_label="text",
        pdf_paragraph_composition=[SimpleNamespace(pdf_formula=object())],
    )
    figure_caption = SimpleNamespace(layout_label="figure_caption")
    table_text = SimpleNamespace(layout_label="table")
    formula_block = SimpleNamespace(layout_label="formula")
    assert runner._is_body_text_paragraph(inline_formula)
    assert not runner._is_independent_formula_paragraph(inline_formula)
    assert runner._is_independent_formula_paragraph(independent_formula)
    assert not runner._is_body_text_paragraph(figure_caption)
    assert not runner._is_body_text_paragraph(table_text)
    assert not runner._is_body_text_paragraph(formula_block)


def test_request_cap_stops_before_an_extra_call() -> None:
    items = [{"id": index, "input": f"p{index}"} for index in range(4)]
    calls = 0
    budget = runner.RequestBudget(max_requests=2)
    budget.set_planned(1)

    def fake_request(batch: list[dict[str, str]]) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if len(batch) > 1:
            return "truncated", "length"
        return json.dumps([{"id": batch[0]["id"], "output": "ok"}]), "stop"

    try:
        runner.translate_json_items_with_splitting(items, fake_request, budget)
    except runner.RequestCapExceeded as error:
        assert "单篇请求上限 2 次" in str(error)
    else:
        raise AssertionError("the configured request cap must stop the batch")

    assert calls == 2
    assert budget.requests == 2


def test_attempt_and_failure_counts_follow_submitted_batches() -> None:
    policy = runner.TranslationPolicy()
    policy.track_pretranslate_attempts = False

    class Paragraph:
        def __init__(self, text: str) -> None:
            self.unicode = text
            self.pdf_paragraph_composition = [text]

    class FakeILTranslator:
        def pre_translate_paragraph(self, paragraph: Paragraph) -> tuple[str, None]:
            return paragraph.unicode, None

        def translate_paragraph(self, paragraph: Paragraph) -> None:
            self.pre_translate_paragraph(paragraph)

    paragraphs = [Paragraph(f"source {index}") for index in range(5)]
    policy.instrument(FakeILTranslator)
    preprocessor = FakeILTranslator()
    for paragraph in paragraphs:
        preprocessor.pre_translate_paragraph(paragraph)
    assert policy.paragraph_attempts == 0, "whole-document preparation is not an API attempt"

    submitted: set[int] = set()
    runner.record_submitted_paragraphs(
        policy, [{"id": 0, "input": "source 0"}], paragraphs, submitted
    )
    snapshots = {
        id(paragraph): (
            paragraph,
            paragraph.unicode,
            paragraph.pdf_paragraph_composition,
        )
        for paragraph in paragraphs
    }
    runner.record_submitted_batch_failures(
        policy, snapshots, submitted, "HTTP 429 Too Many Requests"
    )
    policy.translation_counts = (5, 0, 0)

    assert policy.paragraph_attempts == 1
    assert policy.failed_paragraphs == 1
    summary = policy.count_summary()
    assert summary.startswith("1 ") and " 0 " in summary


def test_request_cap_does_not_count_unsubmitted_paragraphs_as_successes() -> None:
    policy = runner.TranslationPolicy()
    paragraphs = [object() for _ in range(5)]
    submitted: set[int] = set()
    budget = runner.RequestBudget(max_requests=1)

    runner.request_once(
        budget,
        lambda: runner.record_submitted_paragraphs(
            policy,
            [{"id": 0}, {"id": 1}],
            paragraphs,
            submitted,
        ),
    )
    try:
        runner.request_once(
            budget,
            lambda: runner.record_submitted_paragraphs(
                policy,
                [{"id": 2}, {"id": 3}, {"id": 4}],
                paragraphs,
                submitted,
            ),
        )
    except runner.RequestCapExceeded:
        pass
    else:
        raise AssertionError("the request cap should reject the second batch")

    policy.translation_counts = (5, 5, 0)
    assert budget.requests == 1
    assert policy.paragraph_attempts == 2
    assert policy.failed_paragraphs == 0
    summary = policy.count_summary()
    assert summary.startswith("2 ") and " 2 " in summary


def test_mock_server_translation_truncation_and_429_modes() -> None:
    def start_server(status: int):
        server = mock_server.MockOpenAIServer(
            ("127.0.0.1", 0), mock_server.MockOpenAIHandler, status=status
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    payload = {
        "model": "mock-model",
        "max_tokens": 8,
        "messages": [
            {
                "role": "user",
                "content": (
                    "## Here is the input:\n\n"
                    '[{"id":0,"input":"some source text","layout_label":"text"}]'
                ),
            }
        ],
    }
    server, thread = start_server(200)
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        successful_payload = dict(payload, max_tokens=2048)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(successful_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            success = json.loads(response.read())
        success_content = success["choices"][0]["message"]["content"]
        assert success["choices"][0]["finish_reason"] == "stop"
        assert json.loads(success_content) == [{"id": 0, "output": "译：some source text"}]

        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read())
        assert result["choices"][0]["finish_reason"] == "length"
        truncated_content = result["choices"][0]["message"]["content"]
        assert mock_server._token_count(truncated_content) <= 8
        assert truncated_content
        assert server.request_count == 2
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    server, thread = start_server(429)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            assert error.code == 429
        else:
            raise AssertionError("the configured mock should return HTTP 429")
        assert server.request_count == 1
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_mock_fallback_truncation_respects_its_token_estimate() -> None:
    original_tokenizer = mock_server._TOKENIZER
    mock_server._TOKENIZER = None
    try:
        content = json.dumps(
            [{"id": 0, "output": "译：some source text"}],
            ensure_ascii=False,
        )
        truncated = mock_server._truncate_to_tokens(content, 8)
        assert truncated
        assert content.startswith(truncated)
        assert mock_server._token_count(truncated) <= 8
        if len(truncated) < len(content):
            next_character = content[len(truncated)]
            assert mock_server._token_count(truncated + next_character) > 8
    finally:
        mock_server._TOKENIZER = original_tokenizer


if __name__ == "__main__":
    test_output_estimate_and_minimum_greedy_packing()
    test_preprocess_filter_runs_before_packing()
    test_babeldoc_translation_cache_is_bypassed_between_jobs()
    test_runner_always_disables_extra_llm_calls()
    test_input_heading_inside_document_text_is_not_a_prompt_delimiter()
    test_request_once_does_not_retry_errors()
    test_truncated_batches_continue_after_complete_prefix()
    test_document_queue_uses_minimum_greedy_requests_without_truncation()
    test_truncation_continuation_repacks_the_remaining_queue()
    test_nested_zero_prefix_truncation_halves_the_next_request_only()
    test_nested_split_requeues_left_suffix_before_unsent_right_side()
    test_truncated_duplicate_ids_are_not_treated_as_confirmed()
    test_truncated_unicode_escape_is_recognized_and_retried()
    test_truncated_unclosed_array_retries_its_last_item()
    test_single_item_still_truncated_after_continuation_keeps_source()
    test_length_finish_reason_with_closed_array_retries_last_item()
    test_continuation_repackages_suffix_from_observed_expansion_factor()
    test_truncated_suffix_repackages_with_unsent_document_items()
    test_single_item_truncation_is_not_requeued_by_document_executor()
    test_observed_output_ratio_updates_future_batch_estimate()
    test_cut_off_json_is_split_and_single_paragraph_is_bounded()
    test_empty_and_refused_responses_fail_without_retrying()
    test_bad_leaf_does_not_discard_successful_siblings()
    test_partial_output_validation_and_original_composition_restore()
    test_html_api_error_is_summarized_without_markup()
    test_fragment_validation_and_placeholder_integrity()
    test_final_fragment_group_rollback_counts_prior_request_successes()
    test_references_margins_and_body_font_scale_policy()
    test_request_cap_stops_before_an_extra_call()
    test_attempt_and_failure_counts_follow_submitted_batches()
    test_request_cap_does_not_count_unsubmitted_paragraphs_as_successes()
    test_mock_server_translation_truncation_and_429_modes()
    test_mock_fallback_truncation_respects_its_token_estimate()
    print("minimum request regression cases passed")
