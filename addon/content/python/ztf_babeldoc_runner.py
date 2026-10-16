"""Run BabelDOC with the API key kept out of the operating-system command line."""

from __future__ import annotations

import contextlib
import functools
import html
import json
import logging
import logging.handlers
import math
import multiprocessing
import os
import re
import sys
import threading
import traceback
from typing import Any


OUTPUT_EXPANSION_FACTOR = 1.6
JSON_OUTPUT_OVERHEAD_TOKENS = 20
OUTPUT_HEADROOM_RATIO = 0.10
CONSISTENCY_SYSTEM_PROMPT = (
    "Translate the complete document as one unit. Translate technical terms, "
    "abbreviations, and named entities consistently across every item. Use the "
    "translation chosen at the first occurrence again for later occurrences. Keep "
    "abbreviations, formulas, citations, and placeholders unchanged. Return exactly "
    "the JSON array format required by the surrounding prompt, with one output "
    "item for each input item and no extra text."
)


class RequestCapExceeded(RuntimeError):
    """Raised before issuing a completion request beyond the configured cap."""

    kind = "request_cap"


_HTML_ERROR_MARKER = re.compile(r"<!doctype\s+html|<html\b|<script\b|<title\b", re.I)


def _provider_status(value: Any) -> int | None:
    candidates = [
        getattr(value, "status_code", None),
        getattr(value, "status", None),
        getattr(getattr(value, "response", None), "status_code", None),
        getattr(getattr(value, "response", None), "status", None),
    ]
    for candidate in candidates:
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    return None


def _embedded_html_error(value: Any) -> str:
    """Find an HTML body stored on an HTTP exception or its response object."""
    candidates = [getattr(value, "body", None)]
    response = getattr(value, "response", None)
    if response is not None:
        candidates.extend(
            [
                getattr(response, "text", None),
                getattr(response, "content", None),
                getattr(response, "body", None),
            ]
        )
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, bytes):
            candidate_text = candidate.decode("utf-8", errors="replace")
        else:
            candidate_text = str(candidate)
        if _HTML_ERROR_MARKER.search(candidate_text):
            return candidate_text
    return ""


def sanitize_provider_error(value: Any, status: int | None = None) -> str:
    """Replace HTML API error pages with a short, readable diagnostic."""
    text = str(value if value is not None else "").strip()
    is_html = bool(_HTML_ERROR_MARKER.search(text))
    if not is_html:
        embedded_html = _embedded_html_error(value)
        if embedded_html:
            text = (text + "\n" + embedded_html).strip()
            is_html = True
    status = status or _provider_status(value)
    if status is None:
        status_match = re.search(
            r"\b(?:HTTP(?:/\d(?:\.\d)?)?\s+|Error code:\s*|status(?:_code)?\s*[=: ]\s*)(\d{3})\b",
            text,
            re.I,
        )
        if status_match:
            status = int(status_match.group(1))
    if not is_html:
        return text

    title_match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", text, re.I | re.S)
    title = ""
    if title_match:
        title = re.sub(r"<[^>]*>", " ", title_match.group(1))
        title = re.sub(r"<[^>]*>", " ", html.unescape(title))
        title = " ".join(title.split())

    request_id = ""
    element_match = re.search(
        r"<[^>]*\bid\s*=\s*(['\"]?)requestId\1[^>]*>(.*?)</[^>]+>",
        text,
        re.I | re.S,
    )
    if element_match:
        request_id = re.sub(r"<[^>]*>", " ", element_match.group(2))
        request_id = re.sub(r"<[^>]*>", " ", html.unescape(request_id))
        request_id = " ".join(request_id.split())
    if not request_id:
        request_match = re.search(
            r"\brequest[_ -]?id\s*[:=]\s*['\"]?([A-Za-z0-9._-]{5,})",
            text,
            re.I,
        )
        if not request_match:
            # Some error templates expose the ID only as an argument to a small
            # script call after referencing the requestId element.
            request_match = re.search(
                r"requestId.{0,250}?\b[A-Za-z_$][\w$]*\s*\(\s*['\"]([A-Za-z0-9._-]{5,})['\"]\s*\)",
                text,
                re.I | re.S,
            )
        if request_match:
            request_id = request_match.group(1)

    parts = []
    if status:
        parts.append(f"HTTP {status}")
    parts.append(title or "接口返回了网页错误")
    if request_id:
        parts.append(f"requestId: {request_id}")
    if status:
        return parts[0] + "：" + parts[1] + (f"（{parts[2]}）" if len(parts) > 2 else "")
    return "；".join(parts)


def estimate_output_tokens(
    source_tokens: int,
    expansion_factor: float = OUTPUT_EXPANSION_FACTOR,
    json_overhead_tokens: int = JSON_OUTPUT_OVERHEAD_TOKENS,
) -> int:
    """Estimate Chinese output as 1.6x BabelDOC input tokens plus JSON overhead."""
    return max(1, math.ceil(max(0, source_tokens) * expansion_factor + json_overhead_tokens))


def pack_items_by_output_budget(
    items: list[Any],
    max_output_tokens: int,
    token_counter: Any,
    expansion_factor: float = OUTPUT_EXPANSION_FACTOR,
    max_items: int | None = None,
) -> list[list[Any]]:
    """Greedily pack ordered items with 10% output headroom.

    With positive per-item estimates, ordered greedy packing produces the minimum
    number of contiguous batches while preserving document reading order.
    """
    budget = max(1, math.floor(max_output_tokens * (1 - OUTPUT_HEADROOM_RATIO)))
    item_limit = max(1, int(max_items)) if max_items is not None else None
    batches: list[list[Any]] = []
    current: list[Any] = []
    current_estimate = 0

    for item in items:
        estimate = estimate_output_tokens(
            token_counter(item), expansion_factor=expansion_factor
        )
        if current and (
            current_estimate + estimate > budget
            or (item_limit is not None and len(current) >= item_limit)
        ):
            batches.append(current)
            current = []
            current_estimate = 0
        current.append(item)
        current_estimate += estimate

    if current:
        batches.append(current)
    return batches


def merge_document_pending_items(
    pending_items: list[dict[str, Any]],
    requeued_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Put unconfirmed items back into the document queue in reading order."""
    unique_items: dict[int, dict[str, Any]] = {}
    for item in [*pending_items, *requeued_items]:
        paragraph = item.get("paragraph")
        key = id(paragraph) if paragraph is not None else id(item)
        unique_items[key] = item
    return sorted(
        unique_items.values(),
        key=lambda item: (
            item.get("position", (0, 0)),
            item.get("task_index", 0),
            item.get("paragraph_index", 0),
        ),
    )


def prepare_and_pack_items(
    items: list[dict[str, Any]],
    max_output_tokens: int,
    prepare: Any,
    token_counter: Any,
    expansion_factor: float = OUTPUT_EXPANSION_FACTOR,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Prepare/filter items first, then estimate and pack the actual prompt text."""
    prepared_items = []
    for item in items:
        prepared = prepare(item)
        if not isinstance(prepared, (tuple, list)) or not prepared or prepared[0] is None:
            continue
        prepared_item = dict(item)
        prepared_item["prepared_result"] = prepared
        prepared_item["source_tokens"] = max(0, int(token_counter(prepared[0])))
        prepared_items.append(prepared_item)

    packed = pack_items_by_output_budget(
        prepared_items,
        max_output_tokens,
        lambda item: item["source_tokens"],
        expansion_factor=expansion_factor,
    )
    return prepared_items, packed


def update_expansion_factor(
    current_factor: float, input_tokens: int, output_tokens: int
) -> float:
    """Raise the estimate to the observed output/input ratio after truncation."""
    if input_tokens <= 0 or output_tokens <= 0:
        return current_factor
    return max(current_factor, output_tokens / input_tokens)


def call_translator_without_cache(
    instance: Any,
    text: str,
    rate_limit_params: Any = None,
    *,
    llm: bool,
    rate_limiter: Any = None,
) -> Any:
    """Call BabelDOC's translator directly so fallback text cannot be cached."""
    if rate_limiter is not None:
        rate_limiter.wait()
    method = instance.do_llm_translate if llm else instance.do_translate
    return method(text, rate_limit_params)


class RequestBudget:
    """Count completion attempts and update the request progress display."""

    def __init__(self, max_requests: int, on_update: Any = None) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.planned = 0
        self.on_update = on_update

    def _notify(self) -> None:
        if self.on_update:
            self.on_update(self.requests, self.planned)

    def set_planned(self, planned: int) -> None:
        self.planned = max(0, int(planned))
        self._notify()

    def reserve_request(self) -> None:
        if self.requests >= self.max_requests:
            raise RequestCapExceeded(
                f"已达到单篇请求上限 {self.max_requests} 次，已停止以免耗尽接口额度"
            )
        self.requests += 1
        self._notify()

    def add_additional_requests(self, count: int) -> None:
        """Include further queue requests in the live estimate."""
        self.planned += max(0, int(count))
        self._notify()


def request_once(budget: RequestBudget, operation: Any) -> Any:
    """Run one counted operation without retries, enforcing the cap before it starts."""
    budget.reserve_request()
    return operation()


def _failed_json_items(
    items: list[dict[str, Any]], message: str
) -> list[dict[str, Any]]:
    """Return a valid, aligned batch that keeps each failed item's source text."""
    failed = []
    for index, item in enumerate(items):
        source = item.get("input", "")
        failed.append(
            {
                "id": item.get("id", index),
                "output": source if isinstance(source, str) else "",
                "_ztf_failed": message,
            }
        )
    return failed


def _has_cut_off_json_prefix(text: str, error: json.JSONDecodeError) -> bool:
    """Recognize an unfinished JSON array, without treating empty or bad JSON as cut off."""
    prefix = text.strip()
    if not prefix or not prefix.startswith("["):
        return False
    if error.msg == "Unterminated string starting at":
        return True
    if _ends_with_incomplete_json_escape(prefix):
        return True
    if error.pos < len(prefix) - 1:
        return False
    if prefix.endswith((",", ":", "[", "{")):
        return True

    stack: list[str] = []
    in_string = False
    escaped = False
    matching = {"}": "{", "]": "["}
    for character in prefix:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            stack.append(character)
        elif character in "]}":
            if not stack or stack.pop() != matching[character]:
                return False
    return in_string or bool(stack)


def _ends_with_incomplete_json_escape(text: str) -> bool:
    """Recognize a response cut off inside a JSON escape, including ``\\u4e``."""
    in_string = False
    position = 0
    valid_escapes = {'"', "\\", "/", "b", "f", "n", "r", "t"}
    while position < len(text):
        character = text[position]
        if not in_string:
            if character == '"':
                in_string = True
            position += 1
            continue
        if character == '"':
            in_string = False
            position += 1
            continue
        if character != "\\":
            position += 1
            continue
        if position + 1 >= len(text):
            return True
        escaped = text[position + 1]
        if escaped == "u":
            digits = text[position + 2 : position + 6]
            if len(digits) < 4 and all(
                digit in "0123456789abcdefABCDEF" for digit in digits
            ):
                return position + 2 + len(digits) == len(text)
            if len(digits) >= 4 and all(
                digit in "0123456789abcdefABCDEF" for digit in digits[:4]
            ):
                position += 6
                continue
            return False
        if escaped not in valid_escapes:
            return False
        position += 2
    return False


def _align_json_items(
    input_items: list[dict[str, Any]], output_items: Any
) -> list[dict[str, Any]]:
    """Validate model output per item and preserve good siblings independently."""
    if isinstance(output_items, dict) and len(input_items) == 1:
        output_items = [output_items]
    if not isinstance(output_items, list):
        return _failed_json_items(input_items, "模型没有返回所需的 JSON 数组，已保留原文。")

    by_id: dict[str, dict[str, Any]] = {}
    ambiguous_ids: set[str] = set()
    for candidate in output_items:
        if not isinstance(candidate, dict) or candidate.get("id") is None:
            continue
        key = str(candidate["id"])
        if key in by_id:
            ambiguous_ids.add(key)
        else:
            by_id[key] = candidate

    aligned = []
    for index, item in enumerate(input_items):
        item_id = item.get("id", index)
        key = str(item_id)
        candidate = by_id.get(key)
        translated = None
        if candidate is not None and key not in ambiguous_ids:
            translated = candidate.get("output", candidate.get("input"))
        if isinstance(translated, str) and translated.strip():
            aligned.append({"id": item_id, "output": translated})
        else:
            aligned.extend(
                _failed_json_items(
                    [item], "模型未返回该段的有效译文，已保留原文。"
                )
            )
    return aligned


def _parse_json_array_prefix(text: str) -> tuple[list[Any], bool, bool]:
    """Parse complete values from the start of an array, even if its tail was cut off."""
    prefix = text.strip()
    if not prefix.startswith("["):
        return [], False, False

    decoder = json.JSONDecoder()
    values: list[Any] = []
    position = 1
    while True:
        while position < len(prefix) and prefix[position].isspace():
            position += 1
        if position >= len(prefix):
            return values, False, True
        if prefix[position] == "]":
            return values, not prefix[position + 1 :].strip(), False
        if values:
            if prefix[position] != ",":
                return values, False, False
            position += 1
            while position < len(prefix) and prefix[position].isspace():
                position += 1
            if position >= len(prefix):
                return values, False, True
            if prefix[position] == "]":
                return values, False, False
        try:
            value, next_position = decoder.raw_decode(prefix, position)
        except json.JSONDecodeError as error:
            return values, False, _has_cut_off_json_prefix(prefix, error)
        values.append(value)
        position = next_position
        while position < len(prefix) and prefix[position].isspace():
            position += 1
        if position >= len(prefix):
            return values, False, True
        if prefix[position] == "]":
            return values, not prefix[position + 1 :].strip(), False
        if prefix[position] != ",":
            return values, False, False


def restore_paragraph_snapshots(
    snapshots: dict[int, tuple[Any, Any, Any]], paragraph_ids: set[int]
) -> None:
    """Restore the original text and composition for paragraphs kept untranslated."""
    for paragraph_id in paragraph_ids:
        snapshot = snapshots.get(paragraph_id)
        if snapshot is None:
            continue
        paragraph, original_unicode, original_composition = snapshot
        paragraph.unicode = original_unicode
        paragraph.pdf_paragraph_composition = original_composition


def record_submitted_paragraphs(
    policy: Any,
    input_items: list[dict[str, Any]],
    prepared_paragraphs: list[Any],
    submitted_paragraph_ids: set[int],
) -> None:
    """Count paragraphs only once they are part of an actual completion request."""
    for item_index, input_item in enumerate(input_items):
        try:
            paragraph_index = int(input_item.get("id", item_index))
        except (TypeError, ValueError):
            continue
        if paragraph_index < 0 or paragraph_index >= len(prepared_paragraphs):
            continue
        paragraph = prepared_paragraphs[paragraph_index]
        if paragraph is None:
            continue
        paragraph_id = id(paragraph)
        policy._record_attempt(paragraph)
        submitted_paragraph_ids.add(paragraph_id)


def record_submitted_batch_failures(
    policy: Any,
    paragraph_snapshots: dict[int, tuple[Any, Any, Any]],
    submitted_paragraph_ids: set[int],
    message: str,
) -> None:
    """Mark sent paragraphs as failed when a fatal error prevents result application."""
    for paragraph_id in submitted_paragraph_ids:
        snapshot = paragraph_snapshots.get(paragraph_id)
        if snapshot is not None:
            policy.record_failure(snapshot[0], message)


def translate_json_items_with_splitting(
    items: list[dict[str, Any]],
    request: Any,
    budget: RequestBudget,
    on_truncation: Any = None,
    repack_remaining: Any = None,
    defer_remaining: Any = None,
    on_zero_prefix: Any = None,
) -> list[dict[str, Any]]:
    """Translate from one ordered queue, returning only unconfirmed items to its head.

    When ``defer_remaining`` is supplied, this function handles exactly one request;
    the document executor owns the shared queue and will repack the returned suffix
    together with all later paragraphs. Without it, this function drives that same
    queue itself. A zero-prefix truncation only sets a half-size cap for the next
    request; it never sends both halves as pre-formed child batches.
    """

    def item_key(item: dict[str, Any], index: int) -> str:
        return str(item.get("id", index))

    def merge_results(
        batch: list[dict[str, Any]], results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_id = {str(result.get("id")): result for result in results}
        merged = []
        for index, item in enumerate(batch):
            result = by_id.get(item_key(item, index))
            if result is None:
                result = _failed_json_items(
                    [item], "模型未返回该段的有效译文，已保留原文。"
                )[0]
            merged.append(result)
        return merged

    def observe_successes(
        input_batch: list[dict[str, Any]],
        output_batch: list[dict[str, Any]],
    ) -> None:
        if on_truncation is None:
            return
        successful_inputs = []
        successful_outputs = []
        for input_item, output_item in zip(input_batch, output_batch):
            if not output_item.get("_ztf_failed"):
                successful_inputs.append(input_item)
                successful_outputs.append(output_item)
        if successful_inputs:
            on_truncation(successful_inputs, successful_outputs)

    if not items:
        return []

    pending = list(items)
    results_by_id: dict[str, dict[str, Any]] = {}
    next_item_limit: int | None = None
    document_executor_owns_queue = defer_remaining is not None

    while pending:
        if repack_remaining is None:
            batch = pending[:next_item_limit] if next_item_limit else pending[:]
        else:
            batches = [part for part in repack_remaining(pending) if part]
            batch = batches[0] if batches else pending[:]
            if next_item_limit is not None:
                batch = batch[:next_item_limit]
        if not batch:
            batch = pending[:1]
        next_item_limit = None

        if budget.planned <= budget.requests:
            budget.add_additional_requests(1)
        response = request_once(budget, lambda: request(batch))
        content, finish_reason = response[0], response[1]
        refusal = response[2] if len(response) > 2 else None
        finish_reason = str(finish_reason or "").lower()
        if refusal or finish_reason in {"content_filter", "refusal"}:
            aligned = _failed_json_items(
                batch, "模型拒绝或过滤了该批次，已保留原文。"
            )
            results_by_id.update(
                {str(result.get("id")): result for result in aligned}
            )
            pending = pending[len(batch) :]
            if document_executor_owns_queue:
                break
            continue

        cleaned = str(content or "").strip()
        if not cleaned and finish_reason != "length":
            aligned = _failed_json_items(batch, "模型返回空内容，已保留原文。")
            results_by_id.update(
                {str(result.get("id")): result for result in aligned}
            )
            pending = pending[len(batch) :]
            if document_executor_owns_queue:
                break
            continue
        if cleaned.startswith("<json>"):
            cleaned = cleaned[6:]
        if cleaned.endswith("</json>"):
            cleaned = cleaned[:-7]
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        parsed, complete_array, cutoff_error = _parse_json_array_prefix(cleaned)
        is_truncated = finish_reason == "length" or cutoff_error
        if not is_truncated:
            if not complete_array:
                aligned = _failed_json_items(
                    batch, "模型返回了无效的 JSON，已保留原文。"
                )
            else:
                aligned = _align_json_items(batch, parsed)
                observe_successes(batch, aligned)
            results_by_id.update(
                {str(result.get("id")): result for result in aligned}
            )
            pending = pending[len(batch) :]
            if document_executor_owns_queue:
                break
            continue

        # A truncated response never confirms its last parsed object, even when
        # finish_reason says "length" after a syntactically closed JSON array.
        complete_prefix = parsed[:-1]
        parsed_id_counts: dict[str, int] = {}
        candidates_by_id: dict[str, dict[str, Any]] = {}
        for candidate in parsed:
            if not isinstance(candidate, dict) or candidate.get("id") is None:
                continue
            candidate_id = str(candidate["id"])
            parsed_id_counts[candidate_id] = parsed_id_counts.get(candidate_id, 0) + 1
        for candidate in complete_prefix:
            if not isinstance(candidate, dict) or candidate.get("id") is None:
                continue
            candidate_id = str(candidate["id"])
            candidates_by_id[candidate_id] = candidate

        confirmed_outputs: list[dict[str, Any]] = []
        for index, item in enumerate(batch):
            key = item_key(item, index)
            if parsed_id_counts.get(key) != 1 or key not in candidates_by_id:
                break
            candidate = candidates_by_id[key]
            translated = candidate.get("output", candidate.get("input"))
            if not isinstance(translated, str) or not translated.strip():
                break
            confirmed_outputs.append(
                {"id": item.get("id", index), "output": translated}
            )

        observe_successes(batch[: len(confirmed_outputs)], confirmed_outputs)
        results_by_id.update(
            {str(result.get("id")): result for result in confirmed_outputs}
        )
        unresolved_request_items = batch[len(confirmed_outputs) :]

        if not confirmed_outputs and len(batch) == 1:
            failed = _failed_json_items(
                batch, "模型输出被截断，单段无法继续拆分，已保留原文。"
            )
            results_by_id.update(
                {str(result.get("id")): result for result in failed}
            )
            pending = pending[1:]
            if document_executor_owns_queue:
                break
            continue

        if unresolved_request_items and document_executor_owns_queue:
            if not confirmed_outputs and len(batch) > 1 and on_zero_prefix:
                on_zero_prefix(len(batch))
            deferred_results = defer_remaining(unresolved_request_items) or []
            results_by_id.update(
                {
                    str(result.get("id")): result
                    for result in merge_results(
                        unresolved_request_items, deferred_results
                    )
                }
            )
            break

        if confirmed_outputs:
            pending = pending[len(confirmed_outputs) :]
        else:
            # Keep the complete queue intact. The next request takes only its
            # first half; later items are then eligible for greedy repacking.
            next_item_limit = max(1, len(batch) // 2)

    return merge_results(items, list(results_by_id.values()))


def _redact(value: Any, api_key: str) -> str:
    text = str(value if value is not None else "")
    return text.replace(api_key, "[已隐藏]") if api_key else text


def _redact_payload(value: Any, api_key: str) -> Any:
    """Redact protocol values before JSON escapes quotes and backslashes."""
    if isinstance(value, str):
        return _redact(value, api_key)
    if isinstance(value, dict):
        return {
            _redact(key, api_key): _redact_payload(item, api_key)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_payload(item, api_key) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value, api_key)


def _emit(prefix: str, payload: dict[str, Any], api_key: str) -> None:
    safe_payload = _redact_payload(payload, api_key)
    line = prefix + json.dumps(safe_payload, ensure_ascii=False, default=str)
    print(line, flush=True)


class _SynchronousPolicyCapture(logging.Filter):
    """Collect policy records on the emitting thread before QueueHandler enqueues them."""

    def __init__(self, policy: "TranslationPolicy") -> None:
        super().__init__()
        self.policy = policy

    def filter(self, record: logging.LogRecord) -> bool:
        self.policy.handle(record)
        return True


def _install_synchronous_log_capture(babeldoc_main: Any, policy: "TranslationPolicy") -> None:
    """Keep policy collection ahead of BabelDOC 0.6.4's lossy QueueListener path."""
    original_speed_up_logs = babeldoc_main.speed_up_logs
    capture_filter = _SynchronousPolicyCapture(policy)

    @functools.wraps(original_speed_up_logs)
    def speed_up_logs_with_policy_capture() -> Any:
        root_logger = logging.getLogger()
        # Do not let QueueListener invoke policy later on its own thread; by then
        # thread-local paragraph context is gone and EvictQueue may have dropped logs.
        root_logger.handlers[:] = [
            handler for handler in root_logger.handlers if handler is not policy
        ]
        result = original_speed_up_logs()

        queue_handlers = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, logging.handlers.QueueHandler)
        ]
        if queue_handlers:
            queue_handlers[0].addFilter(capture_filter)
        elif root_logger.handlers:
            # Retain synchronous collection if another BabelDOC version does not
            # use QueueHandler, while preserving its configured output handlers.
            root_logger.handlers[0].addFilter(capture_filter)
        else:
            root_logger.addHandler(policy)
        return result

    babeldoc_main.speed_up_logs = speed_up_logs_with_policy_capture


class TranslationPolicy(logging.Handler):
    """Collect paragraph failures and token counts from BabelDOC's own logs."""

    _completion_tokens_re = re.compile(r"^Completion tokens:\s*(\d+)\s*$")
    _translation_counts_re = re.compile(
        r"Translation completed\.\s*Total:\s*(\d+)\s*,\s*Successful:\s*(\d+)\s*,\s*Fallback:\s*(\d+)",
        re.I,
    )

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self._lock = threading.Lock()
        self._thread_state = threading.local()
        self._attempted_paragraph_ids: dict[int, Any] = {}
        self._failed_paragraph_ids: set[int] = set()
        self._unbound_failed_paragraphs = 0
        self.paragraph_attempts = 0
        self.failed_paragraphs = 0
        self.track_pretranslate_attempts = True
        self.completion_tokens: int | None = None
        self.translation_counts: tuple[int, int, int] | None = None
        self.first_error = ""

    def _record_attempt(self, paragraph: Any) -> None:
        if paragraph is None:
            return
        paragraph_id = id(paragraph)
        with self._lock:
            # Keep the object alive so its id cannot be reused during the job.
            self._attempted_paragraph_ids.setdefault(paragraph_id, paragraph)
            self.paragraph_attempts = len(self._attempted_paragraph_ids)

    def record_failure(self, paragraph: Any, message: str) -> None:
        """Count a suppressed per-paragraph fallback while preserving its source text."""
        with self._lock:
            if paragraph is None:
                self._unbound_failed_paragraphs += 1
            else:
                paragraph_id = id(paragraph)
                self._attempted_paragraph_ids.setdefault(paragraph_id, paragraph)
                self._failed_paragraph_ids.add(paragraph_id)
            self.paragraph_attempts = len(self._attempted_paragraph_ids)
            self.failed_paragraphs = (
                len(self._failed_paragraph_ids) + self._unbound_failed_paragraphs
            )
            if not self.first_error:
                self.first_error = sanitize_provider_error(self._first_line(message)) or (
                    "BabelDOC produced an unusable batch translation."
                )

    def instrument(self, il_translator_class: type) -> None:
        """Count paragraphs with text in both batch and fallback translation paths."""
        policy = self
        original_paragraph = il_translator_class.translate_paragraph
        original_pre_translate = il_translator_class.pre_translate_paragraph

        @functools.wraps(original_paragraph)
        def tracked_paragraph(instance: Any, *args: Any, **kwargs: Any) -> Any:
            paragraph = args[0] if args else kwargs.get("paragraph")
            missing = object()
            previous = getattr(policy._thread_state, "paragraph", missing)
            policy._thread_state.paragraph = paragraph
            try:
                return original_paragraph(instance, *args, **kwargs)
            finally:
                if previous is missing:
                    del policy._thread_state.paragraph
                else:
                    policy._thread_state.paragraph = previous

        @functools.wraps(original_pre_translate)
        def tracked_pre_translate(instance: Any, *args: Any, **kwargs: Any) -> Any:
            paragraph = args[0] if args else kwargs.get("paragraph")
            result = original_pre_translate(instance, *args, **kwargs)
            if (
                policy.track_pretranslate_attempts
                and isinstance(result, (tuple, list))
                and result
                and result[0] is not None
            ):
                policy._record_attempt(paragraph)
            return result

        il_translator_class.translate_paragraph = tracked_paragraph
        il_translator_class.pre_translate_paragraph = tracked_pre_translate

    @staticmethod
    def _is_paragraph_error(record: logging.LogRecord, message: str) -> bool:
        return (
            record.name.endswith(".il_translator")
            and (
                message.startswith("Error translating paragraph.")
                or message.startswith("ContentFilterError:")
            )
        )

    @staticmethod
    def _first_line(value: Any) -> str:
        text = str(value).strip()
        return text.splitlines()[0].strip() if text else ""

    @staticmethod
    def _exception_name(record: logging.LogRecord) -> str:
        exc_info = record.exc_info
        if exc_info and len(exc_info) > 1 and exc_info[1] is not None:
            return type(exc_info[1]).__name__
        return ""

    @staticmethod
    def _underlying_error(record: logging.LogRecord, message: str) -> str:
        exc_info = record.exc_info
        exception_name = ""
        if exc_info and len(exc_info) > 1 and exc_info[1] is not None:
            exception_name = type(exc_info[1]).__name__
            detail = TranslationPolicy._first_line(
                sanitize_provider_error(exc_info[1])
            )
            if detail:
                return detail

        if message.startswith("ContentFilterError:"):
            detail = TranslationPolicy._first_line(message.partition(":")[2])
            if detail.endswith("."):
                detail = detail[:-1].rstrip()
            if detail:
                return sanitize_provider_error(detail)

        # Paragraph source text can itself contain "Error:". BabelDOC appends
        # the actual exception at the end, so parse the final marker only.
        marker = message.rfind("Error:")
        if marker >= 0:
            detail = TranslationPolicy._first_line(message[marker + len("Error:"):])
            # BabelDOC appends a period after the exception text in this log message.
            if detail.endswith("."):
                detail = detail[:-1].rstrip()
            if detail:
                return sanitize_provider_error(detail)
        return sanitize_provider_error(
            exception_name
            or message.strip()
            or "BabelDOC reported a paragraph translation error."
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            token_match = self._completion_tokens_re.match(message)
            if token_match:
                with self._lock:
                    self.completion_tokens = int(token_match.group(1))

            counts_match = self._translation_counts_re.search(message)
            if counts_match:
                with self._lock:
                    self.translation_counts = tuple(
                        int(value) for value in counts_match.groups()
                    )

            if not self._is_paragraph_error(record, message):
                return

            paragraph = getattr(self._thread_state, "paragraph", None)
            with self._lock:
                if paragraph is not None:
                    paragraph_id = id(paragraph)
                    self._attempted_paragraph_ids.setdefault(paragraph_id, paragraph)
                    self._failed_paragraph_ids.add(paragraph_id)
                    self.paragraph_attempts = len(self._attempted_paragraph_ids)
                else:
                    # BabelDOC executor logging can lose thread-local context;
                    # each matching fallback error record still represents one failed paragraph.
                    self._unbound_failed_paragraphs += 1
                self.failed_paragraphs = (
                    len(self._failed_paragraph_ids) + self._unbound_failed_paragraphs
                )

            # Count the failed paragraph before extracting a human-readable
            # diagnostic so an empty or malformed exception message cannot hide it.
            try:
                error = self._underlying_error(record, message)
            except Exception:
                error = self._exception_name(record) or (
                    "BabelDOC reported a paragraph translation error."
                )
            with self._lock:
                if not self.first_error:
                    self.first_error = error
        except Exception:
            # Diagnostics must never interfere with BabelDOC's translation threads.
            return

    def decision(self) -> dict[str, Any] | None:
        with self._lock:
            attempts = self.paragraph_attempts
            failures = self.failed_paragraphs
            completion_tokens = self.completion_tokens
            first_error = self.first_error

        count_summary = self.count_summary()

        all_paragraphs_failed = attempts > 0 and failures >= attempts
        no_completion_tokens = attempts > 0 and completion_tokens == 0
        if no_completion_tokens or all_paragraphs_failed:
            return {
                "kind": "translation_failed",
                "failed_paragraphs": failures,
                "message": (first_error or "BabelDOC produced no completion tokens for paragraph text.")
                + (f"；{count_summary}" if count_summary else ""),
            }
        if failures:
            return {
                "kind": "warning",
                "failed_paragraphs": failures,
                "message": (first_error or "One or more paragraph translations failed.")
                + (f"；{count_summary}" if count_summary else ""),
            }
        return None

    def count_summary(self) -> str:
        """Summarize final wrapper outcomes, including items kept as source text."""
        with self._lock:
            total = self.paragraph_attempts
            fallback = min(total, self.failed_paragraphs)
            translation_finished = self.translation_counts is not None
        if total <= 0 or not translation_finished:
            return ""
        successful = total - fallback
        return f"{total} 段中 {successful} 段成功（{fallback} 段保留原文）"


def _configure_logging(api_key: str, policy: TranslationPolicy) -> None:
    class RedactingStreamHandler(logging.StreamHandler):
        def format(self, record: logging.LogRecord) -> str:
            return _redact(super().format(record), api_key)

    handler = RedactingStreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    # BabelDOC's cli() uses basicConfig with a RichHandler that writes to stdout.
    # Configure the root first; basicConfig then leaves this sanitized stderr handler in place.
    root_logger = logging.getLogger()
    root_logger.handlers[:] = [handler, policy]
    root_logger.setLevel(logging.INFO)

    babeldoc_logger = logging.getLogger("babeldoc")
    babeldoc_logger.handlers.clear()
    babeldoc_logger.propagate = True


def _read_integer_setting(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def ensure_runner_options(arguments: list[str]) -> list[str]:
    """Always disable extra glossary/fallback calls and supply a consistency prompt."""
    result = list(arguments)
    for flag in ("--no-auto-extract-glossary", "--disable-same-text-fallback"):
        if flag not in result:
            result.append(flag)
    if "--custom-system-prompt" not in result:
        result.extend(["--custom-system-prompt", CONSISTENCY_SYSTEM_PROMPT])
    return result


def _extract_json_array_prompt(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    # Match the template's standalone heading. The same phrase can appear in a
    # paragraph serialized inside the JSON array, where it is not a delimiter.
    headings = list(re.finditer(r"(?m)^## Here is the input:[ \t]*$", text))
    if not headings:
        return None
    start = text.find("[", headings[-1].end())
    if start < 0:
        return None
    try:
        items = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return None
    return text[:start], items


def _install_min_request_patches(
    policy: TranslationPolicy,
    request_budget: RequestBudget,
    request_state: dict[str, Any],
    max_output_tokens: int,
) -> None:
    """Batch the complete document, count requests, and suppress fallback calls."""
    import openai
    import babeldoc.format.pdf.document_il.midend.il_translator_llm_only as llm_module
    import babeldoc.translator.translator as translator_module
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.translator.translator import OpenAITranslator

    original_openai = openai.OpenAI

    @functools.wraps(original_openai)
    def openai_without_retries(*args: Any, **kwargs: Any) -> Any:
        kwargs["max_retries"] = 0
        return original_openai(*args, **kwargs)

    openai.OpenAI = openai_without_retries

    paragraph_font_maps: dict[int, tuple[Any, Any]] = {}
    original_pre_translate = ILTranslator.pre_translate_paragraph

    @functools.wraps(original_pre_translate)
    def pre_translate_with_paragraph_fonts(
        instance: Any,
        paragraph: Any,
        tracker: Any,
        page_font_map: Any = None,
        xobj_font_map: Any = None,
    ) -> Any:
        prepared_cache = request_state.get("preprocessed_results", {})
        if id(paragraph) in prepared_cache:
            result = prepared_cache[id(paragraph)]
        else:
            specific_maps = paragraph_font_maps.get(id(paragraph))
            if specific_maps is not None:
                page_font_map, xobj_font_map = specific_maps
            result = original_pre_translate(
                instance, paragraph, tracker, page_font_map, xobj_font_map
            )
        prepared_paragraphs = request_state.get("prepared_paragraphs")
        if (
            prepared_paragraphs is not None
            and isinstance(result, (tuple, list))
            and result
            and result[0] is not None
        ):
            prepared_paragraphs.append(paragraph)
        return result

    ILTranslator.pre_translate_paragraph = pre_translate_with_paragraph_fonts

    def record_request_error(error: Exception) -> None:
        if isinstance(error, RequestCapExceeded):
            request_state["fatal_kind"] = "request_cap"
        else:
            request_state["fatal_kind"] = "api_error"
        if not request_state.get("fatal_error"):
            request_state["fatal_error"] = sanitize_provider_error(error) or type(error).__name__

    def completion_options(instance: Any, rate_limit_params: Any) -> tuple[dict[str, Any], dict[str, str]]:
        options: dict[str, Any] = {}
        if instance.send_temperature:
            options.update(instance.options)
        if (
            instance.enable_json_mode_if_requested
            and rate_limit_params
            and rate_limit_params.get("request_json_mode", False)
        ):
            options["response_format"] = {"type": "json_object"}
        headers: dict[str, str] = {}
        if instance.send_dashscope_header:
            headers["X-DashScope-DataInspection"] = (
                '{"input": "disable", "output": "disable"}'
            )
        return options, headers

    def wait_for_split_request(request_index: int) -> None:
        # The uncached wrapper rate-limits the first request; continuations must
        # pass through BabelDOC's limiter too.
        if request_index <= 0:
            return
        limiter = getattr(translator_module, "_translate_rate_limiter", None)
        if limiter is not None:
            limiter.wait()

    def patched_do_llm_translate(
        instance: Any,
        text: str | None,
        rate_limit_params: dict[str, Any] | None = None,
    ) -> str | None:
        if text is None:
            return None

        options, extra_headers = completion_options(instance, rate_limit_params)
        parsed_prompt = _extract_json_array_prompt(text)
        request_index = 0

        def request_batch(
            batch: list[dict[str, Any]],
        ) -> tuple[str, str | None, Any]:
            nonlocal request_index
            wait_for_split_request(request_index)
            request_index += 1
            record_submitted_paragraphs(
                policy,
                batch,
                request_state.get("prepared_paragraphs") or [],
                request_state.setdefault("submitted_paragraph_ids", set()),
            )
            prompt_prefix = parsed_prompt[0] if parsed_prompt else ""
            prompt_text = (
                prompt_prefix + json.dumps(batch, ensure_ascii=False, indent=2)
                if parsed_prompt
                else text
            )
            response = instance.client.chat.completions.create(
                model=instance.model,
                **options,
                max_tokens=max_output_tokens,
                messages=[{"role": "user", "content": prompt_text}],
                extra_headers=extra_headers,
                extra_body=instance.extra_body,
            )
            instance.update_token_count(response)
            choice = response.choices[0]
            message = choice.message
            return (
                message.content or "",
                getattr(choice, "finish_reason", None),
                getattr(message, "refusal", None),
            )

        try:
            if parsed_prompt:
                def observe_truncated_prefix(
                    input_items: list[dict[str, Any]],
                    output_items: list[dict[str, Any]],
                ) -> None:
                    active_translator = request_state.get("active_llm_translator")
                    token_counter = getattr(
                        active_translator, "calc_token_count", None
                    )
                    if not callable(token_counter):
                        return
                    input_tokens = sum(
                        token_counter(str(item.get("input", "")))
                        for item in input_items
                    )
                    output_tokens = sum(
                        token_counter(str(item.get("output", "")))
                        for item in output_items
                    )
                    request_state["output_expansion_factor"] = update_expansion_factor(
                        request_state.get(
                            "output_expansion_factor", OUTPUT_EXPANSION_FACTOR
                        ),
                        input_tokens,
                        output_tokens,
                    )

                def defer_unconfirmed_suffix(
                    remaining_json_items: list[dict[str, Any]],
                ) -> list[dict[str, Any]]:
                    """Return source placeholders and requeue full paragraph records."""
                    prepared_paragraphs = request_state.get("prepared_paragraphs") or []
                    current_items = request_state.get("current_packed_items") or []
                    items_by_paragraph = {
                        id(item["paragraph"]): item for item in current_items
                    }
                    requeued = request_state.setdefault("deferred_items", [])
                    requeued_ids = {id(item["paragraph"]) for item in requeued}
                    deferred_outputs = []
                    for index, input_item in enumerate(remaining_json_items):
                        try:
                            output_index = int(input_item.get("id", index))
                        except (TypeError, ValueError):
                            output_index = -1
                        paragraph = (
                            prepared_paragraphs[output_index]
                            if 0 <= output_index < len(prepared_paragraphs)
                            else None
                        )
                        paragraph_item = (
                            items_by_paragraph.get(id(paragraph))
                            if paragraph is not None
                            else None
                        )
                        if paragraph_item is None:
                            deferred_outputs.extend(
                                _failed_json_items(
                                    [input_item],
                                    "截断响应中的段落无法关联回原文，已保留原文。",
                                )
                            )
                            continue
                        paragraph_id = id(paragraph)
                        if paragraph_id not in requeued_ids:
                            requeued.append(paragraph_item)
                            requeued_ids.add(paragraph_id)
                        deferred_outputs.append(
                            {
                                "id": input_item.get("id", index),
                                "output": input_item.get("input", ""),
                                "_ztf_deferred": True,
                            }
                        )
                    return deferred_outputs

                def cap_next_request_after_empty_prefix(
                    previous_request_item_count: int,
                ) -> None:
                    # The document loop will repack this half-sized prefix from
                    # the head of its one pending queue. Any suffix from that
                    # request stays adjacent to the unsent document paragraphs.
                    request_state["next_request_item_limit"] = max(
                        1, previous_request_item_count // 2
                    )

                outputs = translate_json_items_with_splitting(
                    parsed_prompt[1],
                    request_batch,
                    request_budget,
                    on_truncation=observe_truncated_prefix,
                    defer_remaining=defer_unconfirmed_suffix,
                    on_zero_prefix=cap_next_request_after_empty_prefix,
                )
                prepared_paragraphs = request_state.get("prepared_paragraphs") or []
                active_translator = request_state.get("active_llm_translator")
                failed_paragraph_ids = request_state.get(
                    "failed_batch_paragraph_ids", set()
                )
                normalized_outputs = []
                for item_index, input_item in enumerate(parsed_prompt[1]):
                    output_index = int(input_item.get("id", item_index))
                    output_item = outputs[item_index] if item_index < len(outputs) else None
                    if not isinstance(output_item, dict):
                        output_item = {
                            "id": input_item.get("id", item_index),
                            "output": input_item.get("input", ""),
                            "_ztf_failed": "模型未返回该段的有效译文，已保留原文。",
                        }
                    source_text = input_item.get("input")
                    translated_text = output_item.get("output")
                    failure_message = output_item.get("_ztf_failed")
                    was_deferred = bool(output_item.get("_ztf_deferred"))
                    if output_index < 0 or output_index >= len(prepared_paragraphs):
                        paragraph = None
                    else:
                        paragraph = prepared_paragraphs[output_index]

                    if was_deferred and paragraph is not None:
                        # BabelDOC applies the source placeholder as a no-op result.
                        # Restore the untouched paragraph before its queued retry.
                        failed_paragraph_ids.add(id(paragraph))
                    if failure_message:
                        if paragraph is not None:
                            failed_paragraph_ids.add(id(paragraph))
                        policy.record_failure(
                            paragraph,
                            str(failure_message),
                        )
                    if (
                        not failure_message
                        and not was_deferred
                        and paragraph is not None
                        and isinstance(source_text, str)
                        and translated_text == source_text
                        and active_translator is not None
                        and active_translator.calc_token_count(source_text) > 10
                    ):
                        failed_paragraph_ids.add(id(paragraph))
                        policy.record_failure(
                            paragraph,
                            "模型返回的文本与原文相同，已保留原文。",
                        )

                    # The private marker is consumed here. BabelDOC receives one
                    # well-formed output item per input and never enters its batch
                    # exception path, which assigns a shared list to paragraph.unicode.
                    normalized_outputs.append(
                        {
                            "id": input_item.get("id", item_index),
                            "output": (
                                source_text
                                if failure_message or not isinstance(translated_text, str)
                                else translated_text
                            ),
                        }
                    )
                return json.dumps(normalized_outputs, ensure_ascii=False)

            # Keep any unexpected BabelDOC completion in the same request budget.
            wait_for_split_request(request_index)
            request_index += 1
            response = request_once(
                request_budget,
                lambda: instance.client.chat.completions.create(
                    model=instance.model,
                    **options,
                    max_tokens=max_output_tokens,
                    messages=[{"role": "user", "content": text}],
                    extra_headers=extra_headers,
                    extra_body=instance.extra_body,
                ),
            )
            instance.update_token_count(response)
            return response.choices[0].message.content.strip()
        except Exception as error:
            record_request_error(error)
            raise

    def patched_do_translate(
        instance: Any,
        text: str,
        rate_limit_params: dict[str, Any] | None = None,
    ) -> str:
        options, _ = completion_options(instance, rate_limit_params)
        try:
            response = request_once(
                request_budget,
                lambda: instance.client.chat.completions.create(
                    model=instance.model,
                    **options,
                    max_tokens=max_output_tokens,
                    messages=instance.prompt(text),
                    extra_body=instance.extra_body,
                ),
            )
            instance.update_token_count(response)
            return response.choices[0].message.content.strip()
        except Exception as error:
            record_request_error(error)
            raise

    # Replacing the decorated methods removes BabelDOC's tenacity retries. The
    # OpenAI SDK client is also constructed with max_retries=0 above. Bypassing
    # BaseTranslator's cache prevents an original-text fallback from becoming a
    # successful-looking cached translation on a later job.
    OpenAITranslator.do_llm_translate = patched_do_llm_translate
    OpenAITranslator.do_translate = patched_do_translate

    def uncached_llm_translate(
        instance: Any,
        text: str,
        ignore_cache: bool = False,
        rate_limit_params: dict[str, Any] | None = None,
    ) -> Any:
        del ignore_cache
        return call_translator_without_cache(
            instance,
            text,
            rate_limit_params,
            llm=True,
            rate_limiter=translator_module._translate_rate_limiter,
        )

    def uncached_translate(
        instance: Any,
        text: str,
        ignore_cache: bool = False,
        rate_limit_params: dict[str, Any] | None = None,
    ) -> Any:
        del ignore_cache
        return call_translator_without_cache(
            instance,
            text,
            rate_limit_params,
            llm=False,
            rate_limiter=translator_module._translate_rate_limiter,
        )

    OpenAITranslator.llm_translate = uncached_llm_translate
    OpenAITranslator.translate = uncached_translate

    class DeferredTranslationExecutor:
        """Collect BabelDOC's per-page submissions, then translate in document order."""

        def __init__(self, max_workers: int | None = None, **kwargs: Any) -> None:
            del max_workers, kwargs
            self.tasks: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

        def __enter__(self) -> "DeferredTranslationExecutor":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            if exc_type is None and self.tasks:
                self._translate_document_batches()
            return False

        def submit(self, function: Any, *args: Any, **kwargs: Any) -> None:
            owner = getattr(function, "__self__", None)
            if (
                getattr(function, "__name__", "") == "translate_paragraph"
                and owner is not None
                and hasattr(owner, "il_translator")
                and hasattr(owner, "translate_engine")
            ):
                self.tasks.append((function, args, kwargs))
                return None

            if getattr(owner, "use_as_fallback", False):
                paragraph = args[0] if args else None
                policy.record_failure(
                    paragraph,
                    "BabelDOC 无法使用批量翻译结果，已保留原文。",
                )
                pbar = args[2] if len(args) > 2 else None
                if pbar is not None and hasattr(pbar, "advance"):
                    pbar.advance(1)
                return None

            if not request_state.get("fatal_error"):
                request_state["fatal_error"] = (
                    "BabelDOC requested an unsupported translation task."
                )
                request_state["fatal_kind"] = "runner_error"
            return None

        def _translate_document_batches(self) -> None:
            first_function, first_args, _ = self.tasks[0]
            translator = first_function.__self__
            docs = translator.il_translator.docs
            page_positions: dict[int, tuple[int, int]] = {}
            for page_index, page in enumerate(docs.page):
                for paragraph_index, paragraph in enumerate(page.pdf_paragraph):
                    page_positions[id(paragraph)] = (page_index, paragraph_index)

            flattened: list[dict[str, Any]] = []
            seen_paragraphs: set[int] = set()
            for task_index, (function, args, kwargs) in enumerate(self.tasks):
                batch = args[0]
                page_font_map = args[2] if len(args) > 2 else {}
                xobj_font_map = args[3] if len(args) > 3 else {}
                title_paragraph = args[4] if len(args) > 4 else None
                local_title_paragraph = args[5] if len(args) > 5 else None
                fallback_executor = args[6] if len(args) > 6 else None
                for index, paragraph in enumerate(batch.paragraphs):
                    paragraph_id = id(paragraph)
                    if paragraph_id in seen_paragraphs:
                        continue
                    seen_paragraphs.add(paragraph_id)
                    page = batch.pages[index]
                    tracker = batch.trackers[index]
                    flattened.append(
                        {
                            "paragraph": paragraph,
                            "page": page,
                            "tracker": tracker,
                            "page_font_map": page_font_map,
                            "xobj_font_map": xobj_font_map,
                            "title_paragraph": title_paragraph,
                            "local_title_paragraph": local_title_paragraph,
                            "fallback_executor": fallback_executor,
                            "function": function,
                            "task_index": task_index,
                            "paragraph_index": index,
                            "position": page_positions.get(
                                paragraph_id, (len(docs.page), index)
                            ),
                            "source_tokens": translator.calc_token_count(
                                paragraph.unicode or ""
                            ),
                        }
                    )

            flattened.sort(
                key=lambda item: (
                    item["position"][0],
                    item["position"][1],
                    item["task_index"],
                    item["paragraph_index"],
                )
            )
            pbar = first_args[1] if len(first_args) > 1 else None
            preprocessed_results: dict[int, Any] = {}

            def prepare_paragraph(item: dict[str, Any]) -> Any:
                result = original_pre_translate(
                    translator.il_translator,
                    item["paragraph"],
                    item["tracker"],
                    item["page_font_map"],
                    item["xobj_font_map"],
                )
                if (
                    isinstance(result, (tuple, list))
                    and result
                    and result[0] is not None
                ):
                    preprocessed_results[id(item["paragraph"])] = result
                elif pbar is not None:
                    pbar.advance(1)
                return result

            prepared_items, packed = prepare_and_pack_items(
                flattened,
                max_output_tokens,
                prepare_paragraph,
                translator.calc_token_count,
            )
            request_budget.set_planned(len(packed))
            request_state["preprocessed_results"] = preprocessed_results
            request_state["output_expansion_factor"] = OUTPUT_EXPANSION_FACTOR
            pending_items = prepared_items[:]

            while pending_items:
                if request_state.get("fatal_error"):
                    break
                next_item_limit = request_state.pop(
                    "next_request_item_limit", None
                )
                expansion_factor = request_state.get(
                    "output_expansion_factor", OUTPUT_EXPANSION_FACTOR
                )
                pending_batches = pack_items_by_output_budget(
                    pending_items,
                    max_output_tokens,
                    lambda item: item["source_tokens"],
                    expansion_factor=expansion_factor,
                    max_items=next_item_limit,
                )
                packed_batch = pending_batches[0]
                pending_items = pending_items[len(packed_batch) :]
                paragraph_batch = llm_module.BatchParagraph.__new__(
                    llm_module.BatchParagraph
                )
                paragraph_batch.paragraphs = [
                    item["paragraph"] for item in packed_batch
                ]
                paragraph_batch.pages = [item["page"] for item in packed_batch]
                paragraph_batch.trackers = [item["tracker"] for item in packed_batch]
                for item in packed_batch:
                    paragraph_font_maps[id(item["paragraph"])] = (
                        item["page_font_map"],
                        item["xobj_font_map"],
                    )

                translator.mid += 1
                source_token_count = sum(item["source_tokens"] for item in packed_batch)
                packed_function = packed_batch[0]["function"]
                fallback_executor = packed_batch[0]["fallback_executor"]
                request_state["prepared_paragraphs"] = []
                request_state["active_llm_translator"] = translator
                paragraph_snapshots = {
                    id(item["paragraph"]): (
                        item["paragraph"],
                        item["paragraph"].unicode,
                        item["paragraph"].pdf_paragraph_composition,
                    )
                    for item in packed_batch
                }
                request_state["paragraph_snapshots"] = paragraph_snapshots
                request_state["failed_batch_paragraph_ids"] = set()
                request_state["submitted_paragraph_ids"] = set()
                request_state["current_packed_items"] = packed_batch
                request_state["deferred_items"] = []
                deferred_items: list[dict[str, Any]] = []
                try:
                    packed_function(
                        paragraph_batch,
                        pbar,
                        packed_batch[0]["page_font_map"],
                        packed_batch[0]["xobj_font_map"],
                        packed_batch[0]["title_paragraph"],
                        packed_batch[-1]["local_title_paragraph"],
                        fallback_executor,
                        paragraph_token_count=source_token_count,
                        mp_id=translator.mid,
                    )
                except BaseException:
                    # BabelDOC may assign its shared original-text list to every
                    # paragraph after an outer batch error. Restore the batch before
                    # allowing the API/cap failure to terminate the document.
                    restore_paragraph_snapshots(
                        paragraph_snapshots, set(paragraph_snapshots)
                    )
                    raise
                finally:
                    deferred_items = request_state.pop("deferred_items", [])
                    failed_ids = request_state.get(
                        "failed_batch_paragraph_ids", set()
                    )
                    if request_state.get("fatal_error"):
                        record_submitted_batch_failures(
                            policy,
                            paragraph_snapshots,
                            request_state.get("submitted_paragraph_ids", set()),
                            str(request_state["fatal_error"]),
                        )
                        failed_ids = set(paragraph_snapshots)
                    # If BabelDOC's error path did run despite a suppressed fallback,
                    # a list-valued unicode is a reliable sign that it assigned the
                    # shared batch snapshot incorrectly.
                    for paragraph_id, snapshot in paragraph_snapshots.items():
                        paragraph = snapshot[0]
                        if not isinstance(paragraph.unicode, str):
                            failed_ids.add(paragraph_id)
                    restore_paragraph_snapshots(paragraph_snapshots, failed_ids)
                    request_state.pop("prepared_paragraphs", None)
                    request_state.pop("active_llm_translator", None)
                    request_state.pop("paragraph_snapshots", None)
                    request_state.pop("failed_batch_paragraph_ids", None)
                    request_state.pop("submitted_paragraph_ids", None)
                    request_state.pop("current_packed_items", None)

                if deferred_items and not request_state.get("fatal_error"):
                    # Repack the unconfirmed suffix together with paragraphs that
                    # had not been sent in this request, preserving document order.
                    pending_items = merge_document_pending_items(
                        pending_items, deferred_items
                    )

                expansion_factor = request_state.get(
                    "output_expansion_factor", expansion_factor
                )
                remaining_batches = pack_items_by_output_budget(
                    pending_items,
                    max_output_tokens,
                    lambda item: item["source_tokens"],
                    expansion_factor=expansion_factor,
                    max_items=request_state.get("next_request_item_limit"),
                )
                request_budget.set_planned(
                    request_budget.requests + len(remaining_batches)
                )

            paragraph_font_maps.clear()
            request_state.pop("preprocessed_results", None)
            request_state.pop("output_expansion_factor", None)

    llm_module.PriorityThreadPoolExecutor = DeferredTranslationExecutor


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    # Remove the key before BabelDOC starts multiprocessing children so they do not inherit it.
    api_key = os.environ.pop("ZTF_OPENAI_API_KEY", "")
    if not api_key:
        print("Missing ZTF_OPENAI_API_KEY environment variable.", file=sys.stderr, flush=True)
        return 2

    # The key is added to Python's in-memory argv only. The OS-level command line contains runner.py,
    # never this value; the plugin also asserts the key is absent from Subprocess.call arguments.
    sys.argv[1:1] = ["--openai-api-key", api_key]
    sys.argv = ensure_runner_options(sys.argv)

    try:
        max_output_tokens = _read_integer_setting(
            "ZTF_MAX_OUTPUT_TOKENS", 8192, 1024
        )
        max_requests = _read_integer_setting("ZTF_MAX_REQUESTS", 20, 1)
    except ValueError as error:
        print(_redact(error, api_key), file=sys.stderr, flush=True)
        return 2

    state: dict[str, Any] = {
        "error": "",
        "error_kind": "babeldoc_error",
        "result": "",
        "overall": 0,
        "stage": "正在准备 BabelDOC",
        "current": 0,
        "total": 0,
    }

    def emit_request_progress(requests: int, planned: int) -> None:
        _emit(
            "ZTF_PROGRESS ",
            {
                "overall": state["overall"],
                "stage": state["stage"],
                "current": state["current"],
                "total": state["total"],
                "requests": requests,
                "planned": planned,
            },
            api_key,
        )

    request_budget = RequestBudget(max_requests, emit_request_progress)
    request_state: dict[str, Any] = {
        "fatal_error": "",
        "fatal_kind": "",
    }

    def create_progress_handler(config: Any, show_log: bool = False):
        del config, show_log

        def handle_event(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            if event_type in ("progress_start", "progress_update", "progress_end"):
                state["overall"] = event.get("overall_progress", 0)
                state["stage"] = event.get("stage", "正在翻译")
                state["current"] = event.get("stage_current", 0)
                state["total"] = event.get("stage_total", 0)
                _emit(
                    "ZTF_PROGRESS ",
                    {
                        "overall": state["overall"],
                        "stage": state["stage"],
                        "current": state["current"],
                        "total": state["total"],
                        "requests": request_budget.requests,
                        "planned": request_budget.planned,
                    },
                    api_key,
                )
            elif event_type == "error":
                state["error"] = _redact(
                    sanitize_provider_error(
                        event.get("error", "BabelDOC reported an error.")
                    ),
                    api_key,
                )
            elif event_type == "finish":
                result = event.get("translate_result") or {}
                mono_path = ""
                if isinstance(result, dict):
                    mono_path = result.get("no_watermark_mono_pdf_path") or result.get("mono_pdf_path") or ""
                else:
                    mono_path = getattr(result, "no_watermark_mono_pdf_path", "") or getattr(
                        result, "mono_pdf_path", ""
                    )
                mono_path = str(mono_path) if mono_path else ""
                if mono_path:
                    state["result"] = mono_path

        return contextlib.nullcontext(), handle_event

    try:
        import babeldoc.main as babeldoc_main
        from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator

        babeldoc_main.create_progress_handler = create_progress_handler
        policy = TranslationPolicy()
        # BabelDOC pre-translates every paragraph before submitting any batch;
        # those preparation calls must not count as translation attempts.
        policy.track_pretranslate_attempts = False
        policy.instrument(ILTranslator)
        _install_min_request_patches(
            policy,
            request_budget,
            request_state,
            max_output_tokens,
        )
        _configure_logging(api_key, policy)
        _install_synchronous_log_capture(babeldoc_main, policy)

        try:
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            # Importers or test harnesses may have selected spawn already.
            if multiprocessing.get_start_method() != "spawn":
                raise

        babeldoc_main.cli()
        exit_code = 0
    except SystemExit as error:
        if error.code is None:
            exit_code = 0
        elif isinstance(error.code, int):
            exit_code = error.code
        else:
            print(_redact(error.code, api_key), file=sys.stderr, flush=True)
            exit_code = 1
    except BaseException:
        # Format before writing, then redact the entire traceback so secrets in
        # exception messages, chained causes, or source excerpts cannot escape.
        formatted_traceback = traceback.format_exc()
        print(_redact(formatted_traceback, api_key), file=sys.stderr, flush=True)
        exit_code = 1

    policy_instance = locals().get("policy")
    translation_count_summary = (
        policy_instance.count_summary() if policy_instance else ""
    )

    def with_translation_counts(message: str) -> str:
        if translation_count_summary and translation_count_summary not in message:
            return message + "；" + translation_count_summary
        return message

    if request_state.get("fatal_error"):
        _emit(
            "ZTF_ERROR ",
            {
                "message": with_translation_counts(request_state["fatal_error"]),
                "kind": request_state.get("fatal_kind") or "api_error",
                "requests": request_budget.requests,
                "planned": request_budget.planned,
            },
            api_key,
        )
        return 1

    # BabelDOC can swallow paragraph API errors and still emit a PDF and exit 0.
    decision = policy.decision() if "policy" in locals() else None
    if decision and decision["kind"] == "translation_failed":
        _emit(
            "ZTF_ERROR ",
            {
                "message": with_translation_counts(decision["message"]),
                "kind": decision["kind"],
                "failed_paragraphs": decision["failed_paragraphs"],
                "requests": request_budget.requests,
                "planned": request_budget.planned,
            },
            api_key,
        )
        return 1

    if state["error"]:
        _emit(
            "ZTF_ERROR ",
            {
                "message": with_translation_counts(state["error"]),
                "kind": state["error_kind"],
                "requests": request_budget.requests,
                "planned": request_budget.planned,
            },
            api_key,
        )
        return exit_code or 1

    if exit_code != 0 or not state["result"]:
        return exit_code or 1

    if decision and decision["kind"] == "warning":
        _emit(
            "ZTF_WARNING ",
            {
                "message": decision["message"],
                "failed_paragraphs": decision["failed_paragraphs"],
                "requests": request_budget.requests,
                "planned": request_budget.planned,
            },
            api_key,
        )
    _emit(
        "ZTF_RESULT ",
        {
            "mono": state["result"],
            "requests": request_budget.requests,
            "planned": request_budget.planned,
        },
        api_key,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
