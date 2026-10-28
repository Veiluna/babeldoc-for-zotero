"""Regression: fragment-group validation must retain per-item style failures.

Run with: python tests/test_fragment_group_split.py
Only the external BabelDOC classes are faked. Main validation, repair records,
and the single packed repair pass are the installed runner implementations.
All model responses come from the repository's local MockOpenAIServer.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load(
    "ztf_runner_fragment_group_regression",
    ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py",
)
# The mock's stdlib fallback is sufficient and makes the fixture independent
# of installed tokenizers and their on-disk caches.
with mock.patch.dict(sys.modules, {"tiktoken": None}):
    mock_server = _load(
        "ztf_mock_fragment_group_regression", ROOT / "tests" / "mock_openai_server.py"
    )


class _TranslateInput:
    def __init__(self, unicode, placeholders, base_style):
        self.unicode = unicode
        self.placeholders = placeholders
        self.base_style = base_style
        self.original_placeholder_tokens = {}

    def set_original_placeholder_tokens(self, tokens):
        self.original_placeholder_tokens = tokens


def _unused(*args, **kwargs):
    return None


def _fake_modules():
    def apply(self, paragraph, tracker, translate_input, translated_text):
        paragraph.unicode = translated_text
        return True

    il_translator = type("ILTranslator", (), {
        "post_translate_paragraph": apply,
        "pre_translate_paragraph": _unused,
    })
    openai_translator = type("OpenAITranslator", (), {})
    attributes_by_module = {
        "openai": {"OpenAI": _unused},
        "babeldoc.format.pdf.document_il.midend.il_translator": {
            "ILTranslator": il_translator,
        },
        "babeldoc.format.pdf.document_il.midend.il_translator_llm_only": {
            "ILTranslatorLLMOnly": type("ILTranslatorLLMOnly", (), {}),
        },
        "babeldoc.format.pdf.document_il.midend.layout_parser": {
            "LayoutParser": type("LayoutParser", (), {
                "generate_fallback_line_layout_for_page": _unused,
            }),
        },
        "babeldoc.format.pdf.document_il.midend.typesetting": {
            "Typesetting": type("Typesetting", (), {
                name: _unused for name in (
                    "_layout_typesetting_units", "_get_optimal_scale",
                    "preprocess_document", "typesetting_document", "render_page",
                    "render_paragraph",
                )
            }),
        },
        "babeldoc.format.pdf.document_il.midend.styles_and_formulas": {
            "StylesAndFormulas": type("StylesAndFormulas", (), {
                "_classify_characters_in_composition": _unused,
            }),
        },
        "babeldoc.translator.translator": {"OpenAITranslator": openai_translator},
    }
    modules = {}
    for name, attributes in attributes_by_module.items():
        parts = name.split(".")
        for end in range(1, len(parts) + 1):
            fullname = ".".join(parts[:end])
            module = modules.setdefault(fullname, types.ModuleType(fullname))
            module.__path__ = []
            if end > 1:
                setattr(modules[".".join(parts[:end - 1])], parts[end - 1], module)
        vars(module).update(attributes)
    return modules, openai_translator, il_translator


class FragmentGroupStyleRepairTests(unittest.TestCase):
    def _run_main_and_repair(
        self, grouped, *, repair_fails=False, split_mode="ok", groups=1,
        max_requests=10, apply_failure=None,
    ):
        source = "The measured <style id='1'>local pattern</style> changes when the input grows"
        sibling_source = "and the measured result remains stable across repeated trials."
        base_style = types.SimpleNamespace(font_size=10)
        placeholder = types.SimpleNamespace(
            left_placeholder="<style id='1'>", right_placeholder="</style>",
            composition=types.SimpleNamespace(
                pdf_character=[types.SimpleNamespace(char_unicode=c) for c in "local pattern"],
                pdf_style=types.SimpleNamespace(graphic_state=types.SimpleNamespace(
                    passthrough_per_char_instruction="0 g",
                )),
            ),
        )
        paragraphs = [types.SimpleNamespace(
            unicode=text, layout_label="text", pdf_style=base_style,
            pdf_paragraph_composition=[],
        ) for text in (source, sibling_source) * groups]
        items = []
        for index, paragraph in enumerate(paragraphs):
            translate_input = _TranslateInput(
                paragraph.unicode, [placeholder] if index % 2 == 0 else [], base_style
            )
            items.append({
                "paragraph": paragraph,
                "prepared_result": (paragraph.unicode, translate_input),
                "tracker": None,
                "fragment_type": "column" if grouped else None,
                "fragment_group": f"fixture-group-{index // 2}" if grouped else None,
                "fragment_index": index % 2 if grouped else None,
                "fragment_sibling_paragraph": id(paragraphs[index ^ 1]) if grouped else None,
            })
        state = {
            "prepared_paragraphs": paragraphs,
            "current_packed_items": items,
            "all_prepared_items": items,
            "fragment_group_members": {
                f"fixture-group-{i}": items[2 * i:2 * i + 2] for i in range(groups)
            } if grouped else {},
            "document_paragraph_snapshots": {
                id(p): (p, p.unicode, p.pdf_paragraph_composition) for p in paragraphs
            },
        }
        modules, openai_class, il_class = _fake_modules()
        document_translator = types.SimpleNamespace(
            calc_token_count=mock_server._token_count, il_translator=il_class(),
        )
        state["active_llm_translator"] = document_translator
        budget = runner.RequestBudget(max_requests, _unused)
        budget.set_planned(1)
        logs, requests, main_outputs = [], [], []
        server = mock_server.MockOpenAIServer(
            ("127.0.0.1", 0), mock_server.MockOpenAIHandler, synthetic=True,
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        original_synthetic = mock_server._synthetic_translation_in_place

        def lose_ordinary_style(text, seed=0):
            translated = original_synthetic(text, seed)
            if server.request_count == 1 and text == source:
                translated = translated.replace("<style id='1'>", "").replace("</style>", "")
            return translated

        original_send = mock_server.MockOpenAIHandler._send_json

        class SplitHandler(mock_server.MockOpenAIHandler):
            def _send_json(handler, status, value):
                if server.request_count == 2 and repair_fails and "choices" in value:
                    message = value["choices"][0]["message"]
                    outputs = json.loads(message["content"])
                    for output in outputs:
                        if output["id"] % 2:
                            output["output"] = ""
                    message["content"] = json.dumps(outputs)
                if server.request_count == 3 and "choices" in value:
                    if split_mode in ("503", "adapt"):
                        status = 503 if split_mode == "503" else 400
                        value = {"error": {"message": "Unsupported parameter: max_tokens",
                                           "param": "max_tokens", "code": "unsupported_parameter"}}
                    else:
                        choice = value["choices"][0]
                        outputs = json.loads(choice["message"]["content"])
                        if split_mode == "partial":
                            outputs = outputs[:1]
                        elif split_mode == "empty":
                            outputs = [{"id": x["id"], "output": ""} for x in outputs]
                        elif split_mode == "style":
                            outputs[0]["output"] += "<style id='77'>"
                        elif split_mode == "truncated":
                            choice["finish_reason"] = "length"
                        elif split_mode == "duplicate":
                            outputs.append(dict(outputs[0]))
                        choice["message"]["content"] = (
                            "invalid json" if split_mode == "malformed" else json.dumps(outputs)
                        )
                        if split_mode == "refusal":
                            choice["message"]["refusal"] = "refused"
                return original_send(handler, status, value)

        server.RequestHandlerClass = SplitHandler

        def local_completion(client, request_state, max_tokens, **kwargs):
            del client, request_state
            payload = {"messages": kwargs["messages"], "max_completion_tokens": max_tokens}
            requests.append(mock_server._prompt_items(payload))
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                value = json.load(response)
            choice = value["choices"][0]
            if len(requests) == 1:
                main_outputs.extend(json.loads(choice["message"]["content"]))
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(
                    content=choice["message"]["content"],
                    refusal=choice["message"].get("refusal"),
                ),
                finish_reason=choice["finish_reason"],
            )])

        try:
            original_apply = il_class.post_translate_paragraph

            def apply(instance, paragraph, *args):
                if apply_failure == "repair" and server.request_count == 2 and paragraph is paragraphs[1]:
                    paragraph.unicode = "partially mutated"
                    return False
                if apply_failure == "split" and server.request_count == 3 and paragraph is paragraphs[1]:
                    paragraph.unicode = "partially mutated"
                    raise ValueError("fixture apply failure")
                return original_apply(instance, paragraph, *args)

            il_class.post_translate_paragraph = apply
            with mock.patch.dict(sys.modules, modules), mock.patch.object(
                runner, "_install_pdf_unicode_patch"
            ), mock.patch.object(runner, "_write_job_log", side_effect=lambda s, e, *a: logs.append(e)), mock.patch.object(
                runner, "create_completion_adapting", side_effect=local_completion
            ), mock.patch.object(mock_server, "_synthetic_translation_in_place", side_effect=lose_ordinary_style):
                policy = runner.TranslationPolicy()
                state["test_policy"] = policy
                state["test_budget"] = budget
                runner._install_min_request_patches(
                    policy, budget, state, 8192, True, False, "mock-key",
                )
                translator = openai_class()
                vars(translator).update(
                    send_temperature=False, enable_json_mode_if_requested=False,
                    send_dashscope_header=False, client=None, model="mock-model",
                    extra_body={}, update_token_count=_unused,
                )
                prompt = "## Here is the input:\n" + json.dumps([
                    {"id": index, "input": paragraph.unicode}
                    for index, paragraph in enumerate(paragraphs)
                ])
                translator.do_llm_translate(prompt)
                self.assertNotIn("fatal_error", state)
                self.assertIn(id(paragraphs[0]), state["repair_records"])
                main_checks = dict(state["repair_records"][id(paragraphs[0])]["checks"])

                # The repair function is private to the installer. Retrieve the
                # actual function captured by its installed executor, so this
                # test neither duplicates repair code nor fabricates records.
                llm_module = modules["babeldoc.format.pdf.document_il.midend.il_translator_llm_only"]
                batch_method = llm_module.PriorityThreadPoolExecutor._translate_document_batches
                captured = dict(zip(batch_method.__code__.co_freevars, batch_method.__closure__))
                captured["perform_repair_pass"].cell_contents(document_translator)
                self.assertNotIn("fatal_error", state)
                expected = 3 if grouped and (repair_fails or apply_failure == "repair") and (
                    max_requests is None or max_requests > 2
                ) else 2
                self.assertEqual(server.request_count, expected)
                self.assertEqual(budget.requests, expected)
                captured["perform_repair_pass"].cell_contents(document_translator)
                self.assertEqual(server.request_count, expected, "settlement must be idempotent")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        quality = runner.validate_translation_output(
            source, main_outputs[0]["output"], requests[0][0], mock_server._token_count,
        )
        self.assertFalse(quality["usable"])
        self.assertTrue(quality["checks"]["placeholder_issues"], "fixture must lose ordinary style tags")
        self.assertEqual(requests[0][1]["input"], sibling_source)
        self.assertTrue(runner.validate_translation_output(
            sibling_source, main_outputs[1]["output"], requests[0][1], mock_server._token_count,
        )["usable"], "the sibling must have a valid translation")
        return paragraphs, state, main_checks, requests, logs

    def test_ordinary_paragraph_simplifies_style_in_packed_repair(self):
        paragraphs, state, checks, requests, logs = self._run_main_and_repair(grouped=False)
        self.assertTrue(checks["placeholder_issues"])
        self.assertTrue(state["repair_records"][id(paragraphs[0])].get("simplified_style"))
        self.assertNotIn("<style", requests[1][0]["input"])
        self.assertTrue(any(event["event"] == "style_simplified" for event in logs))

    def test_fragment_group_preserves_style_failure_for_packed_repair(self):
        paragraphs, state, checks, requests, logs = self._run_main_and_repair(grouped=True)
        self.assertIn("fixture-group-0", state["invalid_fragment_groups"])
        self.assertEqual(len(state["repair_records"]), 2, "the whole pair must enter repair")
        record = state["repair_records"][id(paragraphs[0])]
        self.assertTrue(
            record.get("simplified_style"),
            "main validation discarded the item's ordinary-style placeholder failure: "
            f"checks={checks!r}; packed repair input={requests[1][0]['input']!r}; "
            f"style_simplified events={sum(e['event'] == 'style_simplified' for e in logs)}",
        )
        self.assertNotIn("<style", requests[1][0]["input"])

    def test_failed_groups_share_one_unlinked_split_request(self):
        paragraphs, state, _, requests, logs = self._run_main_and_repair(
            grouped=True, repair_fails=True, groups=2, max_requests=None,
        )
        self.assertEqual(len(requests[2]), 4)
        for item in requests[2]:
            for key in ("fragment_id", "fragment_part", "continues_in", "continued_from"):
                self.assertNotIn(key, item)
        self.assertTrue(all(p.unicode != state["document_paragraph_snapshots"][id(p)][1] for p in paragraphs))
        self.assertEqual(state["test_policy"].failed_paragraphs, 0)
        self.assertFalse(state["final_fragment_failure_ids"])
        self.assertEqual(state["translated_paragraph_ids"], {id(p) for p in paragraphs})
        summary = next(e for e in logs if e["event"] == "repair_summary")
        self.assertEqual((summary["repaired_items"], summary["failed_items"]), (4, 0))
        events = [e for e in logs if e["event"] == "fragment_group_split_retry"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["item_count"], 4)
        self.assertNotIn("input", json.dumps(events))

    def test_split_partial_success_counts_each_retained_fragment(self):
        paragraphs, state, _, _, logs = self._run_main_and_repair(
            grouped=True, repair_fails=True, split_mode="partial",
        )
        self.assertNotEqual(paragraphs[0].unicode, state["document_paragraph_snapshots"][id(paragraphs[0])][1])
        self.assertEqual(paragraphs[1].unicode, state["document_paragraph_snapshots"][id(paragraphs[1])][1])
        self.assertEqual(state["test_policy"].failed_paragraphs, 1)
        self.assertEqual(state["final_fragment_failure_ids"], {id(paragraphs[1])})
        self.assertEqual(state["translated_paragraph_ids"], {id(paragraphs[0])})
        summary = next(e for e in logs if e["event"] == "repair_summary")
        self.assertEqual((summary["repaired_items"], summary["failed_items"]), (1, 1))

    def test_cap_blocks_split_but_one_remaining_slot_allows_it(self):
        for cap in (2, 3):
            with self.subTest(cap=cap):
                _, state, _, requests, _ = self._run_main_and_repair(
                    grouped=True, repair_fails=True, max_requests=cap,
                )
                self.assertEqual(len(requests), cap)
                self.assertEqual(state["test_policy"].failed_paragraphs, 2 if cap == 2 else 0)

    def test_invalid_split_responses_do_not_continue_or_repack(self):
        for mode, failures in (("empty", 2), ("malformed", 2), ("refusal", 2),
                               ("truncated", 1), ("duplicate", 1), ("style", 1)):
            with self.subTest(mode=mode):
                paragraphs, state, _, requests, _ = self._run_main_and_repair(
                    grouped=True, repair_fails=True, split_mode=mode,
                )
                self.assertEqual(len(requests), 3)
                self.assertEqual(state["test_policy"].failed_paragraphs, failures)
                for p in paragraphs:
                    if id(p) in state["final_fragment_failure_ids"]:
                        snapshot = state["document_paragraph_snapshots"][id(p)]
                        self.assertEqual(p.unicode, snapshot[1])
                        self.assertIs(p.pdf_paragraph_composition, snapshot[2])

    def test_apply_failure_is_restored_and_successful_sibling_survives(self):
        for phase in ("repair", "split"):
            with self.subTest(phase=phase):
                paragraphs, state, _, _, _ = self._run_main_and_repair(
                    grouped=True, repair_fails=phase == "split", apply_failure=phase,
                )
                self.assertEqual(state["test_policy"].failed_paragraphs, 1 if phase == "split" else 0)
                self.assertNotEqual(paragraphs[0].unicode, state["document_paragraph_snapshots"][id(paragraphs[0])][1])
                if phase == "split":
                    self.assertEqual(paragraphs[1].unicode, state["document_paragraph_snapshots"][id(paragraphs[1])][1])

    def test_ordinary_failure_never_enters_group_split(self):
        _, state, _, requests, logs = self._run_main_and_repair(grouped=False, repair_fails=True)
        self.assertEqual(len(requests), 2)
        self.assertFalse(any(e["event"] == "fragment_group_split_retry" for e in logs))


if __name__ == "__main__":
    unittest.main()
