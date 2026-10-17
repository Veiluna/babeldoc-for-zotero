"""Self-contained tests for the BabelDOC log-driven failure policy."""

from __future__ import annotations

import contextlib
import builtins
import importlib.util
import io
import json
import logging
import logging.handlers
import os
import queue
import threading
from pathlib import Path
from unittest.mock import patch


RUNNER_PATH = Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def make_record(
    name: str,
    message: str,
    error: BaseException | None = None,
    level: int = logging.ERROR,
) -> logging.LogRecord:
    record = logging.LogRecord(name, level, "babeldoc.py", 1, message, (), None)
    if error is not None:
        record.exc_info = (type(error), error, None)
    return record


def test_installed_babeldoc_version_allows_startup() -> None:
    with patch.object(runner.importlib.metadata, "version", return_value="0.6.4") as version:
        assert runner.babeldoc_version_error() is None
        version.assert_called_once_with("BabelDOC")


def test_wrong_babeldoc_version_emits_a_typed_error_before_importing_babeldoc() -> None:
    output = io.StringIO()
    original_import = builtins.__import__

    def reject_babeldoc_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "babeldoc" or name.startswith("babeldoc."):
            raise AssertionError("BabelDOC must not be imported for a version mismatch")
        return original_import(name, *args, **kwargs)

    with patch.object(runner.importlib.metadata, "version", return_value="0.6.3"):
        with patch("builtins.__import__", side_effect=reject_babeldoc_import):
            with patch.dict(os.environ, {"ZTF_OPENAI_API_KEY": "TEST-API-KEY"}):
                with contextlib.redirect_stdout(output):
                    exit_code = runner.main()

    assert exit_code != 0
    event_lines = [line for line in output.getvalue().splitlines() if line.startswith("ZTF_ERROR ")]
    assert len(event_lines) == 1
    event = json.loads(event_lines[0][len("ZTF_ERROR "):])
    assert event["kind"] == "babeldoc_version"
    assert event["message"] == (
        "需要 BabelDOC 0.6.4，当前为 0.6.3。请重新运行 scripts/install-babeldoc.ps1。"
    )
    assert "TEST-API-KEY" not in output.getvalue()


def test_missing_babeldoc_package_uses_the_same_error_kind() -> None:
    output = io.StringIO()
    missing = runner.importlib.metadata.PackageNotFoundError("BabelDOC")
    with patch.object(runner.importlib.metadata, "version", side_effect=missing):
        with patch.dict(os.environ, {"ZTF_OPENAI_API_KEY": "TEST-API-KEY"}):
            with contextlib.redirect_stdout(output):
                exit_code = runner.main()

    assert exit_code != 0
    event_lines = [line for line in output.getvalue().splitlines() if line.startswith("ZTF_ERROR ")]
    assert len(event_lines) == 1
    event = json.loads(event_lines[0][len("ZTF_ERROR "):])
    assert event["kind"] == "babeldoc_version"
    assert event["message"] == (
        "需要 BabelDOC 0.6.4，当前为 未安装。请重新运行 scripts/install-babeldoc.ps1。"
    )


def run_policy(failures: list[str | None], completion_tokens: int) -> tuple[runner.TranslationPolicy, dict | None]:
    policy = runner.TranslationPolicy()

    class FakeParagraph:
        def __init__(self, error: str | None) -> None:
            self.error = error
            self.text = "paragraph text"

    class FakeILTranslator:
        def pre_translate_paragraph(self, paragraph: FakeParagraph) -> tuple[str, None]:
            return paragraph.text, None

        def translate_paragraph(self, paragraph: FakeParagraph) -> None:
            self.pre_translate_paragraph(paragraph)
            if paragraph.error is not None:
                content_filter = paragraph.error.startswith("ContentFilterError:")
                detail = paragraph.error.partition(":")[2].strip() if content_filter else paragraph.error
                exception = None if content_filter else ConnectionError(paragraph.error)
                policy.handle(
                    make_record(
                        "babeldoc.format.pdf.document_il.midend.il_translator",
                        paragraph.error if content_filter else (
                            f"Error translating paragraph. Paragraph: p1 (Hello). Error: {detail}."
                        ),
                        exception,
                        level=logging.WARNING if content_filter else logging.ERROR,
                    )
                )

    policy.instrument(FakeILTranslator)
    translator = FakeILTranslator()
    for failure in failures:
        translator.translate_paragraph(FakeParagraph(failure))

    policy.handle(
        logging.LogRecord(
            "babeldoc.main", logging.INFO, "main.py", 2,
            f"Completion tokens: {completion_tokens}", (), None,
        )
    )
    return policy, policy.decision()


def test_zero_completion_tokens_fails_with_first_underlying_error() -> None:
    policy, decision = run_policy(["Connection error"], 0)
    assert decision is not None
    assert decision["kind"] == "translation_failed"
    assert decision["failed_paragraphs"] == 1
    assert "Connection error" in decision["message"]


def test_partial_failures_warn_when_some_paragraphs_succeeded() -> None:
    _, decision = run_policy([None, "HTTP 429", None], 42)
    assert decision is not None
    assert decision["kind"] == "warning"
    assert decision["failed_paragraphs"] == 1
    assert decision["message"].startswith("HTTP 429")


def test_first_error_message_excludes_traceback_text() -> None:
    _, decision = run_policy(["Connection error\nTraceback: fake stack"], 0)
    assert decision is not None
    assert decision["message"].startswith("Connection error")


def test_empty_exception_message_counts_failure_and_uses_exception_type() -> None:
    policy = runner.TranslationPolicy()

    class FakeParagraph:
        text = "paragraph text"

    class FakeILTranslator:
        def pre_translate_paragraph(self, paragraph: FakeParagraph) -> tuple[str, None]:
            return paragraph.text, None

        def translate_paragraph(self, paragraph: FakeParagraph) -> None:
            self.pre_translate_paragraph(paragraph)
            policy.handle(
                make_record(
                    "babeldoc.format.pdf.document_il.midend.il_translator",
                    "Error translating paragraph. Paragraph: p1 (Hello). Error: .",
                    StopIteration(),
                )
            )

    policy.instrument(FakeILTranslator)
    FakeILTranslator().translate_paragraph(FakeParagraph())
    policy.handle(
        logging.LogRecord(
            "babeldoc.main", logging.INFO, "main.py", 2,
            "Completion tokens: 12", (), None,
        )
    )

    decision = policy.decision()
    assert policy.paragraph_attempts == 1
    assert policy.failed_paragraphs == 1
    assert decision is not None
    assert decision["kind"] == "translation_failed"
    assert decision["failed_paragraphs"] == 1
    assert decision["message"].startswith("StopIteration")


def test_exception_is_preferred_to_source_text_containing_error_marker() -> None:
    message = (
        "Error translating paragraph. Paragraph: p1 "
        "(Error: source sentence\ncontinued). Error: Connection error. "
    )
    record = make_record(
        "babeldoc.format.pdf.document_il.midend.il_translator",
        message,
        ConnectionError("Connection error"),
    )
    assert runner.TranslationPolicy._underlying_error(record, message) == "Connection error"


def test_error_tail_is_used_when_exception_info_is_missing() -> None:
    message = (
        "Error translating paragraph. Paragraph: p1 "
        "(Error: source sentence\ncontinued). Error: Connection error. "
    )
    record = make_record(
        "babeldoc.format.pdf.document_il.midend.il_translator",
        message,
    )
    assert runner.TranslationPolicy._underlying_error(record, message) == "Connection error"


def test_successful_batch_with_one_failed_fallback_is_partial_failure() -> None:
    policy = runner.TranslationPolicy()

    class FakeParagraph:
        def __init__(self, label: str) -> None:
            self.label = label
            self.text = label

    class FakeILTranslator:
        def pre_translate_paragraph(self, paragraph: FakeParagraph) -> tuple[str, None]:
            return paragraph.text, None

        def translate_paragraph(self, paragraph: FakeParagraph) -> None:
            self.pre_translate_paragraph(paragraph)
            exception = ConnectionError("Connection error")
            policy.handle(
                make_record(
                    "babeldoc.format.pdf.document_il.midend.il_translator",
                    f"Error translating paragraph. Paragraph: {paragraph.label}. Error: Connection error.",
                    exception,
                )
            )

    class FakeBatchTranslator:
        def __init__(self, fallback: FakeILTranslator) -> None:
            self.fallback = fallback

        def translate_paragraph(self, paragraphs: list[FakeParagraph]) -> None:
            # This models ILTranslatorLLMOnly's batch path: it preprocesses each
            # paragraph, applies the batch result to the first, then falls back on the second.
            for paragraph in paragraphs:
                self.fallback.pre_translate_paragraph(paragraph)
            self.fallback.translate_paragraph(paragraphs[1])

    policy.instrument(FakeILTranslator)
    fallback_translator = FakeILTranslator()
    FakeBatchTranslator(fallback_translator).translate_paragraph(
        [FakeParagraph("batch success"), FakeParagraph("fallback failure")]
    )
    policy.handle(
        logging.LogRecord(
            "babeldoc.main", logging.INFO, "main.py", 2,
            "Completion tokens: 42", (), None,
        )
    )

    decision = policy.decision()
    assert decision is not None
    assert decision["kind"] == "warning"
    assert policy.paragraph_attempts == 2
    assert decision["failed_paragraphs"] == 1


def test_batch_error_log_counts_when_thread_local_paragraph_context_is_missing() -> None:
    policy = runner.TranslationPolicy()

    class FakeParagraph:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeILTranslator:
        def pre_translate_paragraph(self, paragraph: FakeParagraph) -> tuple[str, None]:
            return paragraph.text, None

        def translate_paragraph(self, paragraph: FakeParagraph) -> None:
            self.pre_translate_paragraph(paragraph)

    policy.instrument(FakeILTranslator)
    translator = FakeILTranslator()
    translator.pre_translate_paragraph(FakeParagraph("batch success"))
    translator.pre_translate_paragraph(FakeParagraph("fallback failure"))
    # Some executor logging paths do not retain the thread-local fallback context.
    policy.handle(
        make_record(
            "babeldoc.format.pdf.document_il.midend.il_translator",
            "Error translating paragraph. Paragraph: p2 (Fallback). Error: Connection error.",
            ConnectionError("Connection error"),
        )
    )
    policy.handle(
        logging.LogRecord(
            "babeldoc.main", logging.INFO, "main.py", 2,
            "Completion tokens: 42", (), None,
        )
    )

    decision = policy.decision()
    assert decision is not None
    assert decision["kind"] == "warning"
    assert policy.paragraph_attempts == 2
    assert decision["failed_paragraphs"] == 1


def test_all_paragraphs_failed_even_if_term_extraction_used_tokens() -> None:
    _, decision = run_policy(["HTTP 401", "HTTP 401"], 12)
    assert decision is not None
    assert decision["kind"] == "translation_failed"
    assert decision["failed_paragraphs"] == 2


def test_partial_content_filter_rejections_warn_and_redact_the_reason() -> None:
    secret = "CONTENT_FILTER_SECRET"
    _, decision = run_policy([f"ContentFilterError: rejected {secret}", None], 42)

    assert decision is not None
    assert decision["kind"] == "warning"
    assert decision["failed_paragraphs"] == 1
    assert decision["message"].startswith(f"rejected {secret}")

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runner._emit("ZTF_WARNING ", decision, secret)
    event = json.loads(output.getvalue().strip()[len("ZTF_WARNING "):])
    assert event["failed_paragraphs"] == 1
    assert "rejected" in event["message"]
    assert secret not in event["message"]


def test_all_content_filter_rejections_fail_even_when_tokens_were_used_elsewhere() -> None:
    _, decision = run_policy(
        ["ContentFilterError: rejected first", "ContentFilterError: rejected second"],
        12,
    )

    assert decision is not None
    assert decision["kind"] == "translation_failed"
    assert decision["failed_paragraphs"] == 2
    assert decision["message"].startswith("rejected first")


def test_policy_captures_logs_before_async_evict_queue_discards_them() -> None:
    policy = runner.TranslationPolicy()
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level
    translator_logger = logging.getLogger(
        "babeldoc.format.pdf.document_il.midend.il_translator"
    )
    original_translator_handlers = translator_logger.handlers[:]
    original_translator_level = translator_logger.level
    original_translator_propagate = translator_logger.propagate
    listener_handlers: list[logging.Handler] = []
    listeners: list[logging.handlers.QueueListener] = []
    listener_started = threading.Event()
    listener_release = threading.Event()

    class EvictQueue(queue.Queue):
        def __init__(self) -> None:
            self.discarded = 0
            super().__init__(maxsize=1)

        def put(self, item: object, block: bool = False, timeout: float | None = None) -> None:
            del timeout
            try:
                super().put(item, block=False)
            except queue.Full:
                try:
                    self.get_nowait()
                    self.discarded += 1
                except queue.Empty:
                    pass
                super().put(item, block=False)

    log_queue = EvictQueue()

    class FakeBabeldocMain:
        @staticmethod
        def speed_up_logs() -> None:
            listener_handlers[:] = root_logger.handlers[:]
            listener = logging.handlers.QueueListener(log_queue, *listener_handlers)
            listener.start()
            listeners.append(listener)
            root_logger.handlers[:] = [logging.handlers.QueueHandler(log_queue)]

    class BlockingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == "Hold queue listener":
                listener_started.set()
                listener_release.wait(timeout=5)

    class FakeParagraph:
        text = "paragraph text"

    class FakeILTranslator:
        def pre_translate_paragraph(self, paragraph: FakeParagraph) -> tuple[str, None]:
            return paragraph.text, None

        def translate_paragraph(self, paragraph: FakeParagraph) -> None:
            self.pre_translate_paragraph(paragraph)
            translator_logger.error(
                "Error translating paragraph. Paragraph: p1 (Hello). Error: Connection error."
            )

    try:
        root_logger.handlers[:] = [BlockingHandler(), policy]
        root_logger.setLevel(logging.INFO)
        translator_logger.handlers.clear()
        translator_logger.setLevel(logging.INFO)
        translator_logger.propagate = True

        runner._install_synchronous_log_capture(FakeBabeldocMain, policy)
        FakeBabeldocMain.speed_up_logs()
        translator_logger.info("Hold queue listener")
        assert listener_started.wait(timeout=5), "the QueueListener should process the blocking record"

        policy.instrument(FakeILTranslator)
        FakeILTranslator().translate_paragraph(FakeParagraph())
        translator_logger.info("Completion tokens: 0")

        assert policy not in listener_handlers, "the asynchronous listener must not run the policy handler"
        assert log_queue.discarded == 1, "the error record should be evicted when the token record arrives"
        assert policy.completion_tokens == 0
        assert policy.failed_paragraphs == 1
        decision = policy.decision()
        assert decision is not None
        assert decision["kind"] == "translation_failed"
        assert decision["message"].startswith("Connection error")
    finally:
        listener_release.set()
        for listener in listeners:
            listener.stop()
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)
        translator_logger.handlers[:] = original_translator_handlers
        translator_logger.setLevel(original_translator_level)
        translator_logger.propagate = original_translator_propagate


def test_suppressed_fallback_is_counted_once_and_can_fail_the_job() -> None:
    policy = runner.TranslationPolicy()

    class FakeParagraph:
        pass

    paragraph = FakeParagraph()
    policy.record_failure(paragraph, "Unusable batch output; original retained.")
    policy.record_failure(paragraph, "Duplicate fallback was suppressed.")
    policy.handle(
        logging.LogRecord(
            "babeldoc.main", logging.INFO, "main.py", 2,
            "Completion tokens: 24", (), None,
        )
    )

    decision = policy.decision()
    assert policy.paragraph_attempts == 1
    assert policy.failed_paragraphs == 1
    assert decision is not None
    assert decision["kind"] == "translation_failed"
    assert decision["failed_paragraphs"] == 1
    assert decision["message"].startswith("Unusable batch output; original retained.")


def test_protocol_output_redacts_the_api_key_from_errors() -> None:
    secret = "FAKE_RUNNER_API_KEY"
    _, decision = run_policy([f"Connection error containing {secret}"], 0)
    assert decision is not None
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runner._emit("ZTF_ERROR ", decision, secret)
    assert secret not in output.getvalue()
    assert "Connection error" in output.getvalue()


def test_policy_sanitizes_html_provider_errors_and_reports_translation_counts() -> None:
    page = (
        '<!DOCTYPE html><html><head><title>Request Limit Exceeded</title></head>'
        '<body><div id="requestId">rq-1792116551</div>'
        '<script>document.getElementById("requestId").textContent=t</script></body></html>'
    )

    class HTTPError(RuntimeError):
        status_code = 429

    error = HTTPError(page)
    record = make_record(
        "babeldoc.format.pdf.document_il.midend.il_translator",
        "Error translating paragraph. Error: provider returned an HTML page.",
        error,
    )
    detail = runner.TranslationPolicy._underlying_error(
        record, record.getMessage()
    )
    assert detail == "HTTP 429：Request Limit Exceeded（requestId: rq-1792116551）"
    assert "<script" not in detail

    policy = runner.TranslationPolicy()
    paragraphs = [object() for _ in range(3)]
    for paragraph in paragraphs:
        policy._record_attempt(paragraph)
    policy.record_failure(paragraphs[0], detail)
    policy.handle(
        logging.LogRecord(
            "babeldoc.format.pdf.document_il.midend.il_translator_llm_only",
            logging.INFO,
            "translator.py",
            1,
            "Translation completed. Total: 3, Successful: 3, Fallback: 0",
            (),
            None,
        )
    )
    decision = policy.decision()
    assert decision is not None
    assert "3 段中 2 段成功（1 段保留原文）" in decision["message"]


def test_final_count_summary_reports_all_failures_over_babeldoc_successes() -> None:
    policy = runner.TranslationPolicy()
    paragraphs = [object(), object()]
    for paragraph in paragraphs:
        policy._record_attempt(paragraph)
        policy.record_failure(paragraph, "Unusable output; original retained.")
    policy.handle(
        logging.LogRecord(
            "babeldoc.main",
            logging.INFO,
            "main.py",
            2,
            "Completion tokens: 24",
            (),
            None,
        )
    )
    policy.handle(
        logging.LogRecord(
            "babeldoc.format.pdf.document_il.midend.il_translator_llm_only",
            logging.INFO,
            "translator.py",
            1,
            "Translation completed. Total: 2, Successful: 2, Fallback: 0",
            (),
            None,
        )
    )

    decision = policy.decision()
    assert decision is not None
    assert decision["kind"] == "translation_failed"
    assert policy.count_summary() == "2 段中 0 段成功（2 段保留原文）"
    assert "2 段中 0 段成功（2 段保留原文）" in decision["message"]


def test_error_protocol_redacts_keys_with_json_escape_characters_before_encoding() -> None:
    secret = 'FAKE"\\RUNNER_KEY'
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runner._emit("ZTF_ERROR ", {"message": f"Connection error: {secret}"}, secret)

    line = output.getvalue().strip()
    event = json.loads(line[len("ZTF_ERROR "):])
    assert secret not in event["message"]
    assert "[已隐藏]" in event["message"]


def test_warning_protocol_redacts_keys_with_json_escape_characters_before_encoding() -> None:
    secret = 'FAKE"\\RUNNER_KEY'
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runner._emit(
            "ZTF_WARNING ",
            {"failed_paragraphs": 1, "message": f"Connection error: {secret}"},
            secret,
        )

    line = output.getvalue().strip()
    event = json.loads(line[len("ZTF_WARNING "):])
    assert secret not in event["message"]
    assert "[已隐藏]" in event["message"]


if __name__ == "__main__":
    test_installed_babeldoc_version_allows_startup()
    test_wrong_babeldoc_version_emits_a_typed_error_before_importing_babeldoc()
    test_missing_babeldoc_package_uses_the_same_error_kind()
    test_zero_completion_tokens_fails_with_first_underlying_error()
    test_partial_failures_warn_when_some_paragraphs_succeeded()
    test_first_error_message_excludes_traceback_text()
    test_empty_exception_message_counts_failure_and_uses_exception_type()
    test_exception_is_preferred_to_source_text_containing_error_marker()
    test_error_tail_is_used_when_exception_info_is_missing()
    test_successful_batch_with_one_failed_fallback_is_partial_failure()
    test_batch_error_log_counts_when_thread_local_paragraph_context_is_missing()
    test_all_paragraphs_failed_even_if_term_extraction_used_tokens()
    test_partial_content_filter_rejections_warn_and_redact_the_reason()
    test_all_content_filter_rejections_fail_even_when_tokens_were_used_elsewhere()
    test_policy_captures_logs_before_async_evict_queue_discards_them()
    test_suppressed_fallback_is_counted_once_and_can_fail_the_job()
    test_protocol_output_redacts_the_api_key_from_errors()
    test_policy_sanitizes_html_provider_errors_and_reports_translation_counts()
    test_final_count_summary_reports_all_failures_over_babeldoc_successes()
    test_error_protocol_redacts_keys_with_json_escape_characters_before_encoding()
    test_warning_protocol_redacts_keys_with_json_escape_characters_before_encoding()
    print("runner policy regression cases passed")
