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
    test_request_cap_stops_before_an_extra_call()
    test_attempt_and_failure_counts_follow_submitted_batches()
    test_request_cap_does_not_count_unsubmitted_paragraphs_as_successes()
    test_mock_server_translation_truncation_and_429_modes()
    test_mock_fallback_truncation_respects_its_token_estimate()
    print("minimum request regression cases passed")
