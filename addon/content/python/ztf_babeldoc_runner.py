"""Run BabelDOC with the API key kept out of the operating-system command line."""

from __future__ import annotations

import contextlib
import copy
import datetime
import functools
import html
import importlib.metadata
import json
import logging
import logging.handlers
import math
import multiprocessing
import os
import re
import statistics
import sys
import threading
import traceback
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any


EXPECTED_BABELDOC_VERSION = "0.6.4"
OUTPUT_EXPANSION_FACTOR = 1.6
JSON_OUTPUT_OVERHEAD_TOKENS = 20
OUTPUT_HEADROOM_RATIO = 0.10
CONSISTENCY_SYSTEM_PROMPT = (
    "Translate the complete document as one unit. Translate technical terms, "
    "abbreviations, and named entities consistently across every item. Use the "
    "translation chosen at the first occurrence again for later occurrences. Keep "
    "abbreviations, formulas, citations, and placeholders unchanged. Keep person "
    "names, author lines, affiliations, e-mail addresses, URLs, dataset or attack "
    "labels and other identifiers as they are (they may be returned unchanged); "
    "translate every sentence of body text. An item with "
    "fragment_id is one part (fragment_part first or second) of a sentence split across "
    "a column or page break; continues_in / continued_from give the id of the other part "
    "when it is in the same request. Translate only that item's own fragment, so that "
    "each output covers exactly its own source text: do not move content between items, "
    "and never return an empty output. Return exactly the JSON array format required by the "
    "surrounding prompt, with one output item for each input item and no extra text."
)

_PLACEHOLDER_TOKEN_RE = re.compile(
    r"<\s*style\b(?=[^>]*\bid\s*=)[^>]*>|<\s*/\s*style\s*>|"
    r"\{\s*v\s*\d+\s*\}|\{[A-Za-z][A-Za-z0-9_]*\}|"
    r"%[sdif]|\[\[[^\]\r\n]+\]\]|%%[^%\r\n]+%%",
    re.I,
)
_STYLE_OPEN_PLACEHOLDER_RE = re.compile(
    r"<\s*style\b(?=[^>]*\bid\s*=)[^>]*>", re.I
)
_STYLE_OPEN_SUPPORTED_RE = re.compile(r"<style id='(?P<id>[^']+)'>")
_STYLE_ID_RE = re.compile(
    r"\bid\s*=\s*(?:'(?P<single>[^']+)'|\"(?P<double>[^\"]+)\"|(?P<bare>[^\s>]+))",
    re.I,
)
_STYLE_CLOSE_PLACEHOLDER_RE = re.compile(r"<\s*/\s*style\s*>", re.I)
_ANY_STYLE_TAG_RE = re.compile(r"<\s*/?\s*style\b[^>]*>", re.I)
_FORMULA_PLACEHOLDER_RE = re.compile(r"\{\s*v\s*(?P<id>\d+)\s*\}", re.I)
_REFERENCE_HEADING_RE = re.compile(
    # Section numbers may be Arabic ("7."), Roman ("VIII.", "VII") or a letter ("A.").
    r"^\s*(?:(?:\d+(?:\.\d+)*|[IVXLCDM]{1,8}|[A-Z])\s*[.)]?\s*)?"
    r"(?:references|bibliography|works\s+cited|参考文献|文献目录)\s*[:.]?\s*$",
    re.I,
)
_REFERENCE_LAYOUT_RE = re.compile(r"reference|bibliograph", re.I)
_REFERENCE_END_LAYOUT_RE = re.compile(
    r"appendix|acknowledg|author.?bio|biograph|about.?the.?author", re.I
)
# Section number prefix shared by start and end headings: "7.", "VIII.", "A.".
_SECTION_NUMBER_PREFIX = r"(?:(?:\d+(?:\.\d+)*|[IVXLCDM]{1,8}|[A-Z])\s*[.)]?\s+)?"
_REFERENCE_END_HEADING_RE = re.compile(
    r"^\s*" + _SECTION_NUMBER_PREFIX
    + r"(?:(?:appendix|appendices)(?:\s+[A-Z0-9一二三四五六七八九十]+)?(?:\s*[:.\-–—]?\s*[^\r\n]{0,120})?"
    r"|(?:acknowledgments?|acknowledgements?|author biographies|about the authors?|"
    r"biograph(?:y|ies)|附录|致谢|作者简介)(?:\s+[A-Z0-9一二三四五六七八九十]+)?\s*[:.]?)\s*$",
    re.I,
)
_REFERENCE_END_HEADING_DETAIL_RE = re.compile(
    r"^\s*" + _SECTION_NUMBER_PREFIX + r"(?:appendix|appendices|acknowledgments?|acknowledgements?|"
    r"author biographies|about the authors?|biography|biographies)\b"
    r"[^\r\n]{0,160}[:.)-]\s*\S",
    re.I,
)
_BODY_TEXT_LAYOUT_LABELS = {"text", "plain text", "paragraph_hybrid"}
_CAPTION_FRAGMENT_OWNER_LABELS = _BODY_TEXT_LAYOUT_LABELS | {
    "table_caption",
    "figure_caption",
}
# Ordinary word gaps are roughly 0.25-0.35 character heights; a table gutter
# must exceed one line height before a fallback line is split.
_TABLE_COLUMN_GAP_HEIGHT_MULTIPLIER = 1.0
# Leave a small inset so rendered glyph bounds do not touch table grid strokes.
_TABLE_CELL_BORDER_INSET = 0.5
_TABLE_CURVE_AXIS_TOLERANCE = 0.5
# A table paragraph under half the median height of its table's paragraphs is
# a fragment of a line (e.g. a stray comma split from its cell); retypeset at
# the table's font size its glyphs would overflow onto the neighbouring text.
_TABLE_FRAGMENT_HEIGHT_RATIO = 0.5
_PRESERVED_LAYOUT_RE = re.compile(
    r"header|footer|page.?number|running.?head|license|copyright|watermark|stamp|marginalia",
    re.I,
)
# Unambiguous licence-stamp sentences: preserved wherever they appear. Weaker
# phrases ("IEEE Xplore", "Downloaded on", "all rights reserved") also occur in
# body text, so they only count inside the page edge band (see below).
_LICENSE_STAMP_RE = re.compile(
    r"authorized\s+licensed\s+use\s+limited\s+to|personal\s+use\s+is\s+permitted,?\s+but|"
    r"permission\s+for\s+republication|licensed\s+for\s+use\s+by",
    re.I,
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


TOKEN_LIMIT_PARAMETERS = ("max_tokens", "max_completion_tokens")
_SAMPLING_PARAMETERS = ("temperature", "top_p", "presence_penalty", "frequency_penalty")
_ADAPTABLE_PARAMETERS = TOKEN_LIMIT_PARAMETERS + _SAMPLING_PARAMETERS
_REJECTED_PARAMETER_PATTERNS = (
    re.compile(r"\bunsupported\s+parameter\s*:\s*['\"]?([a-z_]+)['\"]?", re.I),
    re.compile(r"['\"`]?([a-z_]+)['\"`]?\s+(?:parameter\s+)?(?:is\s+)?not\s+supported\b", re.I),
    re.compile(r"\bdoes\s+not\s+support\s+(?:the\s+)?(?:parameter\s+)?['\"]?([a-z_]+)['\"]?", re.I),
)


def _rejected_parameter(message: str) -> str | None:
    """Return only a parameter named as rejected, never one suggested instead."""
    for pattern in _REJECTED_PARAMETER_PATTERNS:
        match = pattern.search(message)
        if match and match.group(1) in _ADAPTABLE_PARAMETERS:
            return match.group(1)
    return None


def parameter_adaptation(error: Any) -> str | None:
    """How to reshape a request the provider rejected *before* processing it.

    Newer models reject ``max_tokens`` ("Use 'max_completion_tokens' instead")
    or sampling parameters such as ``temperature``; such a 400 costs no quota.
    Returns "max_completion_tokens", "max_tokens" or "drop_sampling", or None
    for any other error (which is never retried).
    """
    if _provider_status(error) != 400:
        return None
    body = getattr(error, "body", None)
    details = body.get("error", body) if isinstance(body, dict) else {}
    details = details if isinstance(details, dict) else {}
    param = str(details.get("param") or "")
    code = str(details.get("code") or "")
    message = " ".join(
        str(part) for part in (details.get("message"), str(error)) if part
    ).casefold()
    unsupported = "unsupported" in code or "unsupported" in message or "not supported" in message
    if not unsupported:
        return None
    # The structured field is authoritative. Message fallback must identify
    # the rejected name, not a replacement mentioned later in the same text.
    if param == "max_tokens":
        return "max_completion_tokens"
    if param == "max_completion_tokens":
        return "max_tokens"
    if param in _SAMPLING_PARAMETERS:
        return "drop_sampling"
    rejected = _rejected_parameter(message)
    if rejected == "max_tokens":
        return "max_completion_tokens"
    if rejected == "max_completion_tokens":
        return "max_tokens"
    if rejected in _SAMPLING_PARAMETERS:
        return "drop_sampling"
    return None


def create_completion_adapting(
    client: Any,
    request_state: dict[str, Any],
    token_limit: int,
    **kwargs: Any,
) -> Any:
    """``client.chat.completions.create`` with the provider's parameter dialect.

    The token-limit parameter name lives in ``request_state["token_param"]``
    (initialised from the plugin setting). If the provider rejects a parameter
    as unsupported, the request is reshaped and sent again — at most twice, in
    the same counted request slot, since the rejected attempt was not processed.
    The learned dialect is kept for the rest of the job.
    """
    attempts = 0
    while True:
        call = dict(kwargs)
        if request_state.get("drop_sampling"):
            for name in _SAMPLING_PARAMETERS:
                call.pop(name, None)
        call[request_state.get("token_param", "max_tokens")] = token_limit
        try:
            return client.chat.completions.create(**call)
        except Exception as error:
            adaptation = parameter_adaptation(error)
            if adaptation is None or attempts >= 2:
                raise
            attempts += 1
            if adaptation == "drop_sampling":
                if request_state.get("drop_sampling"):
                    raise
                request_state["drop_sampling"] = True
            else:
                if request_state.get("token_param") == adaptation:
                    raise
                request_state["token_param"] = adaptation
            request_state.setdefault("parameter_adaptations", []).append(adaptation)


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


_LOG_ERROR_MAX_CHARS = 160
# Provider errors: fixed summary. Reasons: our own messages, which may have a
# provider detail appended, so they are bounded and cut before any body.
_LOG_ERROR_FIELDS = ("error",)
_LOG_REASON_FIELDS = ("reason", "repair_reason")


def _bounded_log_reason(value: Any, api_key: str) -> str:
    text = _mask_api_key(" ".join(str(value if value is not None else "").split()), api_key)
    body_start = re.search(r"[\[{<]", text)
    if body_start:
        text = text[: body_start.start()].rstrip(" -:;；，,") + " …"
    return text[:_LOG_ERROR_MAX_CHARS]


def _api_key_variants(api_key: str) -> list[str]:
    """The key as it may appear after JSON, repr or URL escaping by an SDK or server."""
    if not api_key:
        return []
    variants = {
        api_key,
        json.dumps(api_key)[1:-1],
        repr(api_key)[1:-1],
        api_key.replace("/", "\\/"),
    }
    try:
        from urllib.parse import quote

        variants.add(quote(api_key, safe=""))
    except Exception:
        pass
    return sorted((variant for variant in variants if variant), key=len, reverse=True)


def _mask_api_key(text: str, api_key: str) -> str:
    for variant in _api_key_variants(api_key):
        text = text.replace(variant, "[已隐藏]")
    return re.sub(r"(?i)\b(bearer)\s+\S+", r"\1 [已隐藏]", text)


_LOG_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:-]{1,80}")
_LOG_FREE_TEXT_MAX_CHARS = 80


def _safe_log_identifier(value: Any, api_key: str) -> str:
    """An id/type/code value, redacted, kept only if it looks like an identifier."""
    if not isinstance(value, str) or not value:
        return ""
    masked = _mask_api_key(value, api_key)
    if masked != value or not _LOG_SAFE_IDENTIFIER_RE.fullmatch(masked):
        # Anything that contained the key or is not a plain identifier may be
        # an echo of request content; record that it existed, not its value.
        return "[omitted]"
    return masked


def _provider_error_message(value: Any) -> str:
    """The provider's own error message (OpenAI-style body), if any."""
    body = getattr(value, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            message = error.get("message") or error.get("msg") or error.get("detail")
            if isinstance(message, str):
                return message
        elif isinstance(error, str):
            return error
    message = getattr(value, "message", None)
    return message if isinstance(message, str) else ""


# Fixed diagnostic categories. The provider's own wording is never logged:
# even a short excerpt can be document text echoed back, so only the
# category that matched is recorded.
_PROVIDER_MESSAGE_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("quota_or_rate_limit", re.compile(r"rate.?limit|too many requests|quota|insufficient|balance|余额|额度|限流|次数", re.I)),
    ("context_or_max_tokens", re.compile(r"max_tokens|max_completion_tokens|context.?length|maximum context|too long|token limit", re.I)),
    ("model_unavailable", re.compile(r"model.{0,40}(?:not found|does not exist|not support|unavailable|invalid)|no such model|模型.{0,10}(?:不存在|不支持)", re.I)),
    ("authentication", re.compile(r"api.?key|unauthori[sz]ed|authenticat|permission|forbidden|鉴权|令牌", re.I)),
    ("json_mode_unsupported", re.compile(r"response_format|json_object|json mode", re.I)),
    ("content_filtered", re.compile(r"content.?(?:policy|filter|management)|moderation|safety|sensitive|违规|审核", re.I)),
    ("timeout", re.compile(r"time(?:d)?.?out|超时", re.I)),
    ("invalid_request", re.compile(r"invalid|bad request|malformed|parameter|参数", re.I)),
)


def provider_message_for_log(value: Any, api_key: str, request_text: str) -> str:
    """A fixed diagnostic category for the provider's error message, or "".

    Free text from the provider is never logged (a 24-char echo test cannot
    prove a message is not document text); instead the message is classified
    into one of a few allow-listed categories. ``api_key`` and ``request_text``
    are accepted for interface stability; nothing from them is returned.
    """
    del api_key, request_text
    message = _provider_error_message(value)
    if not message:
        return ""
    for category, pattern in _PROVIDER_MESSAGE_CATEGORIES:
        if pattern.search(message):
            return category
    return "unclassified"


def log_safe_error(value: Any, api_key: str = "") -> str:
    """Controlled, bounded error summary for the diagnostics log.

    Provider responses may echo the request (document text, headers, key), in
    any field. Only a fixed summary is logged: HTTP status, identifier-shaped
    type/code/request id, and — for transport errors without an HTTP status
    and without any body — a short redacted message such as "Connection error.".
    Every value is redacted before it is combined or truncated.
    """
    status = _provider_status(value) if not isinstance(value, str) else None
    raw = _mask_api_key(str(value if value is not None else ""), api_key)
    if status is None:
        status_match = re.search(
            r"\b(?:HTTP(?:/\d(?:\.\d)?)?\s+|Error code:\s*|status(?:_code)?\s*[=: ]\s*)(\d{3})\b",
            raw,
            re.I,
        )
        if status_match:
            status = int(status_match.group(1))

    parts: list[str] = []
    if status:
        parts.append(f"HTTP {status}")
    for attribute in ("type", "code"):
        detail = _safe_log_identifier(getattr(value, attribute, None), api_key)
        if detail:
            parts.append(f"{attribute}={detail}")
    request_id = _safe_log_identifier(getattr(value, "request_id", None), api_key)
    if request_id:
        parts.append(f"request_id={request_id}")
    if _HTML_ERROR_MARKER.search(raw) or _embedded_html_error(value):
        # The HTML summary keeps only the page title and its request id.
        parts.append(_mask_api_key(sanitize_provider_error(value, status), api_key))
    elif status is None:
        first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        body_like = bool(re.search(r"[\[{<]", first_line))
        if first_line and not body_like and len(first_line) <= _LOG_FREE_TEXT_MAX_CHARS:
            parts.append(first_line)
        elif first_line:
            parts.append("provider error body omitted")
    if not isinstance(value, str):
        parts.append(type(value).__name__)
    text = "; ".join(part for part in parts if part) or "error"
    return text[:_LOG_ERROR_MAX_CHARS]


_PLACEHOLDER_DIAGNOSTIC_MAX_CHARS = 40


def _truncate_placeholder_diagnostics(record: Any) -> Any:
    """Shorten placeholder issue values for the log — after redaction, so a key
    inside a returned tag is masked in full before anything is cut."""
    def shorten(issues: Any) -> Any:
        if not isinstance(issues, list):
            return issues
        return [
            {
                key: value[:_PLACEHOLDER_DIAGNOSTIC_MAX_CHARS] if isinstance(value, str) else value
                for key, value in issue.items()
            }
            if isinstance(issue, dict)
            else issue
            for issue in issues
        ]

    if isinstance(record, dict):
        if "placeholder_issues" in record:
            record["placeholder_issues"] = shorten(record["placeholder_issues"])
        checks = record.get("checks")
        if isinstance(checks, dict) and "placeholder_issues" in checks:
            checks["placeholder_issues"] = shorten(checks["placeholder_issues"])
    return record


def _write_job_log(request_state: dict[str, Any], value: dict[str, Any], api_key: str = "") -> None:
    """Append one redacted JSONL record without allowing diagnostics to break translation."""
    log_path = request_state.get("job_log_path")
    if not log_path:
        return
    try:
        record = dict(value)
        for field in _LOG_ERROR_FIELDS:
            if record.get(field) is not None:
                record[field] = log_safe_error(str(record[field]), api_key)
        for field in _LOG_REASON_FIELDS:
            if record.get(field) is not None:
                record[field] = _bounded_log_reason(record[field], api_key)
        record.setdefault("timestamp", datetime.datetime.now(datetime.timezone.utc).isoformat())
        safe_record = _truncate_placeholder_diagnostics(_redact_payload(record, api_key))
        with open(str(log_path), "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(safe_record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # A full disk or an unavailable profile path must not abort the PDF job.
        return


def _text_preview(value: Any, api_key: str = "") -> str:
    # Redact before truncating so a key straddling the cut cannot leak in part.
    text = _mask_api_key(" ".join(str(value or "").split()), api_key)
    return text[:80]


def _expected_placeholder_counts(translate_input: Any) -> dict[str, int]:
    expected = Counter(
        getattr(translate_input, "original_placeholder_tokens", {}) or {}
    )
    placeholder_counts: Counter[str] = Counter()
    for placeholder in getattr(translate_input, "placeholders", ()) or ():
        token = getattr(placeholder, "placeholder", None)
        if token:
            placeholder_counts[token] += 1
        for attribute in ("left_placeholder", "right_placeholder"):
            token = getattr(placeholder, attribute, None)
            if token:
                placeholder_counts[token] += 1
    for token, count in placeholder_counts.items():
        expected[token] = max(expected[token], count)
    return {str(token): int(count) for token, count in expected.items() if count > 0}


def _active_translator_token_counter(
    request_state: dict[str, Any], fallback: Any = len
) -> Any:
    translator = request_state.get("active_llm_translator")
    token_counter = getattr(translator, "calc_token_count", None)
    return token_counter if callable(token_counter) else fallback


def _canonical_placeholder_token(token: str) -> str:
    """Identify BabelDOC markers semantically, allowing harmless tag spacing/case."""
    if _STYLE_CLOSE_PLACEHOLDER_RE.fullmatch(token):
        return "style-close"
    if _STYLE_OPEN_PLACEHOLDER_RE.fullmatch(token):
        match = _STYLE_OPEN_SUPPORTED_RE.fullmatch(token)
        if match:
            return "style-open:" + match.group("id").strip().casefold()
        # BabelDOC 0.6.4 only restores the exact single-quoted opener. Keep
        # other syntactically similar tags distinct so validation rejects them.
        style_id = _STYLE_ID_RE.search(token)
        if style_id:
            identifier = next(
                value for value in style_id.groupdict().values() if value is not None
            )
            return "unsupported-style-open:" + identifier.strip().casefold()
    formula_match = _FORMULA_PLACEHOLDER_RE.fullmatch(token)
    if formula_match:
        return "formula:v" + formula_match.group("id")
    return "literal:" + token


def _normalized_verbatim_parts(text: str) -> tuple[tuple[str, str], ...]:
    """Normalize whitespace and BabelDOC placeholders for an echo comparison."""
    parts: list[tuple[str, str]] = []
    start = 0
    for match in _PLACEHOLDER_TOKEN_RE.finditer(text):
        plain_text = " ".join(text[start : match.start()].split())
        if plain_text:
            parts.append(("text", plain_text))
        parts.append(("placeholder", _canonical_placeholder_token(match.group(0))))
        start = match.end()
    plain_text = " ".join(text[start:].split())
    if plain_text:
        parts.append(("text", plain_text))
    return tuple(parts)


def _is_verbatim_output(source: Any, translated: Any) -> bool:
    return (
        isinstance(source, str)
        and isinstance(translated, str)
        and _normalized_verbatim_parts(source) == _normalized_verbatim_parts(translated)
    )


def _apply_translation_output(
    source: Any,
    translated: Any,
    writeback: Any,
    *,
    model_output: Any = None,
) -> tuple[str, Any]:
    """Skip BabelDOC writeback for a canonical echo; otherwise apply it."""
    comparison_output = translated if model_output is None else model_output
    if _is_verbatim_output(source, comparison_output):
        return "kept_verbatim", False
    return "translated", writeback()


def _fragment_group_output_issues(
    input_items: list[dict[str, Any]],
    output_items: list[dict[str, Any]],
    token_counter: Any,
    expected_group_sizes: dict[str, int],
    group_outcomes: dict[str, dict[str, tuple[dict[str, Any], dict[str, Any]]]] | None = None,
    member_keys_by_item_id: dict[str, str] | None = None,
) -> dict[str, str]:
    """Accumulate fragment outcomes across requests and reject only actual failures."""
    if group_outcomes is None:
        group_outcomes = {}
    if member_keys_by_item_id is None:
        member_keys_by_item_id = {}
    outputs_by_id = {
        str(item.get("id")): item
        for item in output_items
        if isinstance(item, dict) and item.get("id") is not None
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in input_items:
        group_id = str(item.get("fragment_id") or "")
        if group_id:
            groups.setdefault(group_id, []).append(item)

    issues: dict[str, str] = {}
    for group_id, items in groups.items():
        failed = False
        for item in items:
            output = outputs_by_id.get(str(item.get("id")), {})
            translated = output.get("output")
            if output.get("_ztf_deferred"):
                # Continuation repacking will submit this item again; it is not
                # a missing translation outcome yet.
                continue
            if output.get("_ztf_failed"):
                failed = True
                break
            quality = validate_translation_output(
                str(item.get("input", "")), translated, item, token_counter
            )
            if not quality["usable"]:
                failed = True
                break
            member_key = member_keys_by_item_id.get(
                str(item.get("id")), str(item.get("id", ""))
            )
            stored_input = dict(item)
            stored_output = dict(output)
            stored_input["id"] = member_key
            stored_output["id"] = member_key
            group_outcomes.setdefault(group_id, {})[member_key] = (
                stored_input,
                stored_output,
            )
        if failed:
            issues[group_id] = "关联片段中存在空白或无效译文，已合并补译整组。"
            continue

        outcomes = group_outcomes.get(group_id, {})
        expected_size = expected_group_sizes.get(group_id, len(outcomes))
        if len(outcomes) < expected_size:
            continue
        # A complete group is accepted based on its per-item checks above.
        # Short fragments can legitimately expand, so output length alone does
        # not indicate that the model merged neighboring content.
    return issues


def _link_fragment_items(entries: list[tuple[dict[str, Any], Any, dict[str, Any]]]) -> None:
    """Point continues_in/continued_from at the sibling's item id in this request.

    ``entries`` holds (request item, paragraph key, metadata) for one request.
    The metadata carries the fragment group and the sibling's paragraph key;
    when the sibling is not part of the same request (main-pass split or a
    repair batch boundary), the link is omitted and only the part is stated.
    """
    item_id_by_key = {key: item.get("id") for item, key, _ in entries}
    for item, _key, metadata in entries:
        for field in ("continues_in", "continued_from", "fragment_part"):
            item.pop(field, None)
        if not metadata.get("fragment"):
            continue
        item["fragment_id"] = str(metadata.get("fragment_id"))
        part = metadata.get("fragment_part")
        if part:
            item["fragment_part"] = part
        sibling_id = item_id_by_key.get(metadata.get("fragment_sibling_paragraph"))
        if sibling_id is None:
            continue
        item["continues_in" if part == "first" else "continued_from"] = sibling_id


def _is_fragment_item(item: dict[str, Any]) -> bool:
    return bool(
        item.get("fragment_id") or item.get("continues_in") or item.get("continued_from")
    )


# The first-page author-block rule requires an e-mail address as evidence.
_EMAIL_EVIDENCE_PATTERN = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\}\s*@\s*[\w.-]+"
_EMAIL_EVIDENCE_RE = re.compile(_EMAIL_EVIDENCE_PATTERN)


_PROSE_MIN_LOWERCASE_WORDS = 4


def verbatim_output_allowed(text: str) -> bool:
    """Whether text may stay as it is (names, author/affiliation lines, labels,
    identifiers, links): only real sentences are checked for being left in
    English. A sentence has at least four plain lowercase words; tokens with
    ".", "@", digits or braces (e-mails, placeholders) do not count. The prompt
    tells the model to keep such items unchanged, so no further rules here."""
    # Count raw tokens: one carrying a placeholder ("mean{v1}") is not a word.
    # A trailing full stop ends a sentence ("design.") unless the word is a
    # short abbreviation ("avg.", "std.", "min.", "max."), which never counts.
    lowercase = []
    for token in (text or "").split():
        word = token.strip(",;:()[]\"'!?")
        if word.endswith(".") and not word.endswith(".."):
            word = word[:-1]
            if len(word) < 4:
                continue
        if re.fullmatch(r"[a-z]+(?:['’-][a-z]+)*", word):
            lowercase.append(word)
    return len(lowercase) < _PROSE_MIN_LOWERCASE_WORDS


def _is_first_page_author_block(paragraph: Any, page_index: int) -> bool:
    text = getattr(paragraph, "unicode", "") or ""
    has_email_evidence = bool(_EMAIL_EVIDENCE_RE.search(text))
    return (
        page_index == 0
        and has_email_evidence
        and verbatim_output_allowed(text)
    )


def _source_english_residue_ratio(source: str, translated: str) -> float:
    """Share of the source's ordinary English words that survive in the output.

    Only all-lowercase words count: abbreviations (HTTP, DNS), capitalized names
    and identifiers are legitimately kept verbatim in a Chinese translation, while
    an untranslated sentence still shows up through its ordinary words.
    """
    source_words = _ordinary_english_words(source)
    if len(source_words) < 8:
        return 0.0
    output_words = set(_ordinary_english_words(translated))
    return sum(word in output_words for word in source_words) / len(source_words)


_ORDINARY_WORD_RE = re.compile(r"(?<![A-Za-z])[a-z]{2,}(?![A-Za-z])")
# URLs, e-mail addresses (incl. "{a.b, c.d}@host" groups) and bare domains are
# kept verbatim in a translation; their lowercase pieces are not English prose.
_URL_EMAIL_DOMAIN_RE = re.compile(
    r"(?:https?://|ftp://|www\.)\S+"
    r"|\{[^{}]*\}\s*@\s*[\w.-]+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    # Bare domains by shape rather than a fixed TLD list: a path
    # ("kth.se/research"), several labels ("inf.ethz.ch"), a two-letter country
    # code ("ethz.ch") or a common generic TLD. A run-together sentence
    # ("datasets.Researchers") has none of these and is not matched.
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,24}/\S*"
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+){2,}\b"
    r"|\b[a-z0-9][a-z0-9-]*\.(?:[a-z]{2}|com|org|net|edu|gov|info|io|ai)\b",
)


def _ordinary_english_words(text: str) -> list[str]:
    """All-lowercase words of 2+ letters, ignoring placeholders, URLs and e-mails.

    The single word rule used by every English-residue check, so the checks
    cannot disagree (abbreviations, names, identifiers, links are excluded).
    """
    text = _PLACEHOLDER_TOKEN_RE.sub(" ", text or "")
    return _ORDINARY_WORD_RE.findall(_URL_EMAIL_DOMAIN_RE.sub(" ", text))


def _formula_placeholder_nesting(text: str) -> Counter[tuple[str, tuple[str, ...]]]:
    """Map each formula placeholder to the style spans that enclose it.

    BabelDOC 0.6.4 drops a formula when its placeholder is moved into (or out of)
    a <style id='…'>…</style> span, even though every token is still present once.
    """
    nesting: Counter[tuple[str, tuple[str, ...]]] = Counter()
    stack: list[str] = []
    for match in _PLACEHOLDER_TOKEN_RE.finditer(text or ""):
        canonical = _canonical_placeholder_token(match.group(0))
        if canonical.startswith(("style-open:", "unsupported-style-open:")):
            stack.append(canonical.split(":", 1)[1])
        elif canonical == "style-close":
            if stack:
                stack.pop()
        elif canonical.startswith("formula:"):
            nesting[(canonical, tuple(stack))] += 1
    return nesting


def validate_translation_output(
    source: str,
    translated: Any,
    item: dict[str, Any],
    token_counter: Any,
) -> dict[str, Any]:
    """Check output before BabelDOC can pass it to placeholder restoration/typesetting."""
    checks: dict[str, Any] = {
        "non_empty": isinstance(translated, str) and bool(translated.strip()),
        "kept_verbatim": False,
        "identical_english": False,
        "source_english_residue_ratio": 0.0,
        "length_ratio": None,
        "edit_distance": None,
        "placeholder_issues": [],
    }
    if not checks["non_empty"]:
        return {"usable": False, "reason": "模型返回空译文，已进入合并补译。", "checks": checks}

    output = str(translated)
    if _is_verbatim_output(source, output):
        checks["kept_verbatim"] = True
        return {"usable": True, "reason": "模型原样返回，保留原段落与排版。", "checks": checks}

    source_words = _ordinary_english_words(source)
    has_ordinary_words = len(source_words) >= 2
    # Identical output is fine only for abbreviations, names, numbers and
    # identifiers ("HTTP", "CIC-IDS-2017", a lone "pcap"); two or more ordinary
    # lowercase words ("of the system") mean the text was simply not translated.
    identical = " ".join(output.split()) == " ".join(source.split())
    # Label-like sources (names, author lines, labels) skip every
    # "left in English" heuristic; the prompt asks to keep them unchanged.
    label_like = verbatim_output_allowed(source)
    verbatim_ok = identical and label_like
    if identical and not verbatim_ok:
        checks["identical_english"] = True
        return {"usable": False, "reason": "模型返回了未翻译的英文原文，已进入合并补译。", "checks": checks}

    residue_ratio = _source_english_residue_ratio(source, output)
    checks["source_english_residue_ratio"] = round(residue_ratio, 4)
    plain_output = _PLACEHOLDER_TOKEN_RE.sub(" ", output)
    output_words = set(_ordinary_english_words(output))
    # No Chinese at all and every ordinary source word still there: an English
    # echo, even if only punctuation or spacing was changed.
    if (
        not label_like
        and has_ordinary_words
        and not re.search("[㐀-鿿]", plain_output)
        and all(word in output_words for word in source_words)
    ):
        checks["source_english_residue_ratio"] = 1.0
        return {
            "usable": False,
            "reason": "译文仍是英文原句，已进入合并补译。",
            "checks": checks,
        }
    if not label_like and residue_ratio >= 0.72:
        return {
            "usable": False,
            "reason": "译文仍保留大部分英文原句，已进入合并补译。",
            "checks": checks,
        }

    expected = item.get("required_placeholders") or []
    expected_counts: Counter[str] = Counter()
    expected_spellings: dict[str, str] = {}
    for entry in expected:
        token = str(entry.get("token", "")) if isinstance(entry, dict) else str(entry)
        count = int(entry.get("count", 1)) if isinstance(entry, dict) else 1
        if token:
            canonical = _canonical_placeholder_token(token)
            expected_counts[canonical] += count
            expected_spellings.setdefault(canonical, token)
    # Literal look-alikes in the source text ("%s", "{Alice}", "[[0]]") are not
    # BabelDOC placeholders and are not in required_placeholders; keeping them
    # verbatim is correct, so the source's own count is also expected.
    for token in _PLACEHOLDER_TOKEN_RE.findall(source):
        canonical = _canonical_placeholder_token(token)
        source_count = sum(
            1
            for other in _PLACEHOLDER_TOKEN_RE.findall(source)
            if _canonical_placeholder_token(other) == canonical
        )
        if source_count > expected_counts.get(canonical, 0):
            expected_counts[canonical] = source_count
            expected_spellings.setdefault(canonical, token)
    actual_counts: Counter[str] = Counter()
    actual_spellings: dict[str, str] = {}
    for token in _PLACEHOLDER_TOKEN_RE.findall(output):
        canonical = _canonical_placeholder_token(token)
        actual_counts[canonical] += 1
        actual_spellings.setdefault(canonical, token)
        if _STYLE_OPEN_PLACEHOLDER_RE.fullmatch(token):
            expected_spelling = expected_spellings.get(canonical)
            if token != expected_spelling:
                checks["placeholder_issues"].append(
                    {
                        "token": token,
                        "expected_spelling": expected_spelling,
                        "actual_spelling": token,
                        "reason": "unsupported_style_opener",
                    }
                )
    for canonical in expected_counts.keys() | actual_counts.keys():
        expected_count = expected_counts.get(canonical, 0)
        actual_count = actual_counts.get(canonical, 0)
        if actual_count != expected_count:
            checks["placeholder_issues"].append(
                {
                    "token": expected_spellings.get(canonical) or actual_spellings.get(canonical) or canonical,
                    "expected": expected_count,
                    "actual": actual_count,
                }
            )
    # A bare or malformed tag ("<style>", "<style class=x>") is not a BabelDOC
    # placeholder, so the token scan above misses it; BabelDOC would typeset it
    # as literal text. Any style tag beyond the source's own count is an issue.
    required_style_tags = sum(
        int(entry.get("count", 1)) if isinstance(entry, dict) else 1
        for entry in expected
        if _ANY_STYLE_TAG_RE.fullmatch(
            str(entry.get("token", "")) if isinstance(entry, dict) else str(entry)
        )
    )
    extra_style_tags = len(_ANY_STYLE_TAG_RE.findall(output)) - max(
        len(_ANY_STYLE_TAG_RE.findall(source)), required_style_tags
    )
    if extra_style_tags > 0:
        checks["placeholder_issues"].append(
            {"reason": "unexpected_style_tag", "extra": extra_style_tags}
        )
    if not checks["placeholder_issues"]:
        source_nesting = _formula_placeholder_nesting(source)
        output_nesting = _formula_placeholder_nesting(output)
        if source_nesting != output_nesting:
            for (canonical, enclosing), _ in (output_nesting - source_nesting).items():
                checks["placeholder_issues"].append(
                    {
                        "token": canonical,
                        "reason": "formula_nesting_changed",
                        "enclosing_styles": list(enclosing),
                    }
                )
            if not checks["placeholder_issues"]:
                checks["placeholder_issues"].append({"reason": "formula_nesting_changed"})
    if checks["placeholder_issues"]:
        return {
            "usable": False,
            "reason": "译文中的占位符缺失、重复、被改写或移动了位置，已进入合并补译。",
            "checks": checks,
        }

    # Confirmed verbatim content (links, short identifiers, evidenced bylines)
    # is identical by design; the length/edit-distance heuristics do not apply.
    # Placeholder checks above still ran.
    if not _is_fragment_item(item) and not label_like:
        try:
            input_tokens = max(1, int(token_counter(source)))
            output_tokens = max(0, int(token_counter(output)))
            ratio = output_tokens / input_tokens
            checks["length_ratio"] = ratio
            # For labels like "CSV Volume" (2 tokens) a correct translation can
            # easily exceed 3x; the ratio only means something for real text.
            if input_tokens > 4 and not (0.3 < ratio < 3.0):
                return {
                    "usable": False,
                    "reason": "译文长度异常，已进入合并补译。",
                    "checks": checks,
                }
        except Exception:
            pass

        if len(source) <= 4000 and len(output) <= 4000:
            similarity = SequenceMatcher(None, source, output, autojunk=False).ratio()
            distance = int(round((1.0 - similarity) * max(len(source), len(output))))
            checks["edit_distance"] = distance
            if distance < 5 and int(token_counter(source)) > 20:
                return {
                    "usable": False,
                    "reason": "译文与原文差异过小，已进入合并补译。",
                    "checks": checks,
                }

    return {"usable": True, "reason": "", "checks": checks}


def _paragraph_layout_label(paragraph: Any) -> str:
    return str(getattr(paragraph, "layout_label", "") or "").strip()


def _caption_fragment_edge_boxes(layout: Any, page: Any, layouts: list[Any]) -> list[Any] | None:
    """Return unowned edge runs from a broad fallback frame."""
    line_box = getattr(layout, "box", None)
    if line_box is None:
        return None
    characters = list(getattr(page, "pdf_character", ()) or ())
    if not characters:
        return [line_box]

    line_characters = []
    for char in characters:
        char_box = getattr(getattr(char, "visual_bbox", None), "box", None)
        if char_box is None:
            continue
        center_x = (char_box.x + char_box.x2) / 2
        center_y = (char_box.y + char_box.y2) / 2
        if line_box.x <= center_x <= line_box.x2 and line_box.y <= center_y <= line_box.y2:
            is_owned = any(
                getattr(owner, "class_name", "") in _CAPTION_FRAGMENT_OWNER_LABELS
                and owner is not layout
                and getattr(owner, "box", None) is not None
                and min(char_box.x2, owner.box.x2) > max(char_box.x, owner.box.x)
                and min(char_box.y2, owner.box.y2) > max(char_box.y, owner.box.y)
                for owner in layouts
            )
            line_characters.append((char_box, is_owned))
    if not line_characters:
        return None

    line_characters.sort(key=lambda item: item[0].x)
    unowned_groups: list[tuple[int, int, list[Any]]] = []
    for index, (char_box, is_owned) in enumerate(line_characters):
        if is_owned:
            continue
        if not unowned_groups or unowned_groups[-1][1] != index - 1:
            unowned_groups.append((index, index, [char_box]))
        else:
            start, _, group = unowned_groups[-1]
            group.append(char_box)
            unowned_groups[-1] = (start, index, group)

    if not unowned_groups:
        return None

    fragment_boxes = []
    for first_unowned, last_unowned, edge_group in unowned_groups:
        if first_unowned != 0 and last_unowned != len(line_characters) - 1:
            return None
        fragment_box = copy.copy(line_box)
        fragment_box.x = min(char_box.x for char_box in edge_group)
        fragment_box.y = min(char_box.y for char_box in edge_group)
        fragment_box.x2 = max(char_box.x2 for char_box in edge_group)
        fragment_box.y2 = max(char_box.y2 for char_box in edge_group)
        fragment_boxes.append(fragment_box)
    return fragment_boxes


def _caption_expansion_covers_body(caption_box: Any, fragment_boxes: list[Any], layouts: list[Any]) -> bool:
    expanded = copy.copy(caption_box)
    for fragment_box in fragment_boxes:
        expanded.x = min(expanded.x, fragment_box.x)
        expanded.y = min(expanded.y, fragment_box.y)
        expanded.x2 = max(expanded.x2, fragment_box.x2)
        expanded.y2 = max(expanded.y2, fragment_box.y2)

    def intersection_area(left: Any, right: Any) -> float:
        width = max(0, min(left.x2, right.x2) - max(left.x, right.x))
        height = max(0, min(left.y2, right.y2) - max(left.y, right.y))
        return width * height

    return any(
        str(getattr(owner, "class_name", "") or "").lower() in _BODY_TEXT_LAYOUT_LABELS
        and getattr(owner, "box", None) is not None
        and intersection_area(expanded, owner.box) > intersection_area(caption_box, owner.box)
        for owner in layouts
    )


def _repair_fallback_line_layouts(page: Any) -> None:
    """Split table fallback-line boxes at column gutters without changing chars."""
    layouts = list(getattr(page, "page_layout", ()) or ())
    table_layouts = [
        layout for layout in layouts if getattr(layout, "class_name", "") == "table"
    ]
    next_id = max(
        (int(getattr(layout, "id", 0) or 0) for layout in layouts),
        default=0,
    ) + 1
    repaired_layouts = []

    for layout in layouts:
        if getattr(layout, "class_name", "") != "fallback_line":
            repaired_layouts.append(layout)
            continue
        line_box = getattr(layout, "box", None)
        containing_tables = [
            table
            for table in table_layouts
            if (
                line_box is not None
                and table.box.x <= (line_box.x + line_box.x2) / 2 <= table.box.x2
            and table.box.y <= (line_box.y + line_box.y2) / 2 <= table.box.y2
            )
        ]
        if not containing_tables:
            repaired_layouts.append(layout)
            continue
        table = min(
            containing_tables,
            key=lambda item: (item.box.x2 - item.box.x) * (item.box.y2 - item.box.y),
        )

        line_characters = []
        for char in getattr(page, "pdf_character", ()) or ():
            char_box = getattr(getattr(char, "visual_bbox", None), "box", None)
            if char_box is None:
                continue
            center_x = (char_box.x + char_box.x2) / 2
            center_y = (char_box.y + char_box.y2) / 2
            if (
                line_box.x <= center_x <= line_box.x2
                and line_box.y <= center_y <= line_box.y2
            ):
                inside_table = (
                    table.box.x <= center_x <= table.box.x2
                    and table.box.y <= center_y <= table.box.y2
                )
                line_characters.append((char_box, char, inside_table))
        line_characters.sort(key=lambda item: item[0].x)
        line_height = line_box.y2 - line_box.y
        if (
            len(line_characters) < 2
            or not any(inside_table for _, _, inside_table in line_characters)
            or line_height <= 0
            or any(getattr(char, "vertical", False) for _, char, _ in line_characters)
        ):
            repaired_layouts.append(layout)
            continue

        gap_threshold = line_height * _TABLE_COLUMN_GAP_HEIGHT_MULTIPLIER
        segments: list[tuple[bool, list[Any]]] = []
        for char_box, _, inside_table in line_characters:
            gap = (
                char_box.x - segments[-1][1][-1].x2
                if segments
                else 0
            )
            if (
                not segments
                or segments[-1][0] != inside_table
                or (inside_table and gap > gap_threshold)
            ):
                segments.append((inside_table, []))
            segments[-1][1].append(char_box)
        outside_segment_indexes = [
            index for index, (inside_table, _) in enumerate(segments) if not inside_table
        ]
        table_segment_count = sum(inside_table for inside_table, _ in segments)
        if not outside_segment_indexes and table_segment_count <= 1:
            repaired_layouts.append(layout)
            continue

        original_segment_index = outside_segment_indexes[0] if outside_segment_indexes else 0
        for index, (_, segment) in enumerate(segments):
            x = min(char_box.x for char_box in segment)
            y = min(char_box.y for char_box in segment)
            x2 = max(char_box.x2 for char_box in segment)
            y2 = max(char_box.y2 for char_box in segment)
            target = layout if index == original_segment_index else copy.copy(layout)
            target.box = copy.copy(layout.box)
            target.box.x, target.box.y, target.box.x2, target.box.y2 = x, y, x2, y2
            if target is not layout:
                target.id = next_id
                next_id += 1
            repaired_layouts.append(target)

    removed_fallback_ids: set[int] = set()
    for layout in repaired_layouts:
        if getattr(layout, "class_name", "") != "fallback_line":
            continue
        line_box = layout.box
        fragment_boxes = _caption_fragment_edge_boxes(layout, page, repaired_layouts)
        if not fragment_boxes:
            continue
        line_height = line_box.y2 - line_box.y
        if line_height <= 0:
            continue

        matched_caption = None
        valid_fragments = True
        for fragment_box in fragment_boxes:
            center_x = (fragment_box.x + fragment_box.x2) / 2
            center_y = (fragment_box.y + fragment_box.y2) / 2
            if any(
                table.box.x <= center_x <= table.box.x2
                and table.box.y <= center_y <= table.box.y2
                for table in table_layouts
            ):
                valid_fragments = False
                break

            matches = []
            for caption in repaired_layouts:
                if getattr(caption, "class_name", "") not in {
                    "table_caption",
                    "figure_caption",
                }:
                    continue
                caption_box = caption.box
                vertical_overlap = max(
                    0,
                    min(fragment_box.y2, caption_box.y2)
                    - max(fragment_box.y, caption_box.y),
                )
                if vertical_overlap <= line_height * 0.5:
                    continue
                if any(
                    str(getattr(owner, "class_name", "") or "").lower()
                    in _BODY_TEXT_LAYOUT_LABELS
                    and min(fragment_box.x2, owner.box.x2)
                    > max(fragment_box.x, owner.box.x)
                    and min(fragment_box.y2, owner.box.y2)
                    > max(fragment_box.y, owner.box.y)
                    for owner in repaired_layouts
                    if owner is not caption and getattr(owner, "box", None) is not None
                ):
                    continue
                overlap_width = max(
                    0,
                    min(fragment_box.x2, caption_box.x2)
                    - max(fragment_box.x, caption_box.x),
                )
                fragment_width = fragment_box.x2 - fragment_box.x
                caption_width = caption_box.x2 - caption_box.x
                if (
                    min(fragment_width, caption_width) <= 0
                    or overlap_width > min(fragment_width, caption_width) * 0.5
                ):
                    # A small edge overlap is common when a model box misses the
                    # first few glyphs. A wrapped/covering frame has a large overlap.
                    continue
                horizontal_gap = max(
                    0,
                    caption_box.x - fragment_box.x2,
                    fragment_box.x - caption_box.x2,
                )
                if horizontal_gap < line_height:
                    matches.append((horizontal_gap, -vertical_overlap, caption))
            if not matches:
                valid_fragments = False
                break
            caption = min(matches, key=lambda item: (item[0], item[1]))[2]
            if matched_caption is not None and caption is not matched_caption:
                valid_fragments = False
                break
            matched_caption = caption

        if not valid_fragments or matched_caption is None:
            continue
        if _caption_expansion_covers_body(
            matched_caption.box,
            fragment_boxes,
            repaired_layouts,
        ):
            continue

        for fragment_box in fragment_boxes:
            matched_caption.box.x = min(matched_caption.box.x, fragment_box.x)
            matched_caption.box.y = min(matched_caption.box.y, fragment_box.y)
            matched_caption.box.x2 = max(matched_caption.box.x2, fragment_box.x2)
            matched_caption.box.y2 = max(matched_caption.box.y2, fragment_box.y2)
        removed_fallback_ids.add(id(layout))

    page.page_layout = [
        layout for layout in repaired_layouts if id(layout) not in removed_fallback_ids
    ]


def _paragraph_center_inside_table(paragraph: Any, page: Any) -> bool:
    paragraph_box = getattr(paragraph, "box", None)
    if paragraph_box is None:
        return False
    try:
        center_x = (float(paragraph_box.x) + float(paragraph_box.x2)) / 2
        center_y = (float(paragraph_box.y) + float(paragraph_box.y2)) / 2
    except (TypeError, ValueError, AttributeError):
        return False
    return any(
        getattr(layout, "class_name", "") == "table"
        and layout.box.x <= center_x <= layout.box.x2
        and layout.box.y <= center_y <= layout.box.y2
        for layout in getattr(page, "page_layout", ()) or ()
    )


def _table_cell_layout_box(paragraph_box: Any, page: Any, table: Any) -> Any | None:
    """Return the paragraph box widened only to its geometrically bounded cell."""
    table_box = getattr(table, "box", None)
    if paragraph_box is None or table_box is None:
        return None
    try:
        center_x = (float(paragraph_box.x) + float(paragraph_box.x2)) / 2
        center_y = (float(paragraph_box.y) + float(paragraph_box.y2)) / 2
        table_left = float(table_box.x)
        table_right = float(table_box.x2)
        table_top = float(table_box.y)
        table_bottom = float(table_box.y2)
    except (TypeError, ValueError, AttributeError):
        return None
    if not (table_left <= center_x <= table_right and table_top <= center_y <= table_bottom):
        return None

    x_boundaries = [table_left, table_right]
    minimum_line_length = max(3.0, (table_bottom - table_top) * 0.01)
    for curve in getattr(page, "pdf_curve", ()) or ():
        curve_box = getattr(curve, "box", None)
        if curve_box is None:
            continue
        try:
            x, y, x2, y2 = (
                float(curve_box.x),
                float(curve_box.y),
                float(curve_box.x2),
                float(curve_box.y2),
            )
        except (TypeError, ValueError, AttributeError):
            continue
        if (
            abs(x2 - x) > _TABLE_CURVE_AXIS_TOLERANCE
            or y2 - y < minimum_line_length
            or not table_left - _TABLE_CURVE_AXIS_TOLERANCE
            <= (x + x2) / 2
            <= table_right + _TABLE_CURVE_AXIS_TOLERANCE
            or center_y < y - _TABLE_CURVE_AXIS_TOLERANCE
            or center_y > y2 + _TABLE_CURVE_AXIS_TOLERANCE
        ):
            continue
        x_boundaries.append(min(table_right, max(table_left, (x + x2) / 2)))

    cell_left = max((x for x in x_boundaries if x <= center_x), default=table_left)
    cell_right = min((x for x in x_boundaries if x > center_x), default=table_right)
    try:
        bounded_box = copy.copy(paragraph_box)
        bounded_box.x = max(float(paragraph_box.x), cell_left + _TABLE_CELL_BORDER_INSET)
        bounded_box.x2 = cell_right - _TABLE_CELL_BORDER_INSET
        bounded_box.y = max(float(paragraph_box.y), table_top)
        bounded_box.y2 = min(float(paragraph_box.y2), table_bottom)
    except (TypeError, ValueError, AttributeError):
        return None
    if bounded_box.x2 <= bounded_box.x or bounded_box.y2 <= bounded_box.y:
        return None
    return bounded_box


def _with_table_cell_bounds(
    instance: Any,
    paragraph: Any,
    page: Any,
    operation: Any,
) -> Any:
    """Run scale measurement or rendering with table expansion clamped to a cell."""
    paragraph_box = getattr(paragraph, "box", None)
    if paragraph_box is None:
        return operation()
    try:
        center_x = (float(paragraph_box.x) + float(paragraph_box.x2)) / 2
        center_y = (float(paragraph_box.y) + float(paragraph_box.y2)) / 2
        containing_tables = [
            layout
            for layout in getattr(page, "page_layout", ()) or ()
            if getattr(layout, "class_name", "") == "table"
            and layout.box.x <= center_x <= layout.box.x2
            and layout.box.y <= center_y <= layout.box.y2
        ]
    except (TypeError, ValueError, AttributeError):
        return operation()
    if not containing_tables:
        return operation()
    table = min(
        containing_tables,
        key=lambda item: (item.box.x2 - item.box.x) * (item.box.y2 - item.box.y),
    )
    bounded_box = _table_cell_layout_box(paragraph_box, page, table)
    if bounded_box is None:
        return operation()

    original_box = paragraph_box
    instance_dict = getattr(instance, "__dict__", {})
    methods = {}
    for name in ("get_max_right_space", "get_max_bottom_space"):
        method = getattr(instance, name, None)
        if callable(method):
            methods[name] = (name in instance_dict, instance_dict.get(name), method)

    paragraph.box = bounded_box
    try:
        if "get_max_right_space" in methods:
            original_method = methods["get_max_right_space"][2]

            def bounded_right_space(
                current_box: Any,
                owner: Any,
                _original_method: Any = original_method,
            ) -> float:
                available = _original_method(current_box, owner)
                if owner is page:
                    return min(float(available), float(current_box.x2))
                return available

            instance.get_max_right_space = bounded_right_space
        if "get_max_bottom_space" in methods:
            original_method = methods["get_max_bottom_space"][2]

            def bounded_bottom_space(
                current_box: Any,
                owner: Any,
                _original_method: Any = original_method,
            ) -> float:
                available = _original_method(current_box, owner)
                if owner is page:
                    return max(float(available), float(current_box.y))
                return available

            instance.get_max_bottom_space = bounded_bottom_space
        return operation()
    finally:
        paragraph.box = original_box
        for name, (had_override, old_override, _) in methods.items():
            if had_override:
                setattr(instance, name, old_override)
            else:
                delattr(instance, name)


def _page_paragraphs_with_table_short_text(
    page: Any,
    min_text_length: int,
    translated_ids: set[int] | None,
    is_cid_paragraph: Any,
) -> tuple[list[Any], set[int], int]:
    """Keep short paragraphs only when their centers fall inside a table box."""
    translated_ids = translated_ids or set()
    paragraphs = []
    table_paragraph_ids = set()
    skipped_short = 0
    for paragraph in getattr(page, "pdf_paragraph", ()) or ():
        in_table = _paragraph_center_inside_table(paragraph, page)
        if in_table:
            table_paragraph_ids.add(id(paragraph))
        unicode = getattr(paragraph, "unicode", None)
        if (
            id(paragraph) in translated_ids
            or getattr(paragraph, "debug_id", None) is None
            or unicode is None
            or is_cid_paragraph(paragraph)
            or len(unicode) >= min_text_length
            or in_table
        ):
            paragraphs.append(paragraph)
        else:
            skipped_short += 1
    return paragraphs, table_paragraph_ids, skipped_short


def _pre_translate_with_table_min_text_length(
    original_pre_translate: Any,
    instance: Any,
    paragraph: Any,
    tracker: Any,
    page_font_map: Any,
    xobj_font_map: Any,
    *,
    is_table_paragraph: bool,
    min_text_length: int,
    config_lock: Any,
) -> Any:
    """Temporarily relax BabelDOC's length guard for a geometric table item."""
    with config_lock:
        unicode = getattr(paragraph, "unicode", None) or ""
        if len(unicode) < min_text_length and not is_table_paragraph:
            return None, None
        config = instance.translation_config
        current_minimum = config.min_text_length
        if len(unicode) < min_text_length and is_table_paragraph:
            config.min_text_length = 0
            try:
                return original_pre_translate(
                    instance, paragraph, tracker, page_font_map, xobj_font_map
                )
            finally:
                config.min_text_length = current_minimum
        return original_pre_translate(
            instance, paragraph, tracker, page_font_map, xobj_font_map
        )


def _apply_common_table_scales(
    instance: Any,
    deferred: list[tuple[Any, Any, Any]],
    translated_paragraph_ids: set[int],
) -> None:
    """Give translated paragraphs in each table one common final-box scale."""
    groups: dict[tuple[int, int], list[tuple[Any, float]]] = {}
    for paragraph, page, fonts in deferred:
        if id(paragraph) not in translated_paragraph_ids:
            continue
        if _is_independent_formula_paragraph(paragraph):
            continue
        box = getattr(paragraph, "box", None)
        if box is None:
            continue
        center_x = (box.x + box.x2) / 2
        center_y = (box.y + box.y2) / 2
        containing_tables = [
            layout
            for layout in getattr(page, "page_layout", ()) or ()
            if getattr(layout, "class_name", "") == "table"
            and layout.box.x <= center_x <= layout.box.x2
            and layout.box.y <= center_y <= layout.box.y2
        ]
        if not containing_tables:
            continue
        table = min(
            containing_tables,
            key=lambda item: (item.box.x2 - item.box.x) * (item.box.y2 - item.box.y),
        )
        try:
            units = instance.create_typesetting_units(paragraph, fonts)
            if all(unit.can_passthrough for unit in units):
                continue
            raw_scale = float(instance._get_optimal_scale(paragraph, page, units))
        except Exception:
            continue
        table_id = int(getattr(table, "id", id(table)))
        groups.setdefault((id(page), table_id), []).append((paragraph, raw_scale))

    for members in groups.values():
        common_scale = min(scale for _, scale in members)
        for paragraph, _ in members:
            paragraph.optimal_scale = common_scale


def collect_table_fragment_ids(document: Any) -> set[int]:
    """Table paragraphs far shorter than their table's lines, kept as they are."""
    fragment_ids: set[int] = set()
    for page in getattr(document, "page", ()) or ():
        tables = [
            layout
            for layout in getattr(page, "page_layout", ()) or ()
            if getattr(layout, "class_name", "") == "table"
        ]
        groups: dict[int, list[tuple[Any, float]]] = {}
        for paragraph in getattr(page, "pdf_paragraph", ()) or ():
            box = getattr(paragraph, "box", None)
            if box is None:
                continue
            center_x = (box.x + box.x2) / 2
            center_y = (box.y + box.y2) / 2
            containing = [
                table
                for table in tables
                if table.box.x <= center_x <= table.box.x2
                and table.box.y <= center_y <= table.box.y2
            ]
            if not containing:
                continue
            table = min(
                containing,
                key=lambda item: (item.box.x2 - item.box.x) * (item.box.y2 - item.box.y),
            )
            groups.setdefault(id(table), []).append((paragraph, box.y2 - box.y))
        for members in groups.values():
            median = statistics.median(height for _, height in members)
            for paragraph, height in members:
                if height < median * _TABLE_FRAGMENT_HEIGHT_RATIO:
                    fragment_ids.add(id(paragraph))
    return fragment_ids


def _is_body_text_paragraph(paragraph: Any) -> bool:
    return _paragraph_layout_label(paragraph).lower() in _BODY_TEXT_LAYOUT_LABELS


def _is_independent_formula_paragraph(paragraph: Any) -> bool:
    """Exclude paragraphs made only of formula compositions; include inline formulas."""
    compositions = list(getattr(paragraph, "pdf_paragraph_composition", ()) or ())
    return bool(compositions) and all(
        getattr(composition, "pdf_formula", None) is not None
        for composition in compositions
    )


def _paragraph_is_reference_heading(paragraph: Any) -> bool:
    return bool(_REFERENCE_HEADING_RE.match(str(getattr(paragraph, "unicode", "") or "")))


def collect_reference_paragraph_ids(document: Any, translate_references: bool) -> set[int]:
    """Keep a detected bibliography region out of translation unless opted in."""
    if translate_references:
        return set()
    references_started = False
    reference_ids: set[int] = set()
    for page in getattr(document, "page", ()) or ():
        for paragraph in getattr(page, "pdf_paragraph", ()) or ():
            label = _paragraph_layout_label(paragraph)
            heading = str(getattr(paragraph, "unicode", "") or "")
            if references_started and (
                _REFERENCE_END_LAYOUT_RE.search(label)
                or _REFERENCE_END_HEADING_RE.match(heading)
                or _REFERENCE_END_HEADING_DETAIL_RE.match(heading)
            ):
                references_started = False
            if _REFERENCE_LAYOUT_RE.search(label) or _paragraph_is_reference_heading(paragraph):
                references_started = True
            if references_started:
                reference_ids.add(id(paragraph))
    return reference_ids


def _is_in_page_edge_band(paragraph: Any, page: Any, band: float = 0.12) -> bool:
    """True when the whole paragraph lies within the top or bottom ``band``.

    Merely touching the band is not enough: a body paragraph that starts near
    the top of a column must not be treated as a running header or stamp.
    """
    box = getattr(paragraph, "box", None)
    page_box = getattr(getattr(page, "cropbox", None), "box", None) or getattr(
        getattr(page, "mediabox", None), "box", None
    )
    try:
        low = float(getattr(page_box, "y", 0.0))
        high = float(getattr(page_box, "y2"))
        height = high - low
        if height <= 0 or box is None:
            return False
        # PDF y grows upward: box.y is the paragraph's lower edge, box.y2 its upper edge.
        inside_bottom_band = (float(box.y2) - low) / height <= band
        inside_top_band = (high - float(box.y)) / height <= band
        return inside_bottom_band or inside_top_band
    except (TypeError, ValueError, AttributeError):
        return False


def _normalized_margin_text(text: str) -> str:
    # Page numbers and dates differ per page; compare the rest of the line.
    return re.sub(r"\d+", "#", " ".join(text.split()).casefold())


def collect_repeated_marginal_ids(document: Any, min_pages: int = 2) -> set[int]:
    """Find running headers/footers that BabelDOC labels as ordinary body text.

    Short text in the top/bottom edge band whose digit-normalized form appears
    on at least ``max(min_pages, 2, ceil(page_count / 3))`` different pages is
    a running header or footer.
    """
    pages = getattr(document, "page", ()) or ()
    required_pages = max(min_pages, 2, math.ceil(len(pages) / 3))
    pages_by_text: dict[str, set[int]] = {}
    candidates: list[tuple[str, int]] = []
    for page_index, page in enumerate(pages):
        for paragraph in getattr(page, "pdf_paragraph", ()) or ():
            text = str(getattr(paragraph, "unicode", "") or "")
            if not text.strip() or len(text) > 200:
                continue
            if not _is_in_page_edge_band(paragraph, page):
                continue
            key = _normalized_margin_text(text)
            pages_by_text.setdefault(key, set()).add(page_index)
            candidates.append((key, id(paragraph)))
    return {
        paragraph_id
        for key, paragraph_id in candidates
        if len(pages_by_text.get(key, ())) >= required_pages
    }


_EDGE_STAMP_PHRASE_RE = re.compile(
    r"\bdoi\s*:\s*\S|\bisbn(?:-1[03])?\s*:?\s*[\d-]{10,}|©|\(c\)\s*\d{4}|"
    r"\bcopyright\s+(?:©|\(c\)|\d{4})|\ball rights reserved\b|"
    r"\bdownloaded\s+on\s+\w+\s+\d|\bfrom\s+ieee\s+xplore\b|\brestrictions\s+apply\b|"
    r"\bauthorized\s+licensed\s+use\b",
    re.I,
)


def _paragraph_is_marginal_or_stamp(paragraph: Any, page: Any) -> bool:
    text = " ".join(str(getattr(paragraph, "unicode", "") or "").split())
    label = _paragraph_layout_label(paragraph)
    if _PRESERVED_LAYOUT_RE.search(label):
        return True
    if not text:
        return False
    if _LICENSE_STAMP_RE.search(text):
        return True

    edge_text = _is_in_page_edge_band(paragraph, page)

    if re.fullmatch(r"(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?", text, re.I):
        return True
    # Only stamp phrasing, not single words: "We downloaded the datasets…" or
    # "We searched IEEE Xplore…" near a page edge is still body text.
    if edge_text and len(text) <= 280 and _EDGE_STAMP_PHRASE_RE.search(text):
        return True
    return False


def body_font_scale_plan(raw_scales: list[float]) -> tuple[float, list[float], int]:
    """Give a font-size class one common scale, keeping below-floor overflow individual."""
    if not raw_scales:
        return 1.0, [], 0
    normalized = [float(scale) for scale in raw_scales]
    common_scale = min(max(0.8, min(1.0, scale)) for scale in normalized)
    final_scales = [scale if scale < 0.8 else common_scale for scale in normalized]
    overflow_count = sum(scale < 0.8 for scale in normalized)
    return common_scale, final_scales, overflow_count


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


def snapshot_paragraph_state(document: Any) -> dict[int, tuple[Any, Any]]:
    """Each paragraph's text and composition before translation, keyed by id()."""
    return {
        id(paragraph): (paragraph.unicode, paragraph.pdf_paragraph_composition)
        for page in document.page
        for paragraph in page.pdf_paragraph
    }


def classify_paragraphs(
    document: Any, original: dict[int, tuple[Any, Any]], api_key: str = ""
) -> list[dict[str, Any]]:
    """Whether each paragraph ended with model output written back.

    BabelDOC's post_translate_paragraph is the only step before typesetting
    that replaces pdf_paragraph_composition, and rollbacks restore the original
    object, so a paragraph still holding its original composition keeps its
    original glyphs and layout. Text equality is not enough: BabelDOC also
    writes back an output identical to the paragraph's text.
    """
    records = []
    for page_index, page in enumerate(document.page):
        for paragraph_index, paragraph in enumerate(page.pdf_paragraph):
            text, composition = original.get(
                id(paragraph), (paragraph.unicode, paragraph.pdf_paragraph_composition)
            )
            records.append(
                {
                    "page": page_index + 1,
                    "index": paragraph_index,
                    "preview": _text_preview(text, api_key)[:60],
                    "decision": "kept"
                    if paragraph.pdf_paragraph_composition is composition
                    else "translated",
                }
            )
    return records


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


def rollback_fragment_group(
    policy: Any,
    request_state: dict[str, Any],
    group_members: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    reason: str,
    exempt_failure_ids: set[int] | None = None,
) -> set[int]:
    """Restore a linked group, excluding verbatim members from failure counts."""
    if exempt_failure_ids is None:
        exempt_failure_ids = set()
    paragraph_ids = {
        id(member["paragraph"])
        for member in group_members
        if member.get("paragraph") is not None
    }
    restore_paragraph_snapshots(
        request_state.get("document_paragraph_snapshots", {}), paragraph_ids
    )
    request_state.setdefault("translated_paragraph_ids", set()).difference_update(
        paragraph_ids
    )
    request_state.setdefault("final_fragment_failure_ids", set()).update(paragraph_ids)
    for member in group_members:
        paragraph = member.get("paragraph")
        if paragraph is not None and id(paragraph) not in exempt_failure_ids:
            policy.record_failure(paragraph, reason)
    return paragraph_ids


def _clear_unkept_fragment_outputs(
    accepted_outputs: dict[int, dict[str, Any]],
    member_ids: set[int],
    kept_verbatim_ids: set[int],
) -> None:
    for member_id in member_ids - kept_verbatim_ids:
        accepted_outputs.pop(member_id, None)


def _mark_paragraph_kept_verbatim(
    request_state: dict[str, Any], policy: Any, paragraph: Any
) -> None:
    if paragraph is None:
        return
    paragraph_id = id(paragraph)
    request_state.setdefault("kept_verbatim_paragraph_ids", set()).add(paragraph_id)
    request_state.get("final_fragment_failure_ids", set()).discard(paragraph_id)
    request_state.get("translated_paragraph_ids", set()).discard(paragraph_id)
    policy.clear_failure(paragraph)


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
    stop_when_capped: bool = False,
    single_pass: bool = False,
    on_batch_error: Any = None,
) -> list[dict[str, Any]]:
    """Translate from one ordered queue, returning only unconfirmed items to its head.

    When ``defer_remaining`` is supplied, this function handles exactly one request;
    the document executor owns the shared queue and will repack the returned suffix
    together with all later paragraphs. Without it, this function drives that same
    queue itself. A zero-prefix truncation only sets a half-size cap for the next
    request; it never sends both halves as pre-formed child batches.

    ``single_pass`` (repair): every item is submitted at most once — after a
    truncated response the unconfirmed items are failed, never re-sent.
    ``on_batch_error`` (repair): a request error fails only that batch and the
    loop continues, so earlier batches keep their results; the callback gets
    the exception. A request-cap stop still ends the loop.
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

        if budget.requests >= budget.max_requests and stop_when_capped:
            results_by_id.update(
                {
                    str(result.get("id")): result
                    for result in _failed_json_items(
                        pending, "请求上限已用完，补译未发送，已保留原文。"
                    )
                }
            )
            break
        if budget.planned <= budget.requests:
            budget.add_additional_requests(1)
        if on_batch_error is None:
            response = request_once(budget, lambda: request(batch))
        else:
            try:
                response = request_once(budget, lambda: request(batch))
            except RequestCapExceeded:
                results_by_id.update(
                    {
                        str(result.get("id")): result
                        for result in _failed_json_items(
                            pending, "请求上限已用完，补译未发送，已保留原文。"
                        )
                    }
                )
                break
            except Exception as error:
                on_batch_error(error)
                results_by_id.update(
                    {
                        str(result.get("id")): result
                        for result in _failed_json_items(
                            batch, "该批补译请求失败，已保留原文。"
                        )
                    }
                )
                pending = pending[len(batch) :]
                continue
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

        if single_pass:
            # Repair sends each item once: what the truncated answer did not
            # confirm keeps its original text instead of being re-sent.
            results_by_id.update(
                {
                    str(result.get("id")): result
                    for result in _failed_json_items(
                        unresolved_request_items,
                        "补译输出被截断，未确认的段落保留原文。",
                    )
                }
            )
            pending = pending[len(batch) :]
            continue

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
    for variant in _api_key_variants(api_key):
        text = text.replace(variant, "[已隐藏]")
    return text


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

    def clear_failure(self, paragraph: Any) -> None:
        """Remove a provisional failure once a later model echo keeps the source."""
        if paragraph is None:
            return
        with self._lock:
            self._failed_paragraph_ids.discard(id(paragraph))
            self.failed_paragraphs = (
                len(self._failed_paragraph_ids) + self._unbound_failed_paragraphs
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


def _read_boolean_setting(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def ensure_runner_options(arguments: list[str]) -> list[str]:
    """Always disable extra glossary/fallback calls and use this runner's prompt.

    The consistency + fragment prompt is owned here; any --custom-system-prompt
    passed by the caller (e.g. an older plugin build) is replaced, never kept,
    so the fragment rules cannot be lost.
    """
    result: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--custom-system-prompt":
            skip_next = True
            continue
        if argument.startswith("--custom-system-prompt="):
            continue
        result.append(argument)
    for flag in ("--no-auto-extract-glossary", "--disable-same-text-fallback"):
        if flag not in result:
            result.append(flag)
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


def _normalize_cjk_tounicode(data: bytes) -> bytes:
    """Patch single-character destinations in the backend's CMap records.

    Keep unrelated entries (including multi-character destinations) verbatim.
    This deliberately handles bfchar, scalar ranges and array ranges,
    rather than parsing arbitrary source-PDF CMaps.
    """
    def destination(value: bytes) -> bytes:
        # MuPDF also emits five-digit scalar values for supplementary glyphs.
        if len(value) == 5:
            code = int(value, 16)
            if 0x2F800 <= code <= 0x2FA1F:
                return unicodedata.normalize("NFD", chr(code)).encode("utf-16-be").hex().encode()
            return value
        if len(value) % 4:
            return value
        raw = bytes.fromhex(value.decode("ascii"))
        try:
            char = raw.decode("utf-16-be")
        except UnicodeDecodeError:
            return value
        if len(char) == 1 and (
            0xF900 <= ord(char) <= 0xFAFF or 0x2F800 <= ord(char) <= 0x2FA1F
        ):
            return unicodedata.normalize("NFD", char).encode("utf-16-be").hex().encode()
        return value

    def block(match: re.Match[bytes]) -> bytes:
        kind, body = match.group("kind", "body")
        if kind == b"bfchar":
            changed = re.sub(
                rb"(?m)^(\s*<[0-9a-fA-F]+>\s*)<([0-9a-fA-F]+)>([ \t]*\r?$)",
                lambda entry: entry[1] + b"<" + destination(entry[2]) + b">" + entry[3],
                body,
            )
            return match[0].replace(body, changed, 1)

        def ranges(entry: re.Match[bytes]) -> list[bytes]:
            first, last, value, array = entry.group(1, 2, 3, 4)
            if array is not None:
                changed = re.sub(
                    rb"<([0-9a-fA-F]+)>",
                    lambda item: b"<" + destination(item[1]) + b">", array,
                )
                return [entry[0].replace(array, changed, 1)]
            start, stop = int(first, 16), int(last, 16)
            # Generated Identity-H fonts use two-byte CIDs. Leave other forms alone.
            if len(first) != 4 or len(last) != 4 or (len(value) != 5 and len(value) % 4):
                return [entry[0]]
            base = int(value, 16)
            lines = []
            run = start
            for cid in range(start, stop + 1):
                original = f"{base + cid - start:0{len(value)}x}".encode()
                normalized = destination(original)
                if normalized.lower() == original.lower():
                    continue
                if run < cid:
                    lines.append(f"<{run:04x}><{cid - 1:04x}><{base + run - start:0{len(value)}x}>")
                lines.append(f"<{cid:04x}><{cid:04x}><{normalized.decode('ascii')}>")
                run = cid + 1
            if not lines:
                return [entry[0]]
            if run <= stop:
                lines.append(f"<{run:04x}><{stop:04x}><{base + run - start:0{len(value)}x}>")
            return [line.encode() for line in lines]

        records = []
        changed = False
        cursor = 0
        for entry in re.finditer(
            rb"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*"
            rb"(?:<([0-9a-fA-F]+)>|(\[(?:\s*<[0-9a-fA-F]+>)*\s*\]))",
            body,
        ):
            # Keep an unsupported block intact instead of dropping its contents.
            if body[cursor:entry.start()].strip():
                return match[0]
            rewritten = ranges(entry)
            records.extend(rewritten)
            changed |= rewritten != [entry[0]]
            cursor = entry.end()
        if not changed or body[cursor:].strip():
            return match[0]
        return b"\n".join(
            str(len(records[offset:offset + 100])).encode() + b" beginbfrange\n"
            + b"\n".join(records[offset:offset + 100]) + b"\nendbfrange"
            for offset in range(0, len(records), 100)
        )

    return re.sub(
        rb"\d+ begin(?P<kind>bfchar|bfrange)\s*\n(?P<body>.*?)end(?P=kind)",
        block, data, flags=re.S,
    )


def _install_pdf_unicode_patch() -> None:
    from babeldoc.assets.embedding_assets_metadata import FONT_NAMES
    from babeldoc.format.pdf import high_level

    def reproduce_cmap(doc: Any) -> Any:
        seen = set()
        for page in doc:
            for font in page.get_fonts():
                name = re.sub(r"^[A-Z]{6}\+", "", font[3])
                if font[1] != "ttf" or name not in FONT_NAMES or ".ttf" not in font[4]:
                    continue
                kind, value = doc.xref_get_key(font[0], "ToUnicode")
                if kind != "xref":
                    continue
                xref = int(value.split()[0])
                if xref in seen:
                    continue
                seen.add(xref)
                original = doc.xref_stream(xref)
                normalized = _normalize_cjk_tounicode(original)
                if normalized != original:
                    doc.update_stream(xref, normalized)
        return doc

    # fix_cmap already saves to a working file before replacing each output PDF.
    high_level.reproduce_cmap = reproduce_cmap


def _install_min_request_patches(
    policy: TranslationPolicy,
    request_budget: RequestBudget,
    request_state: dict[str, Any],
    max_output_tokens: int,
    repair_failed: bool,
    translate_references: bool,
    api_key: str,
) -> None:
    """Batch the complete document, count requests, and suppress fallback calls."""
    import openai
    import babeldoc.format.pdf.document_il.midend.il_translator_llm_only as llm_module
    import babeldoc.translator.translator as translator_module
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
    from babeldoc.translator.translator import OpenAITranslator

    _install_pdf_unicode_patch()
    original_openai = openai.OpenAI

    original_post_translate_paragraph = ILTranslator.post_translate_paragraph

    def log_applied(
        paragraph: Any,
        overridden: bool,
        outcome: str = "translated",
    ) -> None:
        accepted = request_state.get("accepted_outputs", {}).pop(id(paragraph), None)
        if accepted is None:
            return
        metadata = accepted.get("metadata") or {}
        _write_job_log(
            request_state,
            {
                "event": "item_outcome",
                "phase": "main",
                "id": accepted.get("item_id"),
                "outcome": outcome,
                "reason": "模型原样返回，保留原段落与排版。" if outcome == "kept_verbatim" else "",
                "babeldoc_check_overridden": overridden,
                "fragment": bool(metadata.get("fragment")),
                "fragment_type": metadata.get("fragment_type"),
                "fragment_id": metadata.get("fragment_id"),
                "preview": _text_preview(accepted.get("source", ""), api_key),
                "output_preview": _text_preview(accepted.get("text", ""), api_key),
            },
            api_key,
        )

    @functools.wraps(original_post_translate_paragraph)
    def post_translate_paragraph_with_log(instance: Any, paragraph: Any, *args: Any, **kwargs: Any) -> Any:
        translate_input = args[1] if len(args) > 1 else kwargs.get("translate_input")
        translated_text = args[2] if len(args) > 2 else kwargs.get("translated_text")
        source = getattr(translate_input, "unicode", None)
        accepted = request_state.get("accepted_outputs", {}).get(id(paragraph))
        if accepted is None:
            return original_post_translate_paragraph(
                instance, paragraph, *args, **kwargs
            )
        outcome, result = _apply_translation_output(
            source,
            translated_text,
            lambda: original_post_translate_paragraph(instance, paragraph, *args, **kwargs),
            model_output=accepted.get("text"),
        )
        if outcome == "kept_verbatim":
            _mark_paragraph_kept_verbatim(request_state, policy, paragraph)
        log_applied(paragraph, overridden=False, outcome=outcome)
        return result

    ILTranslator.post_translate_paragraph = post_translate_paragraph_with_log

    original_generate_fallback_line_layouts = (
        LayoutParser.generate_fallback_line_layout_for_page
    )
    original_process_page = llm_module.ILTranslatorLLMOnly.process_page
    table_min_text_lock = threading.RLock()

    @functools.wraps(original_generate_fallback_line_layouts)
    def generate_fallback_line_layouts_with_table_repair(
        instance: Any, page: Any, progress: Any
    ) -> Any:
        result = original_generate_fallback_line_layouts(instance, page, progress)
        _repair_fallback_line_layouts(page)
        return result

    LayoutParser.generate_fallback_line_layout_for_page = (
        generate_fallback_line_layouts_with_table_repair
    )

    @functools.wraps(original_process_page)
    def process_page_with_table_short_paragraphs(
        instance: Any,
        page: Any,
        executor: Any,
        pbar: Any = None,
        tracker: Any = None,
        executor2: Any = None,
        translated_ids: set[int] | None = None,
    ) -> Any:
        config = instance.translation_config
        with table_min_text_lock:
            minimum = config.min_text_length
            paragraphs, table_ids, skipped_short = (
                _page_paragraphs_with_table_short_text(
                    page,
                    minimum,
                    translated_ids,
                    llm_module.is_cid_paragraph,
                )
            )
            request_state.setdefault("table_paragraph_ids", set()).update(table_ids)
            original_paragraphs = page.pdf_paragraph
            page.pdf_paragraph = paragraphs
            current_minimum = config.min_text_length
            try:
                # The geometry filter above has removed every short paragraph
                # outside table boxes before BabelDOC applies its page gate.
                config.min_text_length = 0
                result = original_process_page(
                    instance,
                    page,
                    executor,
                    pbar,
                    tracker,
                    executor2,
                    translated_ids,
                )
            finally:
                config.min_text_length = current_minimum
                page.pdf_paragraph = original_paragraphs
        if skipped_short and pbar is not None and hasattr(pbar, "advance"):
            pbar.advance(skipped_short)
        return result

    llm_module.ILTranslatorLLMOnly.process_page = process_page_with_table_short_paragraphs

    def apply_accepted_after_babeldoc_rejection(owner: Any, args: tuple[Any, ...]) -> bool:
        """BabelDOC re-checks length after our validation and hands rejected
        paragraphs to its fallback translator. Our checks are authoritative
        (e.g. "Recall" → "召回率" is 1→3 tokens, valid but rejected by BabelDOC),
        so apply the accepted text directly; if that fails, queue repair.
        Returns True when the paragraph was handled here."""
        paragraph = args[0] if args else None
        accepted = request_state.get("accepted_outputs", {}).get(id(paragraph))
        if paragraph is None or accepted is None:
            return False
        tracker = args[3] if len(args) > 3 else accepted.get("tracker")
        pbar = args[2] if len(args) > 2 else None
        try:
            translate_input = accepted["translate_input"]
            outcome, _ = _apply_translation_output(
                getattr(translate_input, "unicode", None),
                accepted["text"],
                lambda: original_post_translate_paragraph(
                    owner, paragraph, tracker, translate_input, accepted["text"]
                ),
            )
            if outcome == "kept_verbatim":
                _mark_paragraph_kept_verbatim(request_state, policy, paragraph)
        except Exception as error:
            request_state.get("accepted_outputs", {}).pop(id(paragraph), None)
            queue_unapplied_for_repair(paragraph, accepted, error)
        else:
            log_applied(paragraph, overridden=True, outcome=outcome)
        if pbar is not None and hasattr(pbar, "advance"):
            pbar.advance(1)
        return True

    def queue_unapplied_for_repair(paragraph: Any, accepted: dict[str, Any], error: Exception) -> None:
        reason = "译文无法应用到段落，已进入合并补译。" if repair_failed else "译文无法应用到段落，已保留原文。"
        metadata = accepted.get("metadata") or {}
        request_state.get("translated_paragraph_ids", set()).discard(id(paragraph))
        _write_job_log(
            request_state,
            {
                "event": "item_outcome",
                "phase": "main",
                "id": accepted.get("item_id"),
                "outcome": "needs_repair" if repair_failed else "retained_source",
                "reason": reason,
                "checks": {"apply_error": log_safe_error(error, api_key)},
                "fragment": bool(metadata.get("fragment")),
                "fragment_type": metadata.get("fragment_type"),
                "fragment_id": metadata.get("fragment_id"),
                "preview": _text_preview(accepted.get("source", ""), api_key),
                "output_preview": "",
            },
            api_key,
        )
        if not repair_failed or request_state.get("fatal_error"):
            policy.record_failure(paragraph, reason)
            return
        request_state.setdefault("failed_batch_paragraph_ids", set()).add(id(paragraph))
        request_state.setdefault("repair_records", {})[id(paragraph)] = {
            "paragraph": paragraph,
            "translate_input": accepted.get("translate_input"),
            "tracker": accepted.get("tracker"),
            "source": accepted.get("source", ""),
            "reason": reason,
            "checks": {"apply_error": True},
            "metadata": metadata,
            "layout_label": _paragraph_layout_label(paragraph),
            "fragment": bool(metadata.get("fragment")),
            "repair_status": "pending",
        }
        group_id = metadata.get("fragment_id")
        if group_id:
            # A fragment pair is one unit: undo an already-applied sibling so the
            # repair pass (which adds every member of an invalid group) rewrites both.
            request_state.setdefault("invalid_fragment_groups", {}).setdefault(str(group_id), reason)
            member_ids = {
                id(member["paragraph"])
                for member in request_state.get("fragment_group_members", {}).get(str(group_id), ())
            }
            restore_paragraph_snapshots(
                request_state.get("document_paragraph_snapshots", {}), member_ids
            )
            request_state.get("translated_paragraph_ids", set()).difference_update(member_ids)
            kept_verbatim_ids = request_state.get(
                "kept_verbatim_paragraph_ids", set()
            )
            _clear_unkept_fragment_outputs(
                request_state.get("accepted_outputs", {}),
                member_ids,
                kept_verbatim_ids,
            )

    def _font_policy_body(paragraph: Any) -> bool:
        if id(paragraph) not in request_state.get("translated_paragraph_ids", set()):
            return False
        if not _is_body_text_paragraph(paragraph):
            return False
        return not _is_independent_formula_paragraph(paragraph)

    original_layout_typesetting_units = Typesetting._layout_typesetting_units

    @functools.wraps(original_layout_typesetting_units)
    def layout_with_body_line_spacing(
        instance: Any,
        typesetting_units: Any,
        box: Any,
        scale: float,
        line_skip: float,
        paragraph: Any,
        use_english_line_break: bool = True,
    ) -> Any:
        if not _font_policy_body(paragraph):
            return original_layout_typesetting_units(
                instance,
                typesetting_units,
                box,
                scale,
                line_skip,
                paragraph,
                use_english_line_break,
            )
        candidate = float(line_skip)
        last_result = None
        while candidate >= 1.05:
            result = original_layout_typesetting_units(
                instance,
                typesetting_units,
                box,
                scale,
                candidate,
                paragraph,
                use_english_line_break,
            )
            last_result = result
            if len(result) > 1 and result[1]:
                request_state.setdefault("body_line_spacing", {})[id(paragraph)] = candidate
                return result
            candidate = round(candidate - 0.05, 4)
        return last_result if last_result is not None else original_layout_typesetting_units(
            instance,
            typesetting_units,
            box,
            scale,
            line_skip,
            paragraph,
            use_english_line_break,
        )

    Typesetting._layout_typesetting_units = layout_with_body_line_spacing

    original_find_optimal_scale_and_layout = Typesetting._find_optimal_scale_and_layout

    @functools.wraps(original_find_optimal_scale_and_layout)
    def find_table_scale_with_cell_bounds(
        instance: Any,
        paragraph: Any,
        page: Any,
        typesetting_units: Any,
        initial_scale: float = 1.0,
        use_english_line_break: bool = True,
        apply_layout: bool = False,
    ) -> Any:
        return _with_table_cell_bounds(
            instance,
            paragraph,
            page,
            lambda: original_find_optimal_scale_and_layout(
                instance,
                paragraph,
                page,
                typesetting_units,
                initial_scale,
                use_english_line_break,
                apply_layout,
            ),
        )

    Typesetting._find_optimal_scale_and_layout = find_table_scale_with_cell_bounds

    original_get_optimal_scale = Typesetting._get_optimal_scale

    @functools.wraps(original_get_optimal_scale)
    def get_optimal_scale_with_capture(
        instance: Any,
        paragraph: Any,
        page: Any,
        typesetting_units: Any,
        use_english_line_break: bool = True,
    ) -> float:
        scale = original_get_optimal_scale(
            instance,
            paragraph,
            page,
            typesetting_units,
            use_english_line_break,
        )
        request_state.setdefault("body_raw_scales", {})[id(paragraph)] = float(scale)
        return scale

    Typesetting._get_optimal_scale = get_optimal_scale_with_capture

    original_preprocess_document = Typesetting.preprocess_document

    @functools.wraps(original_preprocess_document)
    def preprocess_document_with_body_scale_classes(
        instance: Any, document: Any, pbar: Any
    ) -> Any:
        result = original_preprocess_document(instance, document, pbar)
        # BabelDOC clamps every paragraph to a document-wide mode scale. Restore
        # each paragraph's independent pre-clamp result first so the custom body
        # policy does not change captions, tables, formulas, or other layout types.
        # The common per-class body scale is applied later, against the final
        # paragraph boxes (see typesetting_document_two_phase).
        raw_scales = request_state.get("body_raw_scales", {})
        for page in getattr(document, "page", ()) or ():
            for paragraph in getattr(page, "pdf_paragraph", ()) or ():
                paragraph.optimal_scale = float(raw_scales.get(id(paragraph), 1.0))
        return result

    Typesetting.preprocess_document = preprocess_document_with_body_scale_classes

    original_render_page = Typesetting.render_page

    def apply_common_body_scales(
        instance: Any, deferred: list[tuple[Any, Any, Any]]
    ) -> None:
        """Measure body paragraphs in their final boxes and give each font-size
        class one scale (floored at 0.8; below-floor overflow stays individual)."""
        groups: dict[float, list[tuple[Any, float]]] = {}
        for paragraph, page, fonts in deferred:
            if not _font_policy_body(paragraph):
                continue
            try:
                font_size = round(float(paragraph.pdf_style.font_size), 1)
            except (TypeError, ValueError, AttributeError):
                continue
            try:
                units = instance.create_typesetting_units(paragraph, fonts)
                if all(unit.can_passthrough for unit in units):
                    continue
                raw_scale = float(instance._get_optimal_scale(paragraph, page, units))
            except Exception:
                continue
            groups.setdefault(font_size, []).append((paragraph, raw_scale))

        class_summary = []
        for font_size, members in sorted(groups.items()):
            common_scale, final_scales, overflow_count = body_font_scale_plan(
                [raw for _, raw in members]
            )
            for (paragraph, _), final_scale in zip(members, final_scales):
                paragraph.optimal_scale = final_scale
            class_summary.append(
                {
                    "original_font_size": font_size,
                    "paragraph_count": len(members),
                    "common_scale": round(common_scale, 4),
                    "overflow_paragraphs": overflow_count,
                }
            )
        request_state["body_font_class_summary"] = class_summary
        _apply_common_table_scales(
            instance,
            deferred,
            request_state.get("translated_paragraph_ids", set()),
        )

    def typesetting_document_two_phase(instance: Any, document: Any) -> None:
        """BabelDOC's render_page shrinks paragraph boxes to resolve overlaps and
        then re-typesets each paragraph independently, which undid a scale
        chosen on the original boxes. Run its box adjustment first with
        rendering deferred, fix the body scales on the final boxes, then render."""

        def run(pbar: Any) -> None:
            instance.preprocess_document(document, pbar)
            deferred_by_page: list[list[tuple[Any, Any, Any]]] = []
            for page in document.page:
                instance.translation_config.raise_if_cancelled()
                page_deferred: list[tuple[Any, Any, Any]] = []
                instance.render_paragraph = (
                    lambda paragraph, owner, fonts, _sink=page_deferred: _sink.append(
                        (paragraph, owner, fonts)
                    )
                )
                try:
                    original_render_page(instance, page)
                finally:
                    del instance.render_paragraph
                deferred_by_page.append(page_deferred)
            apply_common_body_scales(
                instance, [entry for page_entries in deferred_by_page for entry in page_entries]
            )
            for page_entries in deferred_by_page:
                instance.translation_config.raise_if_cancelled()
                for paragraph, page, fonts in page_entries:
                    instance.render_paragraph(paragraph, page, fonts)
                if pbar is not None:
                    pbar.advance()

        monitor = getattr(instance.translation_config, "progress_monitor", None)
        if monitor:
            with monitor.stage_start(instance.stage_name, len(document.page) * 2) as pbar:
                run(pbar)
        else:
            run(None)

    original_typesetting_document = Typesetting.typesetting_document

    @functools.wraps(original_typesetting_document)
    def typesetting_document_with_scale_log(
        instance: Any, document: Any
    ) -> Any:
        result = typesetting_document_two_phase(instance, document)
        by_page: dict[int, list[float]] = {}
        by_class: dict[float, list[float]] = {}
        for page_index, page in enumerate(getattr(document, "page", ()) or (), start=1):
            for paragraph in getattr(page, "pdf_paragraph", ()) or ():
                if not _font_policy_body(paragraph):
                    continue
                scale = getattr(paragraph, "scale", None)
                if scale is None:
                    scale = getattr(paragraph, "optimal_scale", None)
                try:
                    final_scale = float(scale)
                    font_size = round(float(paragraph.pdf_style.font_size), 1)
                except (TypeError, ValueError, AttributeError):
                    continue
                by_page.setdefault(page_index, []).append(final_scale)
                by_class.setdefault(font_size, []).append(final_scale)
        summary = {
            "classes": [
                {
                    "original_font_size": font_size,
                    "count": len(scales),
                    "min_scale": round(min(scales), 4),
                    "max_scale": round(max(scales), 4),
                }
                for font_size, scales in sorted(by_class.items())
            ],
            "pages": {
                str(page): {
                    "min_scale": round(min(scales), 4),
                    "max_scale": round(max(scales), 4),
                }
                for page, scales in sorted(by_page.items())
            },
            "line_spacing_adjusted_paragraphs": len(
                request_state.get("body_line_spacing", {})
            ),
            "class_policy": request_state.get("body_font_class_summary", []),
        }
        request_state["body_font_scale_summary"] = summary
        _write_job_log(
            request_state,
            {"event": "body_font_scale_summary", **summary},
            api_key,
        )
        return result

    Typesetting.typesetting_document = typesetting_document_with_scale_log

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
            request_state["fatal_error_log"] = log_safe_error(error, api_key)

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
            *,
            prefix_override: str | None = None,
            phase: str = "main",
        ) -> tuple[str, str | None, Any]:
            nonlocal request_index
            wait_for_split_request(request_index)
            request_index += 1
            if phase == "main":
                record_submitted_paragraphs(
                    policy,
                    batch,
                    request_state.get("prepared_paragraphs") or [],
                    request_state.setdefault("submitted_paragraph_ids", set()),
                )
                # A continuation request may carry only part of a fragment
                # pair; rebuild the links against the items really sent.
                prepared = request_state.get("prepared_paragraphs") or []
                metadata_by_paragraph = request_state.get("validation_metadata", {})
                linked_batch = [dict(item) for item in batch]
                entries = []
                for item in linked_batch:
                    try:
                        paragraph = prepared[int(item.get("id"))]
                    except (TypeError, ValueError, IndexError):
                        continue
                    metadata = metadata_by_paragraph.get(id(paragraph))
                    if metadata:
                        entries.append((item, id(paragraph), metadata))
                _link_fragment_items(entries)
                batch = linked_batch
            prompt_prefix = (
                prefix_override
                if prefix_override is not None
                else parsed_prompt[0] if parsed_prompt else ""
            )
            prompt_text = (
                prompt_prefix + json.dumps(batch, ensure_ascii=False, indent=2)
                if parsed_prompt
                else text
            )
            active_translator = request_state.get("active_llm_translator")
            token_counter = getattr(active_translator, "calc_token_count", None)
            estimated_output_tokens = (
                sum(
                    estimate_output_tokens(
                        int(token_counter(str(item.get("input", ""))))
                    )
                    for item in batch
                )
                if callable(token_counter)
                else None
            )
            request_items_log = [
                {
                    "id": item.get("id"),
                    "preview": _text_preview(item.get("input", ""), api_key),
                    "fragment": _is_fragment_item(item),
                }
                for item in batch
            ]
            if phase == "repair":
                request_state["repair_request_count"] = int(
                    request_state.get("repair_request_count", 0)
                ) + 1
            try:
                response = create_completion_adapting(
                    instance.client,
                    request_state,
                    max_output_tokens,
                    model=instance.model,
                    **options,
                    messages=[{"role": "user", "content": prompt_text}],
                    extra_headers=extra_headers,
                    extra_body=instance.extra_body,
                )
            except Exception as error:
                _write_job_log(
                    request_state,
                    {
                        "event": "request",
                        "phase": phase,
                        "request_index": request_budget.requests,
                        "item_count": len(batch),
                        "estimated_output_tokens": estimated_output_tokens,
                        "actual_output_tokens": None,
                        "finish_reason": None,
                        "error": log_safe_error(error, api_key),
                        "provider_message": provider_message_for_log(error, api_key, prompt_text),
                        "items": request_items_log,
                    },
                    api_key,
                )
                raise
            instance.update_token_count(response)
            choice = response.choices[0]
            message = choice.message
            usage = getattr(response, "usage", None)
            actual_output_tokens = getattr(usage, "completion_tokens", None)
            finish_reason = getattr(choice, "finish_reason", None)
            _write_job_log(
                request_state,
                {
                    "event": "request",
                    "phase": phase,
                    "request_index": request_budget.requests,
                    "item_count": len(batch),
                    "estimated_output_tokens": estimated_output_tokens,
                    "actual_output_tokens": actual_output_tokens,
                    "finish_reason": finish_reason,
                    "items": request_items_log,
                },
                api_key,
            )
            return (
                message.content or "",
                finish_reason,
                getattr(message, "refusal", None),
            )

        request_state["request_batch_callback"] = request_batch
        request_state["openai_translator"] = instance

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

                prepared_paragraphs = request_state.get("prepared_paragraphs") or []
                packed_items_by_paragraph = {
                    id(item["paragraph"]): item
                    for item in request_state.get("current_packed_items") or []
                }
                all_prepared_items = request_state.get("all_prepared_items") or []
                positions_by_paragraph = {
                    id(item["paragraph"]): index
                    for index, item in enumerate(all_prepared_items)
                }
                validation_metadata = request_state.setdefault(
                    "validation_metadata", {}
                )
                fragment_paragraph_ids = request_state.setdefault(
                    "fragment_paragraph_ids", set()
                )

                def metadata_for_packed_item(
                    packed_item: dict[str, Any],
                ) -> tuple[Any, dict[str, Any]]:
                    paragraph = packed_item["paragraph"]
                    prepared_result = packed_item.get("prepared_result") or ()
                    translate_input = (
                        prepared_result[1] if len(prepared_result) > 1 else None
                    )
                    expected_placeholders = _expected_placeholder_counts(
                        translate_input
                    )
                    fragment_type = packed_item.get("fragment_type")
                    fragment_group = packed_item.get("fragment_group")
                    fragment_sibling = packed_item.get("fragment_sibling_id")
                    fragment_index = packed_item.get("fragment_index")
                    position = positions_by_paragraph.get(id(paragraph))
                    previous_context = ""
                    next_context = ""
                    if position is not None:
                        if position > 0:
                            previous_context = str(
                                (all_prepared_items[position - 1].get("prepared_result") or ("",))[0]
                            )
                        if position + 1 < len(all_prepared_items):
                            next_context = str(
                                (all_prepared_items[position + 1].get("prepared_result") or ("",))[0]
                            )
                    metadata = {
                        "required_placeholders": [
                            {"token": token, "count": count}
                            for token, count in sorted(expected_placeholders.items())
                        ],
                        "fragment": bool(fragment_type),
                        "fragment_type": fragment_type,
                        "fragment_id": fragment_group,
                        "fragment_part": (
                            ("first" if fragment_index == 0 else "second")
                            if fragment_type
                            else None
                        ),
                        "fragment_sibling_paragraph": packed_item.get(
                            "fragment_sibling_paragraph"
                        ),
                        # Resolved per request to the sibling's item id; see
                        # _link_fragment_items. Never a debug id.
                        "continues_in": None,
                        "continued_from": None,
                        "previous_context": previous_context,
                        "next_context": next_context,
                    }
                    packed_item["validation_metadata"] = metadata
                    return translate_input, metadata

                request_entries: list[tuple[dict[str, Any], Any, dict[str, Any]]] = []
                for item_index, input_item in enumerate(parsed_prompt[1]):
                    try:
                        paragraph_index = int(input_item.get("id", item_index))
                    except (TypeError, ValueError):
                        continue
                    if not (0 <= paragraph_index < len(prepared_paragraphs)):
                        continue
                    paragraph = prepared_paragraphs[paragraph_index]
                    packed_item = packed_items_by_paragraph.get(id(paragraph), {})
                    _, metadata = metadata_for_packed_item(packed_item)
                    if metadata["required_placeholders"]:
                        input_item["required_placeholders"] = metadata[
                            "required_placeholders"
                        ]
                    request_entries.append((input_item, paragraph, metadata))

                _link_fragment_items(
                    [
                        (input_item, id(paragraph), metadata)
                        for input_item, paragraph, metadata in request_entries
                    ]
                )
                for input_item, paragraph, metadata in request_entries:
                    if metadata["fragment"]:
                        fragment_paragraph_ids.add(id(paragraph))
                    validation_metadata[id(paragraph)] = metadata

                outputs = translate_json_items_with_splitting(
                    parsed_prompt[1],
                    request_batch,
                    request_budget,
                    on_truncation=observe_truncated_prefix,
                    defer_remaining=defer_unconfirmed_suffix,
                    on_zero_prefix=cap_next_request_after_empty_prefix,
                )
                group_sizes = {
                    group_id: len(members)
                    for group_id, members in request_state.get(
                        "fragment_group_members", {}
                    ).items()
                }
                member_keys_by_item_id: dict[str, str] = {}
                for item_index, input_item in enumerate(parsed_prompt[1]):
                    try:
                        paragraph_index = int(input_item.get("id", item_index))
                    except (TypeError, ValueError):
                        continue
                    if 0 <= paragraph_index < len(prepared_paragraphs):
                        member_keys_by_item_id[str(input_item.get("id", item_index))] = str(
                            id(prepared_paragraphs[paragraph_index])
                        )
                kept_verbatim_paragraph_ids = request_state.setdefault(
                    "kept_verbatim_paragraph_ids", set()
                )
                outputs_by_item_id = {
                    str(output_item.get("id")): output_item
                    for output_item in outputs
                    if isinstance(output_item, dict) and output_item.get("id") is not None
                }
                for item_index, input_item in enumerate(parsed_prompt[1]):
                    item_id = str(input_item.get("id", item_index))
                    output_item = outputs_by_item_id.get(item_id, {})
                    if output_item.get("_ztf_failed") or output_item.get("_ztf_deferred"):
                        continue
                    if not _is_verbatim_output(
                        input_item.get("input"), output_item.get("output")
                    ):
                        continue
                    try:
                        paragraph_index = int(item_id)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= paragraph_index < len(prepared_paragraphs):
                        kept_paragraph = prepared_paragraphs[paragraph_index]
                        _mark_paragraph_kept_verbatim(
                            request_state, policy, kept_paragraph
                        )
                fragment_group_issues = _fragment_group_output_issues(
                    parsed_prompt[1],
                    outputs,
                    _active_translator_token_counter(request_state),
                    group_sizes,
                    request_state.setdefault("fragment_group_outcomes", {}),
                    member_keys_by_item_id,
                )
                invalid_fragment_groups = request_state.setdefault(
                    "invalid_fragment_groups", {}
                )
                repair_records = request_state.setdefault("repair_records", {})
                main_group_diagnostic_ids: set[int] = set()
                final_fragment_failure_ids = request_state.setdefault(
                    "final_fragment_failure_ids", set()
                )
                for group_id, reason in fragment_group_issues.items():
                    first_group_failure = group_id not in invalid_fragment_groups
                    invalid_fragment_groups.setdefault(group_id, reason)
                    group_members = request_state.get(
                        "fragment_group_members", {}
                    ).get(group_id, ())
                    group_member_ids = {
                        id(member["paragraph"])
                        for member in group_members
                    }
                    final_group_failure = (
                        not repair_failed or bool(request_state.get("fatal_error"))
                    )
                    if final_group_failure:
                        group_member_ids = rollback_fragment_group(
                            policy,
                            request_state,
                            group_members,
                            reason,
                            exempt_failure_ids=(
                                group_member_ids & kept_verbatim_paragraph_ids
                            ),
                        )
                    else:
                        restore_paragraph_snapshots(
                            request_state.get("document_paragraph_snapshots", {}),
                            group_member_ids,
                        )
                        request_state.get("translated_paragraph_ids", set()).difference_update(
                            group_member_ids
                        )
                    main_group_diagnostic_ids.update(group_member_ids)
                    if not final_group_failure:
                        for group_member in group_members:
                            member_paragraph = group_member["paragraph"]
                            member_key = id(member_paragraph)
                            if member_key in kept_verbatim_paragraph_ids:
                                continue
                            if member_key in repair_records:
                                continue
                            member_translate_input, member_metadata = (
                                metadata_for_packed_item(group_member)
                            )
                            member_prepared_result = group_member.get(
                                "prepared_result"
                            ) or ()
                            repair_records[member_key] = {
                                "paragraph": member_paragraph,
                                "translate_input": member_translate_input,
                                "tracker": group_member.get("tracker"),
                                "source": str(
                                    member_prepared_result[0]
                                    if member_prepared_result
                                    else member_paragraph.unicode or ""
                                ),
                                "reason": reason,
                                "checks": {"fragment_group_invalid": True},
                                "metadata": member_metadata,
                                "layout_label": _paragraph_layout_label(
                                    member_paragraph
                                ),
                                "fragment": True,
                                "repair_status": "pending",
                            }

                    if not first_group_failure:
                        continue
                    for group_member in group_members:
                        member_paragraph = group_member["paragraph"]
                        if id(member_paragraph) in kept_verbatim_paragraph_ids:
                            continue
                        member_prepared_result = group_member.get(
                            "prepared_result"
                        ) or ()
                        member_metadata = group_member.get(
                            "validation_metadata", {}
                        )
                        member_source = str(
                            member_prepared_result[0]
                            if member_prepared_result
                            else member_paragraph.unicode or ""
                        )
                        main_group_diagnostic_ids.add(id(member_paragraph))
                        _write_job_log(
                            request_state,
                            {
                                "event": "item_outcome",
                                "phase": "main",
                                "id": group_member.get("paragraph_index"),
                                "outcome": "retained_source"
                                if final_group_failure
                                else "needs_repair",
                                "reason": reason,
                                "checks": {"fragment_group_invalid": True},
                                "fragment": True,
                                "fragment_type": group_member.get("fragment_type"),
                                "fragment_id": group_id,
                                "placeholder_issues": [],
                                "preview": _text_preview(member_source, api_key),
                                "output_preview": "",
                            },
                            api_key,
                        )
                active_translator = request_state.get("active_llm_translator")
                failed_paragraph_ids = request_state.get(
                    "failed_batch_paragraph_ids", set()
                )
                records_by_paragraph = {
                    id(record["paragraph"]): record
                    for record in repair_records.values()
                    if record.get("paragraph") is not None
                }
                normalized_outputs = []
                request_state["fragment_translation_pairs"] = []
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

                    packed_item = packed_items_by_paragraph.get(id(paragraph), {}) if paragraph else {}
                    prepared_result = packed_item.get("prepared_result") or ()
                    translate_input = prepared_result[1] if len(prepared_result) > 1 else None
                    metadata = validation_metadata.get(id(paragraph), {}) if paragraph else {}
                    fragment_group_failure = (
                        invalid_fragment_groups.get(str(metadata.get("fragment_id")))
                        if metadata.get("fragment_id")
                        and id(paragraph) not in kept_verbatim_paragraph_ids
                        else None
                    )
                    checks: dict[str, Any] = {}
                    item_reason = str(failure_message or "")
                    usable = False
                    if was_deferred:
                        # Truncated suffix items are requeued in document order;
                        # the next main request, not the repair pass, owns them.
                        if paragraph is not None:
                            failed_paragraph_ids.add(id(paragraph))
                    elif fragment_group_failure:
                        checks = {"fragment_group_invalid": True}
                        item_reason = fragment_group_failure
                    elif not failure_message and isinstance(source_text, str):
                        quality = validate_translation_output(
                            source_text,
                            translated_text,
                            input_item,
                            active_translator.calc_token_count
                            if active_translator is not None
                            else len,
                        )
                        usable = bool(quality["usable"])
                        checks = quality["checks"]
                        item_reason = quality["reason"]
                    else:
                        checks = {"non_empty": False}
                        item_reason = item_reason or "模型未返回该段的有效译文，已保留原文。"

                    if not was_deferred and not usable:
                        if paragraph is not None:
                            failed_paragraph_ids.add(id(paragraph))
                        if repair_failed and paragraph is not None and not request_state.get("fatal_error"):
                            paragraph_key = id(paragraph)
                            existing_record = records_by_paragraph.get(paragraph_key)
                            if existing_record is None:
                                record = {
                                    "paragraph": paragraph,
                                    "translate_input": translate_input,
                                    "tracker": packed_item.get("tracker"),
                                    "source": str(source_text or ""),
                                    "reason": item_reason,
                                    "checks": checks,
                                    "metadata": metadata,
                                    "layout_label": _paragraph_layout_label(paragraph),
                                    "fragment": bool(metadata.get("fragment")),
                                    "repair_status": "pending",
                                }
                                repair_records[paragraph_key] = record
                                records_by_paragraph[paragraph_key] = record
                        elif (
                            id(paragraph) not in final_fragment_failure_ids
                            and (not fragment_group_failure or not repair_failed)
                        ):
                            policy.record_failure(paragraph, item_reason)
                    elif usable and paragraph is not None:
                        if checks.get("kept_verbatim"):
                            kept_verbatim_paragraph_ids.add(id(paragraph))
                        request_state.setdefault("translated_paragraph_ids", set()).add(id(paragraph))
                        # BabelDOC applies the text later and may still reject it
                        # with its own length check; keep what we accepted so the
                        # fallback hook can apply it (or queue repair) and success
                        # is only logged once the text is really in the paragraph.
                        request_state.setdefault("accepted_outputs", {})[id(paragraph)] = {
                            "text": translated_text,
                            "translate_input": translate_input,
                            "source": str(source_text or ""),
                            "metadata": metadata,
                            "tracker": packed_item.get("tracker"),
                            "item_id": input_item.get("id", item_index),
                        }
                        if metadata.get("fragment") and isinstance(source_text, str):
                            normalize_fragment_check_text = lambda value: re.sub(
                                r"[. 。…，]{20,}", ".", str(value)
                            )
                            request_state["fragment_translation_pairs"].append(
                                (
                                    normalize_fragment_check_text(source_text),
                                    normalize_fragment_check_text(translated_text),
                                )
                            )

                    if paragraph is None or id(paragraph) not in main_group_diagnostic_ids:
                        _write_job_log(
                            request_state,
                            {
                                "event": "item_outcome",
                                "phase": "main",
                                "id": input_item.get("id", item_index),
                                # "accepted" = passed our checks; a later
                                # "translated"/"needs_repair" record says whether
                                # BabelDOC actually applied it.
                                "outcome": "deferred" if was_deferred else "accepted" if usable else "needs_repair" if repair_failed else "retained_source",
                                "reason": "" if usable else item_reason,
                                "checks": checks,
                                "fragment": bool(metadata.get("fragment")),
                                "fragment_type": metadata.get("fragment_type"),
                                "fragment_id": metadata.get("fragment_id"),
                                "placeholder_issues": checks.get("placeholder_issues", []),
                                "preview": _text_preview(source_text, api_key),
                                "output_preview": _text_preview(translated_text, api_key)
                                if usable
                                else "",
                            },
                            api_key,
                        )

                    # The private marker is consumed here. BabelDOC receives one
                    # well-formed output item per input and never enters its batch
                    # exception path, which assigns a shared list to paragraph.unicode.
                    normalized_outputs.append(
                        {
                            "id": input_item.get("id", item_index),
                            "output": (
                                source_text
                                if failure_message or was_deferred or not usable
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
                lambda: create_completion_adapting(
                    instance.client,
                    request_state,
                    max_output_tokens,
                    model=instance.model,
                    **options,
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
                lambda: create_completion_adapting(
                    instance.client,
                    request_state,
                    max_output_tokens,
                    model=instance.model,
                    **options,
                    messages=instance.prompt(text),
                    extra_body=instance.extra_body,
                ),
            )
            instance.update_token_count(response)
            return response.choices[0].message.content.strip()
        except Exception as error:
            record_request_error(error)
            raise

    def perform_repair_pass(document_translator: Any) -> None:
        """Repair every failed item in one document-wide packed pass at most once."""
        if request_state.get("repair_pass_ran"):
            return
        request_state["repair_pass_ran"] = True
        kept_verbatim_paragraph_ids = request_state.get(
            "kept_verbatim_paragraph_ids", set()
        )
        queued_repair_records = request_state.get("repair_records", {})
        for paragraph_id in kept_verbatim_paragraph_ids:
            queued_repair_records.pop(paragraph_id, None)
        repair_records = list(queued_repair_records.values())
        if not repair_failed or not repair_records:
            return

        # A fragment group is one translation unit for rollback purposes.
        # Ensure every original member is represented in the repair request,
        # even if an earlier per-item failure only queued part of the group.
        records_by_paragraph = {
            id(record.get("paragraph")): record
            for record in repair_records
            if record.get("paragraph") is not None
        }
        repair_records_by_key = request_state.setdefault("repair_records", {})
        fragment_group_members = request_state.get("fragment_group_members", {})
        represented_group_ids = {
            str(record.get("metadata", {}).get("fragment_id"))
            for record in repair_records
            if record.get("metadata", {}).get("fragment_id")
        }
        for group_id in represented_group_ids:
            for member in fragment_group_members.get(group_id, ()):
                paragraph = member["paragraph"]
                if id(paragraph) in kept_verbatim_paragraph_ids:
                    continue
                if id(paragraph) in records_by_paragraph:
                    continue
                prepared_result = member.get("prepared_result") or ()
                translate_input = prepared_result[1] if len(prepared_result) > 1 else None
                expected_placeholders = _expected_placeholder_counts(translate_input)
                fragment_index = member.get("fragment_index")
                metadata = member.get("validation_metadata") or {
                    "required_placeholders": [
                        {"token": token, "count": count}
                        for token, count in sorted(expected_placeholders.items())
                    ],
                    "fragment": True,
                    "fragment_type": member.get("fragment_type"),
                    "fragment_id": group_id,
                    "fragment_part": "first" if fragment_index == 0 else "second",
                    "fragment_sibling_paragraph": member.get("fragment_sibling_paragraph"),
                    "continues_in": None,
                    "continued_from": None,
                    "previous_context": "",
                    "next_context": "",
                }
                record = {
                    "paragraph": paragraph,
                    "translate_input": translate_input,
                    "tracker": member.get("tracker"),
                    "source": str(
                        prepared_result[0]
                        if prepared_result
                        else paragraph.unicode or ""
                    ),
                    "reason": request_state.get("invalid_fragment_groups", {}).get(
                        group_id, "Linked fragment group requires repair."
                    ),
                    "checks": {"fragment_group_invalid": True},
                    "metadata": metadata,
                    "layout_label": _paragraph_layout_label(paragraph),
                    "fragment": True,
                    "repair_status": "pending",
                }
                repair_records_by_key[id(paragraph)] = record
                records_by_paragraph[id(paragraph)] = record
        repair_records = list(repair_records_by_key.values())

        request_batch_callback = request_state.get("request_batch_callback")
        if not callable(request_batch_callback) or document_translator is None:
            for repair_id, record in enumerate(repair_records):
                record["repair_status"] = "unavailable"
                record["repair_reason"] = record.get("reason", "补译不可用。")
                record["repair_checks"] = {"repair_available": False}
                policy.record_failure(record.get("paragraph"), record["repair_reason"])
                metadata = record.get("metadata", {})
                _write_job_log(
                    request_state,
                    {
                        "event": "item_outcome",
                        "phase": "repair",
                        "id": repair_id,
                        "outcome": "retained_source",
                        "reason": record["repair_reason"],
                        "checks": record["repair_checks"],
                        "fragment": record.get("fragment", False),
                        "fragment_type": metadata.get("fragment_type"),
                        "fragment_id": metadata.get("fragment_id"),
                        "placeholder_issues": [],
                        "preview": _text_preview(record.get("source", ""), api_key),
                        "output_preview": "",
                    },
                    api_key,
                )
            _write_job_log(
                request_state,
                {"event": "repair_summary", "repair_request_count": 0, "failed_items": len(repair_records)},
                api_key,
            )
            return

        request_state["active_llm_translator"] = document_translator
        token_counter = document_translator.calc_token_count
        repair_items: list[dict[str, Any]] = []
        records_by_repair_id: dict[int, dict[str, Any]] = {}
        for repair_id, record in enumerate(repair_records):
            metadata = record.get("metadata", {})
            repair_item: dict[str, Any] = {
                "id": repair_id,
                "input": record.get("source", ""),
                "layout_label": record.get("layout_label", "text"),
                "repair_reason": record.get("reason", ""),
                "required_placeholders": metadata.get("required_placeholders", []),
                "previous_context": metadata.get("previous_context", ""),
                "next_context": metadata.get("next_context", ""),
            }
            placeholders_hint = getattr(record.get("translate_input"), "get_placeholders_hint", None)
            if callable(placeholders_hint):
                hint = placeholders_hint()
                if hint:
                    repair_item["formula_placeholders_hint"] = hint
            records_by_repair_id[repair_id] = record
            repair_items.append(repair_item)
        # Repair items are renumbered, so fragment links are rebuilt against the
        # new ids (main-pass ids would point at unrelated repair items).
        _link_fragment_items(
            [
                (item, id(records_by_repair_id[item["id"]].get("paragraph")),
                 records_by_repair_id[item["id"]].get("metadata", {}))
                for item in repair_items
            ]
        )

        repair_prompt_prefix = (
            "Translate each input paragraph into Chinese as an independent fixed unit. "
            "This is the one packed repair pass for unusable main-pass outputs. "
            "Do not translate or copy previous_context or next_context; they are read-only "
            "neighboring context only. Preserve each listed required_placeholders token "
            "exactly the specified number of times and keep each formula placeholder inside "
            "the same <style> span as in the input. If fragment_id is present, the item is "
            "one part (fragment_part) of a sentence split across a column or page break, and "
            "continues_in / continued_from name the other part's id when it is in this "
            "request; translate only this item's own fragment; do not move content between "
            "items and do not return an empty output. Return only a JSON array with one "
            "{id, output} object per input.\n"
            "## Here is the input:\n"
        )

        def pack_repair_items(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
            return pack_items_by_output_budget(
                items,
                max_output_tokens,
                lambda item: token_counter(str(item.get("input", ""))),
                expansion_factor=request_state.get(
                    "output_expansion_factor", OUTPUT_EXPANSION_FACTOR
                ),
            )

        repair_batches = pack_repair_items(repair_items)
        request_budget.add_additional_requests(len(repair_batches))
        _write_job_log(
            request_state,
            {
                "event": "repair_started",
                "item_count": len(repair_items),
                "planned_requests": len(repair_batches),
                "main_minimum_requests": request_state.get(
                    "main_minimum_requests", request_budget.requests
                ),
                "main_requests_at_repair": request_budget.requests,
            },
            api_key,
        )
        def send_repair_batch(batch: list[dict[str, Any]]) -> Any:
            # Links must only name items inside the request actually sent.
            linked = [dict(item) for item in batch]
            _link_fragment_items(
                [
                    (
                        item,
                        id(records_by_repair_id[item["id"]].get("paragraph")),
                        records_by_repair_id[item["id"]].get("metadata", {}),
                    )
                    for item in linked
                    if item.get("id") in records_by_repair_id
                ]
            )
            return request_batch_callback(
                linked,
                prefix_override=repair_prompt_prefix,
                phase="repair",
            )

        def log_repair_batch_error(error: Exception) -> None:
            # Not fatal: the batch's paragraphs keep their original text and the
            # remaining repair batches (and earlier results) are unaffected.
            _write_job_log(
                request_state,
                {
                    "event": "repair_error",
                    "error": log_safe_error(error, api_key),
                    "repair_request_count": request_state.get("repair_request_count", 0),
                },
                api_key,
            )

        try:
            repaired_outputs = translate_json_items_with_splitting(
                repair_items,
                send_repair_batch,
                request_budget,
                repack_remaining=pack_repair_items,
                stop_when_capped=True,
                single_pass=True,
                on_batch_error=log_repair_batch_error,
            )
        except Exception as error:
            _write_job_log(
                request_state,
                {
                    "event": "repair_error",
                    "error": log_safe_error(error, api_key),
                    "repair_request_count": request_state.get("repair_request_count", 0),
                },
                api_key,
            )
            repaired_outputs = []

        # Repair has no later continuation queue. Treat a deferred or missing
        # completion as failed here so a linked group cannot be partially used.
        repair_validation_outputs = []
        for output in repaired_outputs:
            if not isinstance(output, dict):
                continue
            normalized = dict(output)
            if normalized.pop("_ztf_deferred", False):
                normalized["_ztf_failed"] = "Repair output was incomplete; original text retained."
            repair_validation_outputs.append(normalized)
        outputs_by_id = {
            int(item.get("id")): item
            for item in repair_validation_outputs
            if str(item.get("id", "")).isdigit()
        }
        repair_group_sizes = {
            group_id: sum(
                1
                for record in records_by_repair_id.values()
                if str(record.get("metadata", {}).get("fragment_id") or "")
                == group_id
            )
            for group_id in represented_group_ids
        }
        repair_member_keys = {
            str(repair_id): str(id(record["paragraph"]))
            for repair_id, record in records_by_repair_id.items()
        }
        repair_group_issues = _fragment_group_output_issues(
            repair_items,
            repair_validation_outputs,
            token_counter,
            repair_group_sizes,
            {},
            repair_member_keys,
        )
        repair_kept_verbatim_paragraph_ids: set[int] = set()
        for repair_id, record in records_by_repair_id.items():
            output_item = outputs_by_id.get(repair_id, {})
            if output_item.get("_ztf_failed") or output_item.get("_ztf_deferred"):
                continue
            if _is_verbatim_output(
                repair_items[repair_id].get("input"), output_item.get("output")
            ):
                paragraph_id = id(record["paragraph"])
                repair_kept_verbatim_paragraph_ids.add(paragraph_id)
                _mark_paragraph_kept_verbatim(
                    request_state, policy, record["paragraph"]
                )
        failed_repair_groups = set(repair_group_issues)
        for group_id, reason in repair_group_issues.items():
            rollback_fragment_group(
                policy,
                request_state,
                fragment_group_members.get(group_id, ()),
                reason,
                exempt_failure_ids=kept_verbatim_paragraph_ids,
            )

        repair_quality_by_id: dict[int, dict[str, Any]] = {}
        repair_translation_by_id: dict[int, Any] = {}
        for repair_id, record in records_by_repair_id.items():
            repair_item = repair_items[repair_id]
            output_item = outputs_by_id.get(repair_id, {})
            translated = output_item.get("output")
            failure_message = output_item.get("_ztf_failed")
            quality = (
                validate_translation_output(
                    repair_item["input"],
                    translated,
                    repair_item,
                    token_counter,
                )
                if not failure_message
                else {"usable": False, "reason": str(failure_message), "checks": {"non_empty": False}}
            )
            repair_translation_by_id[repair_id] = translated
            group_id = str(record.get("metadata", {}).get("fragment_id") or "")
            if (
                group_id in failed_repair_groups
                and id(record["paragraph"])
                not in repair_kept_verbatim_paragraph_ids
            ):
                quality = {
                    "usable": False,
                    "reason": repair_group_issues[group_id],
                    "checks": {"fragment_group_invalid": True},
                }
            repair_quality_by_id[repair_id] = quality

        applied_by_id: dict[int, bool] = {}
        kept_verbatim_by_id: set[int] = set()
        for repair_id, record in records_by_repair_id.items():
            quality = repair_quality_by_id[repair_id]
            translated = repair_translation_by_id.get(repair_id)
            applied = False
            if quality["usable"] and quality["checks"].get("kept_verbatim"):
                kept_verbatim_by_id.add(repair_id)
                request_state.get("translated_paragraph_ids", set()).discard(
                    id(record["paragraph"])
                )
            elif quality["usable"]:
                try:
                    applied = bool(
                        document_translator.il_translator.post_translate_paragraph(
                            record["paragraph"],
                            record["tracker"],
                            record["translate_input"],
                            str(translated),
                        )
                    )
                except Exception as error:
                    quality = {
                        "usable": False,
                        "reason": "补译结果无法应用，已保留原文。",
                        "checks": quality.get("checks", {}),
                    }
                    _write_job_log(
                        request_state,
                        {"event": "repair_apply_error", "error": log_safe_error(error, api_key)},
                        api_key,
                    )
            applied_by_id[repair_id] = applied
            if applied:
                request_state.setdefault("translated_paragraph_ids", set()).add(
                    id(record["paragraph"])
                )

        # post_translate_paragraph mutates in place. If applying one member of
        # an otherwise-valid group fails, restore and fail every member too.
        for group_id in represented_group_ids - failed_repair_groups:
            group_repair_ids = [
                repair_id
                for repair_id, record in records_by_repair_id.items()
                if str(record.get("metadata", {}).get("fragment_id") or "")
                == group_id
            ]
            if any(
                not applied_by_id.get(repair_id, False)
                and repair_id not in kept_verbatim_by_id
                for repair_id in group_repair_ids
            ):
                reason = "A linked fragment repair could not be applied; the original group was retained."
                repair_group_issues[group_id] = reason
                failed_repair_groups.add(group_id)
                rollback_fragment_group(
                    policy,
                    request_state,
                    fragment_group_members.get(group_id, ()),
                    reason,
                    exempt_failure_ids=kept_verbatim_paragraph_ids,
                )

        for repair_id, record in records_by_repair_id.items():
            quality = repair_quality_by_id[repair_id]
            group_id = str(record.get("metadata", {}).get("fragment_id") or "")
            translated = repair_translation_by_id.get(repair_id)
            if repair_id in kept_verbatim_by_id:
                applied = False
                record["repair_status"] = "kept_verbatim"
                record["repair_reason"] = "模型原样返回，保留原段落与排版。"
                record["repair_checks"] = quality["checks"]
            elif group_id in failed_repair_groups:
                applied = False
                record["repair_status"] = "failed"
                record["repair_reason"] = repair_group_issues[group_id]
                record["repair_checks"] = {"fragment_group_invalid": True}
            else:
                applied = applied_by_id.get(repair_id, False)
                if applied:
                    record["repair_status"] = "repaired"
                    record["repair_checks"] = quality["checks"]
                else:
                    record["repair_status"] = "failed"
                    record["repair_reason"] = quality.get("reason") or "补译未通过检查，已保留原文。"
                    record["repair_checks"] = quality.get("checks", {})
            if not applied and repair_id not in kept_verbatim_by_id:
                policy.record_failure(record.get("paragraph"), record["repair_reason"])

            _write_job_log(
                request_state,
                {
                    "event": "item_outcome",
                    "phase": "repair",
                    "id": repair_id,
                    "outcome": (
                        "kept_verbatim"
                        if repair_id in kept_verbatim_by_id
                        else "repaired" if applied else "retained_source"
                    ),
                    "reason": (
                        ""
                        if applied or repair_id in kept_verbatim_by_id
                        else record.get("repair_reason", "")
                    ),
                    "checks": record.get("repair_checks", {}),
                    "fragment": record.get("fragment", False),
                    "fragment_type": record.get("metadata", {}).get("fragment_type"),
                    "fragment_id": record.get("metadata", {}).get("fragment_id"),
                    "placeholder_issues": record.get("repair_checks", {}).get("placeholder_issues", []),
                    "preview": _text_preview(record.get("source", ""), api_key),
                    "output_preview": _text_preview(translated, api_key) if applied else "",
                },
                api_key,
            )

        _write_job_log(
            request_state,
            {
                "event": "repair_summary",
                "repair_request_count": request_state.get("repair_request_count", 0),
                "repaired_items": sum(record.get("repair_status") == "repaired" for record in repair_records),
                "kept_verbatim_items": sum(
                    record.get("repair_status") == "kept_verbatim"
                    for record in repair_records
                ),
                "failed_items": sum(
                    record.get("repair_status") not in {"repaired", "kept_verbatim"}
                    for record in repair_records
                ),
            },
            api_key,
        )

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
                if apply_accepted_after_babeldoc_rejection(owner, args):
                    return None
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
            request_state["document_translator"] = translator
            request_state["reference_paragraph_ids"] = collect_reference_paragraph_ids(
                translator.il_translator.docs, translate_references
            )
            request_state["preserved_paragraph_ids"] = set()
            docs = translator.il_translator.docs
            original_paragraph_state = snapshot_paragraph_state(docs)
            repeated_marginal_ids = collect_repeated_marginal_ids(docs)
            table_fragment_ids = collect_table_fragment_ids(docs)
            page_positions: dict[int, tuple[int, int]] = {}
            for page_index, page in enumerate(docs.page):
                for paragraph_index, paragraph in enumerate(page.pdf_paragraph):
                    page_positions[id(paragraph)] = (page_index, paragraph_index)

            flattened: list[dict[str, Any]] = []
            seen_paragraphs: set[int] = set()
            for task_index, (function, args, kwargs) in enumerate(self.tasks):
                batch = args[0]
                fragment_kind = None
                fragment_group = None
                fragment_siblings: dict[int, tuple[int, str]] = {}
                fragment_sibling_paragraphs: dict[int, int] = {}
                if len(getattr(batch, "paragraphs", ())) == 2:
                    pair = batch.paragraphs
                    pair_pages = getattr(batch, "pages", ())
                    if len(pair_pages) == 2 and pair_pages[0] is not pair_pages[1]:
                        fragment_kind = "cross-page"
                    elif (
                        len(pair_pages) == 2
                        and pair_pages[0] is pair_pages[1]
                        and all(_is_body_text_paragraph(part) for part in pair)
                    ):
                        first_box = getattr(pair[0], "box", None)
                        second_box = getattr(pair[1], "box", None)
                        try:
                            if (
                                first_box is not None
                                and second_box is not None
                                and float(second_box.y2) - float(first_box.y2) > 20
                            ):
                                fragment_kind = "cross-column"
                        except (TypeError, ValueError, AttributeError):
                            fragment_kind = None
                    if fragment_kind:
                        first_id = str(getattr(pair[0], "debug_id", None) or f"{task_index}-0")
                        second_id = str(getattr(pair[1], "debug_id", None) or f"{task_index}-1")
                        fragment_group = f"{fragment_kind}:{first_id}:{second_id}"
                        fragment_siblings = {
                            id(pair[0]): (0, second_id),
                            id(pair[1]): (1, first_id),
                        }
                        # The model-facing continues_in/continued_from hints must
                        # name the sibling's item id inside each request, so keep
                        # the sibling paragraph itself, not only its debug id.
                        fragment_sibling_paragraphs = {
                            id(pair[0]): id(pair[1]),
                            id(pair[1]): id(pair[0]),
                        }
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
                            "fragment_type": fragment_kind,
                            "fragment_group": fragment_group,
                            "fragment_index": fragment_siblings.get(paragraph_id, (None, None))[0],
                            "fragment_sibling_id": fragment_siblings.get(paragraph_id, (None, None))[1],
                            "fragment_sibling_paragraph": fragment_sibling_paragraphs.get(paragraph_id),
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
                paragraph = item["paragraph"]
                if (
                    id(paragraph) in request_state["reference_paragraph_ids"]
                    or id(paragraph) in repeated_marginal_ids
                    or id(paragraph) in table_fragment_ids
                    or _paragraph_is_marginal_or_stamp(paragraph, item["page"])
                    or _is_first_page_author_block(paragraph, item["position"][0])
                ):
                    request_state["preserved_paragraph_ids"].add(id(paragraph))
                    if pbar is not None:
                        pbar.advance(1)
                    return None, None
                result = _pre_translate_with_table_min_text_length(
                    original_pre_translate,
                    translator.il_translator,
                    paragraph,
                    item["tracker"],
                    item["page_font_map"],
                    item["xobj_font_map"],
                    is_table_paragraph=(
                        id(paragraph)
                        in request_state.get("table_paragraph_ids", set())
                    ),
                    min_text_length=translator.il_translator.translation_config.min_text_length,
                    config_lock=table_min_text_lock,
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
            request_state["all_prepared_items"] = prepared_items
            request_state["document_paragraph_snapshots"] = {
                id(item["paragraph"]): (
                    item["paragraph"],
                    item["paragraph"].unicode,
                    item["paragraph"].pdf_paragraph_composition,
                )
                for item in prepared_items
            }
            request_state["fragment_group_outcomes"] = {}
            fragment_group_members: dict[str, list[dict[str, Any]]] = {}
            for prepared_item in prepared_items:
                group_id = str(prepared_item.get("fragment_group") or "")
                if prepared_item.get("fragment_type") and group_id:
                    fragment_group_members.setdefault(group_id, []).append(
                        prepared_item
                    )
            request_state["fragment_group_members"] = fragment_group_members
            request_state["main_minimum_requests"] = len(packed)
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
                original_token_counter = translator.calc_token_count

                def fragment_relaxed_token_count(value: str) -> int:
                    normalized = re.sub(r"[. 。…，]{20,}", ".", str(value))
                    for source_fragment, translated_fragment in request_state.get(
                        "fragment_translation_pairs", ()
                    ):
                        if normalized == source_fragment or normalized == translated_fragment:
                            return int(original_token_counter(source_fragment))
                    return int(original_token_counter(value))

                translator.calc_token_count = fragment_relaxed_token_count
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
                    translator.calc_token_count = original_token_counter
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
                    request_state.pop("fragment_translation_pairs", None)

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

            if not request_state.get("fatal_error") and request_state.get("repair_records"):
                perform_repair_pass(translator)

            _write_job_log(
                request_state,
                {
                    "event": "paragraph_classification",
                    "paragraphs": classify_paragraphs(docs, original_paragraph_state, api_key),
                },
                api_key,
            )

            paragraph_font_maps.clear()
            request_state.pop("preprocessed_results", None)
            request_state.pop("output_expansion_factor", None)
            request_state.pop("all_prepared_items", None)

    llm_module.PriorityThreadPoolExecutor = DeferredTranslationExecutor


def babeldoc_version_error() -> str | None:
    try:
        installed_version = importlib.metadata.version("BabelDOC")
    except importlib.metadata.PackageNotFoundError:
        installed_version = "未安装"

    if installed_version != EXPECTED_BABELDOC_VERSION:
        return (
            f"需要 BabelDOC {EXPECTED_BABELDOC_VERSION}，当前为 {installed_version}。"
            "请重新翻译或在 Zotero 设置中点击“安装 / 修复环境”。"
        )
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    # Remove the key before BabelDOC starts multiprocessing children so they do not inherit it.
    api_key = os.environ.pop("ZTF_OPENAI_API_KEY", "")
    if not api_key:
        print("Missing ZTF_OPENAI_API_KEY environment variable.", file=sys.stderr, flush=True)
        return 2

    version_error = babeldoc_version_error()
    if version_error:
        _emit(
            "ZTF_ERROR ",
            {"message": version_error, "kind": "babeldoc_version"},
            api_key,
        )
        return 1

    # The key is added to Python's in-memory argv only. The OS-level command line contains runner.py,
    # never this value; the plugin also asserts the key is absent from Subprocess.call arguments.
    sys.argv[1:1] = ["--openai-api-key", api_key]
    sys.argv = ensure_runner_options(sys.argv)

    try:
        max_output_tokens = _read_integer_setting(
            "ZTF_MAX_OUTPUT_TOKENS", 8192, 1024
        )
        max_requests = _read_integer_setting("ZTF_MAX_REQUESTS", 20, 1)
        repair_failed = _read_boolean_setting("ZTF_REPAIR_FAILED", True)
        translate_references = _read_boolean_setting(
            "ZTF_TRANSLATE_REFERENCES", False
        )
    except ValueError as error:
        print(_redact(error, api_key), file=sys.stderr, flush=True)
        return 2

    job_log_path = os.environ.get("ZTF_JOB_LOG_PATH", "").strip()
    if job_log_path:
        try:
            log_directory = os.path.dirname(job_log_path)
            if log_directory:
                os.makedirs(log_directory, exist_ok=True)
                log_files = sorted(
                    (
                        os.path.join(log_directory, name)
                        for name in os.listdir(log_directory)
                        if name.lower().endswith(".jsonl")
                        and os.path.join(log_directory, name) != job_log_path
                    ),
                    key=lambda path: os.path.getmtime(path),
                )
                for old_path in log_files[:-19]:
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
        except OSError:
            # Logging is best-effort and never blocks a document translation.
            job_log_path = ""

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
    token_param = os.environ.pop("ZTF_TOKEN_PARAM", "").strip()
    request_state: dict[str, Any] = {
        # The provider's token-limit parameter, learned by the plugin's
        # connection test; requests still adapt if the provider rejects it.
        "token_param": token_param if token_param in TOKEN_LIMIT_PARAMETERS else "max_tokens",
        "fatal_error": "",
        "fatal_kind": "",
        "job_log_path": job_log_path,
        "repair_request_count": 0,
        "repair_records": {},
        "translated_paragraph_ids": set(),
        "fragment_paragraph_ids": set(),
    }
    _write_job_log(
        request_state,
        {
            "event": "job_started",
            "repair_failed": repair_failed,
            "translate_references": translate_references,
            "max_requests": max_requests,
            "max_output_tokens": max_output_tokens,
        },
        api_key,
    )

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
            repair_failed,
            translate_references,
            api_key,
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
        _write_job_log(
            request_state,
            {
                "event": "job_finished",
                "outcome": "failed",
                "requests": request_budget.requests,
                "fragment_item_count": len(request_state.get("fragment_paragraph_ids", set())),
                "repair_request_count": request_state.get("repair_request_count", 0),
                # The fatal message may carry a provider body (it is shown,
                # redacted, in the alert); the log only gets the fixed summary.
                "error": request_state.get("fatal_error_log")
                or log_safe_error(request_state["fatal_error"], api_key),
            },
            api_key,
        )
        _emit(
            "ZTF_ERROR ",
            {
                "message": with_translation_counts(request_state["fatal_error"]),
                "kind": request_state.get("fatal_kind") or "api_error",
                "requests": request_budget.requests,
                "planned": request_budget.planned,
                "log_path": job_log_path or None,
            },
            api_key,
        )
        return 1

    # BabelDOC can swallow paragraph API errors and still emit a PDF and exit 0.
    decision = policy.decision() if "policy" in locals() else None
    if decision and decision["kind"] == "translation_failed":
        _write_job_log(
            request_state,
            {
                "event": "job_finished",
                "outcome": "failed",
                "requests": request_budget.requests,
                "fragment_item_count": len(request_state.get("fragment_paragraph_ids", set())),
                "repair_request_count": request_state.get("repair_request_count", 0),
                "reason": decision["message"],
            },
            api_key,
        )
        _emit(
            "ZTF_ERROR ",
            {
                "message": with_translation_counts(decision["message"]),
                "kind": decision["kind"],
                "failed_paragraphs": decision["failed_paragraphs"],
                "requests": request_budget.requests,
                "planned": request_budget.planned,
                "log_path": job_log_path or None,
            },
            api_key,
        )
        return 1

    if state["error"]:
        _write_job_log(
            request_state,
            {
                "event": "job_finished",
                "outcome": "failed",
                "requests": request_budget.requests,
                "fragment_item_count": len(request_state.get("fragment_paragraph_ids", set())),
                "repair_request_count": request_state.get("repair_request_count", 0),
                "reason": state["error"],
            },
            api_key,
        )
        _emit(
            "ZTF_ERROR ",
            {
                "message": with_translation_counts(state["error"]),
                "kind": state["error_kind"],
                "requests": request_budget.requests,
                "planned": request_budget.planned,
                "log_path": job_log_path or None,
            },
            api_key,
        )
        return exit_code or 1

    if exit_code != 0 or not state["result"]:
        _write_job_log(
            request_state,
            {
                "event": "job_finished",
                "outcome": "failed",
                "requests": request_budget.requests,
                "fragment_item_count": len(request_state.get("fragment_paragraph_ids", set())),
                "repair_request_count": request_state.get("repair_request_count", 0),
            },
            api_key,
        )
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
    _write_job_log(
        request_state,
        {
            "event": "job_finished",
            "outcome": "warning" if decision and decision["kind"] == "warning" else "success",
            "requests": request_budget.requests,
            "planned_requests": request_budget.planned,
            "fragment_item_count": len(request_state.get("fragment_paragraph_ids", set())),
            "repair_request_count": request_state.get("repair_request_count", 0),
            "preserved_layout_paragraphs": len(request_state.get("preserved_paragraph_ids", set())),
            "body_font_scale_summary": request_state.get("body_font_scale_summary", {}),
            "token_param": request_state.get("token_param"),
            "parameter_adaptations": request_state.get("parameter_adaptations", []),
        },
        api_key,
    )
    _emit(
        "ZTF_RESULT ",
        {
            "mono": state["result"],
            # Lets the plugin remember the provider's token-limit parameter.
            "token_param": request_state.get("token_param"),
            "requests": request_budget.requests,
            "main_minimum_requests": request_state.get(
                "main_minimum_requests", request_budget.requests
            ),
            "planned": request_budget.planned,
            "log_path": job_log_path or None,
            "repair_request_count": request_state.get("repair_request_count", 0),
            "fragment_item_count": len(request_state.get("fragment_paragraph_ids", set())),
        },
        api_key,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
