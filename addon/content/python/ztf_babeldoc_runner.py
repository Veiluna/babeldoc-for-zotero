"""Run BabelDOC with the API key kept out of the operating-system command line."""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import logging.handlers
import multiprocessing
import os
import re
import sys
import threading
import traceback
from typing import Any


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

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self._lock = threading.Lock()
        self._thread_state = threading.local()
        self._attempted_paragraph_ids: dict[int, Any] = {}
        self._failed_paragraph_ids: set[int] = set()
        self._unbound_failed_paragraphs = 0
        self.paragraph_attempts = 0
        self.failed_paragraphs = 0
        self.completion_tokens: int | None = None
        self.first_error = ""

    def _record_attempt(self, paragraph: Any) -> None:
        if paragraph is None:
            return
        paragraph_id = id(paragraph)
        with self._lock:
            # Keep the object alive so its id cannot be reused during the job.
            self._attempted_paragraph_ids.setdefault(paragraph_id, paragraph)
            self.paragraph_attempts = len(self._attempted_paragraph_ids)

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
            if isinstance(result, (tuple, list)) and result and result[0] is not None:
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
            detail = TranslationPolicy._first_line(exc_info[1])
            if detail:
                return detail

        if message.startswith("ContentFilterError:"):
            detail = TranslationPolicy._first_line(message.partition(":")[2])
            if detail.endswith("."):
                detail = detail[:-1].rstrip()
            if detail:
                return detail

        # Paragraph source text can itself contain "Error:". BabelDOC appends
        # the actual exception at the end, so parse the final marker only.
        marker = message.rfind("Error:")
        if marker >= 0:
            detail = TranslationPolicy._first_line(message[marker + len("Error:"):])
            # BabelDOC appends a period after the exception text in this log message.
            if detail.endswith("."):
                detail = detail[:-1].rstrip()
            if detail:
                return detail
        return (
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

        all_paragraphs_failed = attempts > 0 and failures >= attempts
        no_completion_tokens = attempts > 0 and completion_tokens == 0
        if no_completion_tokens or all_paragraphs_failed:
            return {
                "kind": "translation_failed",
                "failed_paragraphs": failures,
                "message": first_error or "BabelDOC produced no completion tokens for paragraph text.",
            }
        if failures:
            return {
                "kind": "warning",
                "failed_paragraphs": failures,
                "message": first_error or "One or more paragraph translations failed.",
            }
        return None


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

    state: dict[str, Any] = {"error": "", "result": ""}

    def create_progress_handler(config: Any, show_log: bool = False):
        del config, show_log

        def handle_event(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            if event_type in ("progress_start", "progress_update", "progress_end"):
                _emit(
                    "ZTF_PROGRESS ",
                    {
                        "overall": event.get("overall_progress", 0),
                        "stage": event.get("stage", ""),
                        "current": event.get("stage_current", 0),
                        "total": event.get("stage_total", 0),
                    },
                    api_key,
                )
            elif event_type == "error":
                state["error"] = _redact(
                    event.get("error", "BabelDOC reported an error."), api_key
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
        policy.instrument(ILTranslator)
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

    # BabelDOC can swallow paragraph API errors and still emit a PDF and exit 0.
    decision = policy.decision() if "policy" in locals() else None
    if decision and decision["kind"] == "translation_failed":
        _emit(
            "ZTF_ERROR ",
            {
                "message": decision["message"],
                "kind": decision["kind"],
                "failed_paragraphs": decision["failed_paragraphs"],
            },
            api_key,
        )
        return 1

    if state["error"]:
        _emit("ZTF_ERROR ", {"message": state["error"]}, api_key)
        return exit_code or 1

    if exit_code != 0 or not state["result"]:
        return exit_code or 1

    if decision and decision["kind"] == "warning":
        _emit(
            "ZTF_WARNING ",
            {
                "message": decision["message"],
                "failed_paragraphs": decision["failed_paragraphs"],
            },
            api_key,
        )
    _emit("ZTF_RESULT ", {"mono": state["result"]}, api_key)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
