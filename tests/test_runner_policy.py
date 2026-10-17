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
        "需要 BabelDOC 0.6.4，当前为 0.6.3。请重新翻译或在 Zotero 设置中点击“安装 / 修复环境”。"
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
        "需要 BabelDOC 0.6.4，当前为 未安装。请重新翻译或在 Zotero 设置中点击“安装 / 修复环境”。"
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


def test_paragraph_classification_reports_written_back_paragraphs_as_translated() -> None:
    from types import SimpleNamespace as NS

    kept = NS(unicode="Jane Doe  jane@example.org", pdf_paragraph_composition=["glyphs"])
    translated = NS(unicode="Deep learning is widely used.", pdf_paragraph_composition=["glyphs"])
    untouched_none = NS(unicode=None, pdf_paragraph_composition=None)
    same_text_rewritten = NS(unicode="CIC-IDS-2017", pdf_paragraph_composition=["glyphs"])
    rolled_back = NS(unicode="Results", pdf_paragraph_composition=["glyphs"])
    document = NS(page=[
        NS(pdf_paragraph=[kept, translated]),
        NS(pdf_paragraph=[untouched_none, same_text_rewritten, rolled_back]),
    ])
    original = runner.snapshot_paragraph_state(document)
    translated.unicode = "深度学习被广泛使用。"
    translated.pdf_paragraph_composition = ["new glyphs"]
    # BabelDOC writes back an output equal to the text: still re-typeset.
    same_text_rewritten.pdf_paragraph_composition = ["new glyphs"]
    original_composition = rolled_back.pdf_paragraph_composition
    rolled_back.unicode, rolled_back.pdf_paragraph_composition = "结果", ["new glyphs"]
    runner.restore_paragraph_snapshots(
        {id(rolled_back): (rolled_back, "Results", original_composition)}, {id(rolled_back)}
    )

    records = runner.classify_paragraphs(document, original, "sk-secret")

    assert records == [
        {"page": 1, "index": 0, "preview": "Jane Doe jane@example.org", "decision": "kept"},
        {"page": 1, "index": 1, "preview": "Deep learning is widely used.", "decision": "translated"},
        {"page": 2, "index": 0, "preview": "", "decision": "kept"},
        {"page": 2, "index": 1, "preview": "CIC-IDS-2017", "decision": "translated"},
        {"page": 2, "index": 2, "preview": "Results", "decision": "kept"},
    ]


def test_paragraph_classification_preview_is_redacted_and_short() -> None:
    from types import SimpleNamespace as NS

    paragraph = NS(unicode="key sk-secret " + "x" * 200, pdf_paragraph_composition=[])
    document = NS(page=[NS(pdf_paragraph=[paragraph])])
    records = runner.classify_paragraphs(document, runner.snapshot_paragraph_state(document), "sk-secret")
    assert "sk-secret" not in records[0]["preview"]
    assert len(records[0]["preview"]) <= 60


def test_table_fallback_line_splits_at_column_gaps_without_changing_characters() -> None:
    from types import SimpleNamespace as NS

    def box(x: float, y: float, x2: float, y2: float) -> NS:
        return NS(x=x, y=y, x2=x2, y2=y2)

    table = NS(id=1, class_name="table", box=box(0, 0, 100, 20))
    line = NS(id=2, conf=1, class_name="fallback_line", box=box(0, 0, 55, 10))
    characters = [
        NS(visual_bbox=NS(box=box(x, 0, x2, 10)), char_unicode=letter, vertical=False)
        for x, x2, letter in [
            (0, 5, "A"),
            (6, 11, "B"),
            (14, 19, "C"),  # ordinary word spacing is below one line height
            (40, 45, "D"),  # the column gutter is larger than one line height
            (50, 55, "E"),
        ]
    ]
    original_characters = [(char.visual_bbox.box.x, char.char_unicode) for char in characters]
    page = NS(page_layout=[table, line], pdf_character=characters)

    runner._repair_fallback_line_layouts(page)

    fallback_lines = [layout for layout in page.page_layout if layout.class_name == "fallback_line"]
    assert [(item.box.x, item.box.x2) for item in fallback_lines] == [(0, 19), (40, 55)]
    assert [char.char_unicode for char in characters] == ["A", "B", "C", "D", "E"]
    assert [(char.visual_bbox.box.x, char.char_unicode) for char in characters] == original_characters


def test_table_split_keeps_characters_outside_the_table_on_the_original_line() -> None:
    from types import SimpleNamespace as NS

    def box(x: float, y: float, x2: float, y2: float) -> NS:
        return NS(x=x, y=y, x2=x2, y2=y2)

    table = NS(id=1, class_name="table", box=box(20, 0, 100, 10))
    line = NS(id=2, conf=1, class_name="fallback_line", box=box(0, 0, 115, 10))
    characters = [
        NS(visual_bbox=NS(box=box(x, 0, x2, 10)), char_unicode=letter, vertical=False)
        for x, x2, letter in [
            (0, 5, "L"),
            (16, 19, "l"),
            (25, 30, "A"),
            (32, 37, "B"),
            (70, 75, "C"),
            (77, 82, "D"),
            (110, 115, "R"),
        ]
    ]
    page = NS(page_layout=[table, line], pdf_character=characters)

    runner._repair_fallback_line_layouts(page)

    fallback_lines = [layout for layout in page.page_layout if layout.class_name == "fallback_line"]
    assert line in fallback_lines
    assert (line.box.x, line.box.x2) == (0, 19)
    outside_segments = [
        layout for layout in fallback_lines if layout.box.x < table.box.x or layout.box.x2 > table.box.x2
    ]
    table_segments = [layout for layout in fallback_lines if layout not in outside_segments]
    assert [(item.box.x, item.box.x2) for item in outside_segments] == [(0, 19), (110, 115)]
    assert [(item.box.x, item.box.x2) for item in table_segments] == [(25, 37), (70, 82)]
    for outside in (characters[0], characters[-1]):
        center_x = (outside.visual_bbox.box.x + outside.visual_bbox.box.x2) / 2
        assert any(item.box.x <= center_x <= item.box.x2 for item in outside_segments)
    for inside in characters[2:6]:
        center_x = (inside.visual_bbox.box.x + inside.visual_bbox.box.x2) / 2
        assert not any(item.box.x <= center_x <= item.box.x2 for item in outside_segments)


def test_fallback_line_with_no_table_characters_is_unchanged() -> None:
    from types import SimpleNamespace as NS

    def box(x: float, y: float, x2: float, y2: float) -> NS:
        return NS(x=x, y=y, x2=x2, y2=y2)

    table = NS(id=1, class_name="table", box=box(20, 0, 100, 10))
    line = NS(id=2, conf=1, class_name="fallback_line", box=box(0, 0, 115, 10))
    characters = [
        NS(visual_bbox=NS(box=box(x, 0, x2, 10)), char_unicode=letter, vertical=False)
        for x, x2, letter in [(0, 5, "L"), (110, 115, "R")]
    ]
    page = NS(page_layout=[table, line], pdf_character=characters)

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [table, line]
    assert (line.box.x, line.box.x2) == (0, 115)


def test_fallback_line_outside_table_is_unchanged() -> None:
    from types import SimpleNamespace as NS

    table = NS(id=1, class_name="table", box=NS(x=0, y=0, x2=100, y2=20))
    line = NS(
        id=2,
        class_name="fallback_line",
        box=NS(x=120, y=0, x2=175, y2=10),
    )
    page = NS(page_layout=[table, line], pdf_character=[])

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [table, line]
    assert (line.box.x, line.box.y, line.box.x2, line.box.y2) == (120, 0, 175, 10)


def test_near_fallback_fragment_expands_caption_box_and_is_removed() -> None:
    from types import SimpleNamespace as NS

    table = NS(id=1, class_name="table", box=NS(x=0, y=20, x2=100, y2=80))
    caption = NS(id=2, class_name="table_caption", box=NS(x=0, y=0, x2=90, y2=10))
    fragment = NS(id=3, class_name="fallback_line", box=NS(x=94, y=2, x2=114, y2=8))
    page = NS(page_layout=[table, caption, fragment], pdf_character=[])

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [table, caption]
    assert (caption.box.x, caption.box.y, caption.box.x2, caption.box.y2) == (0, 0, 114, 10)


def test_distant_fallback_fragment_stays_separate_from_caption() -> None:
    from types import SimpleNamespace as NS

    caption = NS(id=1, class_name="figure_caption", box=NS(x=0, y=0, x2=90, y2=10))
    fragment = NS(id=2, class_name="fallback_line", box=NS(x=130, y=2, x2=150, y2=8))
    page = NS(page_layout=[caption, fragment], pdf_character=[])

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [caption, fragment]
    assert (caption.box.x, caption.box.x2) == (0, 90)


def test_overlapping_fallback_frame_is_not_merged_into_caption() -> None:
    from types import SimpleNamespace as NS

    caption = NS(id=1, class_name="table_caption", box=NS(x=0, y=0, x2=90, y2=10))
    fragment = NS(id=2, class_name="fallback_line", box=NS(x=0, y=0, x2=200, y2=10))
    page = NS(page_layout=[caption, fragment], pdf_character=[])

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [caption, fragment]
    assert (caption.box.x, caption.box.y, caption.box.x2, caption.box.y2) == (0, 0, 90, 10)


def test_fallback_frame_crossing_a_body_layout_stays_out_of_caption() -> None:
    from types import SimpleNamespace as NS

    caption = NS(id=1, class_name="figure_caption", box=NS(x=0, y=0, x2=90, y2=10))
    body = NS(id=2, class_name="text", box=NS(x=100, y=0, x2=200, y2=10))
    fragment = NS(id=3, class_name="fallback_line", box=NS(x=0, y=0, x2=200, y2=10))
    page = NS(page_layout=[caption, body, fragment], pdf_character=[])

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [caption, body, fragment]
    assert (caption.box.x, caption.box.y, caption.box.x2, caption.box.y2) == (0, 0, 90, 10)


def test_caption_merges_a_small_fragment_overlapping_one_edge() -> None:
    from types import SimpleNamespace as NS

    caption = NS(id=1, class_name="table_caption", box=NS(x=100, y=0, x2=200, y2=10))
    fragment = NS(id=2, class_name="fallback_line", box=NS(x=85, y=2, x2=105, y2=8))
    page = NS(page_layout=[caption, fragment], pdf_character=[])

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [caption]
    assert (caption.box.x, caption.box.y, caption.box.x2, caption.box.y2) == (85, 0, 200, 10)


def test_caption_extracts_only_unclaimed_edge_chars_from_a_wide_fallback_frame() -> None:
    from types import SimpleNamespace as NS

    caption = NS(id=1, class_name="table_caption", box=NS(x=100, y=0, x2=200, y2=10))
    body = NS(id=2, class_name="text", box=NS(x=200, y=0, x2=220, y2=10))
    frame = NS(id=3, class_name="fallback_line", box=NS(x=80, y=0, x2=220, y2=10))

    def char(x: float, x2: float) -> object:
        box = NS(x=x, y=2, x2=x2, y2=8)
        return NS(box=box, visual_bbox=NS(box=box))

    page = NS(
        page_layout=[caption, body, frame],
        pdf_character=[
            char(85, 90),
            char(90, 95),
            char(105, 110),
            char(115, 120),
            char(205, 210),
            char(210, 215),
        ],
    )

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [caption, body]
    assert (caption.box.x, caption.box.y, caption.box.x2, caption.box.y2) == (85, 0, 200, 10)


def test_caption_merges_both_unclaimed_edges_of_a_wide_fallback_frame() -> None:
    from types import SimpleNamespace as NS

    caption = NS(id=1, class_name="figure_caption", box=NS(x=100, y=0, x2=200, y2=10))
    frame = NS(id=2, class_name="fallback_line", box=NS(x=80, y=0, x2=220, y2=10))

    def char(x: float, x2: float) -> object:
        box = NS(x=x, y=2, x2=x2, y2=8)
        return NS(box=box, visual_bbox=NS(box=box))

    page = NS(
        page_layout=[caption, frame],
        pdf_character=[
            char(85, 90),
            char(90, 95),
            char(105, 110),
            char(115, 120),
            char(180, 185),
            char(205, 210),
        ],
    )

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [caption]
    assert (caption.box.x, caption.box.y, caption.box.x2, caption.box.y2) == (85, 0, 210, 10)


def test_caption_extension_does_not_cover_an_intervening_body_layout() -> None:
    from types import SimpleNamespace as NS

    caption = NS(id=1, class_name="table_caption", box=NS(x=100, y=0, x2=200, y2=10))
    body = NS(id=2, class_name="text", box=NS(x=95, y=0, x2=98, y2=10))
    frame = NS(id=3, class_name="fallback_line", box=NS(x=90, y=0, x2=200, y2=10))

    def char(x: float, x2: float) -> object:
        box = NS(x=x, y=2, x2=x2, y2=8)
        return NS(box=box, visual_bbox=NS(box=box))

    page = NS(
        page_layout=[caption, body, frame],
        pdf_character=[char(90, 94), char(95, 98), char(105, 110)],
    )

    runner._repair_fallback_line_layouts(page)

    assert page.page_layout == [caption, body, frame]
    assert (caption.box.x, caption.box.y, caption.box.x2, caption.box.y2) == (100, 0, 200, 10)


def test_translated_paragraphs_in_one_table_share_the_minimum_scale() -> None:
    from types import SimpleNamespace as NS

    table = NS(id=1, class_name="table", box=NS(x=0, y=0, x2=100, y2=100))
    page = NS(page_layout=[table])
    left = NS(layout_label="fallback_line", box=NS(x=5, y=10, x2=40, y2=20), optimal_scale=1.0)
    right = NS(layout_label="fallback_line", box=NS(x=55, y=10, x2=90, y2=20), optimal_scale=1.0)
    body = NS(layout_label="text", box=NS(x=120, y=10, x2=180, y2=20), optimal_scale=1.0)
    figure_caption = NS(
        layout_label="figure_caption",
        box=NS(x=120, y=30, x2=180, y2=40),
        optimal_scale=1.0,
    )
    formula = NS(
        layout_label="fallback_line",
        box=NS(x=5, y=30, x2=40, y2=40),
        pdf_paragraph_composition=[NS(pdf_formula=object())],
        optimal_scale=1.0,
    )
    scales = {id(left): 0.91, id(right): 0.84, id(body): 0.88, id(figure_caption): 0.87, id(formula): 0.82}
    instance = NS(
        create_typesetting_units=lambda paragraph, fonts: [NS(can_passthrough=False)],
        _get_optimal_scale=lambda paragraph, owner, units: scales[id(paragraph)],
    )
    deferred = [
        (paragraph, page, None)
        for paragraph in (left, right, body, figure_caption, formula)
    ]

    runner._apply_common_table_scales(instance, deferred, {id(item[0]) for item in deferred})

    assert left.optimal_scale == right.optimal_scale == 0.84
    assert body.optimal_scale == figure_caption.optimal_scale == formula.optimal_scale == 1.0


def test_common_table_scale_can_fall_below_point_eight() -> None:
    from types import SimpleNamespace as NS

    table = NS(id=1, class_name="table", box=NS(x=0, y=0, x2=100, y2=100))
    page = NS(page_layout=[table])
    left = NS(box=NS(x=5, y=10, x2=40, y2=20), optimal_scale=1.0)
    right = NS(box=NS(x=55, y=10, x2=90, y2=20), optimal_scale=1.0)
    scales = {id(left): 0.76, id(right): 0.62}
    instance = NS(
        create_typesetting_units=lambda paragraph, fonts: [NS(can_passthrough=False)],
        _get_optimal_scale=lambda paragraph, owner, units: scales[id(paragraph)],
    )
    deferred = [(left, page, None), (right, page, None)]

    runner._apply_common_table_scales(instance, deferred, {id(left), id(right)})

    assert left.optimal_scale == right.optimal_scale == 0.62


def test_final_table_retypesetting_uses_cell_box_and_blocks_expansion() -> None:
    from types import SimpleNamespace as NS

    table = NS(id=1, class_name="table", box=NS(x=0, y=0, x2=100, y2=30))
    divider = NS(box=NS(x=50, y=0, x2=50, y2=30))
    paragraph = NS(box=NS(x=55, y=10, x2=70, y2=20), optimal_scale=0.62)
    page = NS(page_layout=[table], pdf_curve=[divider])
    original_box = paragraph.box
    instance = NS(
        get_max_right_space=lambda box, owner: 200,
        get_max_bottom_space=lambda box, owner: 0,
    )
    seen = []

    def retypeset():
        seen.append(
            (
                paragraph.box.x,
                paragraph.box.y,
                paragraph.box.x2,
                paragraph.box.y2,
                instance.get_max_right_space(paragraph.box, page),
                instance.get_max_bottom_space(paragraph.box, page),
            )
        )
        return "rendered"

    result = runner._with_table_cell_bounds(instance, paragraph, page, retypeset)

    assert result == "rendered"
    assert seen == [(55, 10, 99.5, 20, 99.5, 10)]
    assert paragraph.box is original_box

    left_cell_paragraph = NS(box=NS(x=30, y=10, x2=40, y2=20))
    left_cell_box = runner._table_cell_layout_box(left_cell_paragraph.box, page, table)
    assert (left_cell_box.x, left_cell_box.x2) == (30, 49.5)


def test_table_short_paragraphs_enter_translation_and_restore_minimum_length() -> None:
    from threading import RLock
    from types import SimpleNamespace as NS

    def box(x: float, y: float, x2: float, y2: float) -> NS:
        return NS(x=x, y=y, x2=x2, y2=y2)

    table = NS(id=1, class_name="table", box=box(0, 0, 100, 40))
    table_short = NS(debug_id="table-yes", unicode="YES", box=box(10, 10, 20, 20))
    outside_short = NS(debug_id="body-no", unicode="NO", box=box(120, 10, 130, 20))
    long_outside = NS(debug_id="body-long", unicode="body text", box=box(120, 30, 160, 40))
    page = NS(
        page_layout=[table],
        pdf_paragraph=[table_short, outside_short, long_outside],
    )

    paragraphs, table_ids, skipped = runner._page_paragraphs_with_table_short_text(
        page,
        min_text_length=5,
        translated_ids=set(),
        is_cid_paragraph=lambda paragraph: False,
    )
    assert paragraphs == [table_short, long_outside]
    assert table_ids == {id(table_short)}
    assert skipped == 1

    config = NS(min_text_length=5)
    translator = NS(translation_config=config)
    observed_minimums = []
    submitted = []

    def prepare(instance, paragraph, tracker, page_fonts, xobj_fonts):
        observed_minimums.append(instance.translation_config.min_text_length)
        if len(paragraph.unicode) < instance.translation_config.min_text_length:
            return None, None
        submitted.append(paragraph.unicode)
        return paragraph.unicode, None

    table_result = runner._pre_translate_with_table_min_text_length(
        prepare,
        translator,
        table_short,
        None,
        {},
        {},
        is_table_paragraph=True,
        min_text_length=5,
        config_lock=RLock(),
    )
    outside_result = runner._pre_translate_with_table_min_text_length(
        prepare,
        translator,
        outside_short,
        None,
        {},
        {},
        is_table_paragraph=False,
        min_text_length=5,
        config_lock=RLock(),
    )

    assert table_result == ("YES", None)
    assert outside_result == (None, None)
    assert submitted == ["YES"]
    assert observed_minimums == [0]
    assert config.min_text_length == 5


def test_table_fragments_much_shorter_than_a_cell_line_are_kept() -> None:
    from types import SimpleNamespace as NS

    table = NS(id=1, class_name="table", box=NS(x=0, y=0, x2=200, y2=100))
    other_table = NS(id=2, class_name="table", box=NS(x=0, y=200, x2=200, y2=300))
    lines = [
        NS(box=NS(x=5, y=10 + 10 * row, x2=60, y2=17 + 10 * row)) for row in range(4)
    ]
    # A stray comma split from its cell: 2.4 pt tall in a table of 7 pt lines.
    comma = NS(box=NS(x=62, y=10, x2=63.5, y2=12.4))
    two_line_cell = NS(box=NS(x=100, y=10, x2=180, y2=24))
    outside = NS(box=NS(x=5, y=150, x2=8, y2=152))
    lone_small = NS(box=NS(x=5, y=210, x2=8, y2=212))
    page = NS(
        page_layout=[table, other_table],
        pdf_paragraph=[*lines, comma, two_line_cell, outside, lone_small],
    )

    fragments = runner.collect_table_fragment_ids(NS(page=[page]))

    assert fragments == {id(comma)}

    # Even count: the median is the mean of the middle two (11), so ordinary
    # 7 pt single-line cells next to two-line cells are not fragments.
    short = [NS(box=NS(x=5, y=10 * row, x2=60, y2=10 * row + 7)) for row in range(2)]
    tall = [NS(box=NS(x=100, y=20 * row, x2=180, y2=20 * row + 15)) for row in range(2)]
    even_page = NS(page_layout=[table], pdf_paragraph=[*short, *tall])
    assert runner.collect_table_fragment_ids(NS(page=[even_page])) == set()


if __name__ == "__main__":
    test_table_fragments_much_shorter_than_a_cell_line_are_kept()
    test_paragraph_classification_reports_written_back_paragraphs_as_translated()
    test_paragraph_classification_preview_is_redacted_and_short()
    test_table_fallback_line_splits_at_column_gaps_without_changing_characters()
    test_table_split_keeps_characters_outside_the_table_on_the_original_line()
    test_fallback_line_with_no_table_characters_is_unchanged()
    test_fallback_line_outside_table_is_unchanged()
    test_near_fallback_fragment_expands_caption_box_and_is_removed()
    test_distant_fallback_fragment_stays_separate_from_caption()
    test_overlapping_fallback_frame_is_not_merged_into_caption()
    test_fallback_frame_crossing_a_body_layout_stays_out_of_caption()
    test_caption_merges_a_small_fragment_overlapping_one_edge()
    test_caption_extracts_only_unclaimed_edge_chars_from_a_wide_fallback_frame()
    test_caption_merges_both_unclaimed_edges_of_a_wide_fallback_frame()
    test_caption_extension_does_not_cover_an_intervening_body_layout()
    test_translated_paragraphs_in_one_table_share_the_minimum_scale()
    test_common_table_scale_can_fall_below_point_eight()
    test_final_table_retypesetting_uses_cell_box_and_blocks_expansion()
    test_table_short_paragraphs_enter_translation_and_restore_minimum_length()
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
