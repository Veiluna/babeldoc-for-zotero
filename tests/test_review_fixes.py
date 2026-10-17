"""Regression tests for the v0.2.1 review fixes (no BabelDOC import needed).

Run with: python tests/test_review_fixes.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace


RUNNER_PATH = Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _page(paragraphs):
    return SimpleNamespace(
        cropbox=SimpleNamespace(box=SimpleNamespace(x=0.0, y=0.0, x2=612.0, y2=792.0)),
        pdf_paragraph=paragraphs,
    )


def _paragraph(text, y, y2, label="text"):
    return SimpleNamespace(
        unicode=text,
        layout_label=label,
        box=SimpleNamespace(x=50.0, y=float(y), x2=560.0, y2=float(y2)),
    )


def test_first_page_author_block_is_preserved():
    assert runner._EMAIL_EVIDENCE_RE.search("x@host.tld")
    assert runner._EMAIL_EVIDENCE_RE.search("d.essam{v4}@adfa.edu.au")

    author_block = _paragraph(
        "Lisa Liu{v1}, Gints Engelen{v2}, Timothy Lynar Daryl Essam Wouter Joosen "
        "Universityof New South Wales, Canberra{v3}l.liuthorrold, t.lynar, "
        "d.essam{v4}@adfa.edu.au{v5}imec-DistriNet, KU Leuven{v6}gints.engelen, "
        "wouter.joosen{v7}@kuleuven.be",
        300,
        400,
        label="plain text",
    )
    assert runner._is_first_page_author_block(author_block, 0)
    assert not runner._is_first_page_author_block(author_block, 1)

    ordinary_email = _paragraph("Lisa Liu l.liu@adfa.edu.au", 300, 400)
    assert runner._is_first_page_author_block(ordinary_email, 0)

    sentence = _paragraph(
        "Correspondence should be addressed to the first author at "
        "l.liu@adfa.edu.au for further details.",
        300,
        400,
    )
    assert not runner._is_first_page_author_block(sentence, 0)

    title = _paragraph("A Robust Translation Framework", 300, 400)
    assert not runner._is_first_page_author_block(title, 0)


def test_repeated_running_headers_are_preserved_even_when_labeled_body_text():
    pages = []
    headers = []
    bodies = []
    for number in range(3):
        header = _paragraph("2022 IEEE Conference on Communications and Network Security (CNS)", 760, 772)
        footer = _paragraph(str(259 + number), 20, 30)
        body = _paragraph("Body text in the middle of the page " + str(number), 300, 400)
        headers.append(header)
        bodies.append(body)
        pages.append(_page([header, body, footer]))
    unique_edge = _paragraph("A figure caption that sits near the bottom once", 40, 60)
    pages[0].pdf_paragraph.append(unique_edge)
    document = SimpleNamespace(page=pages)

    preserved = runner.collect_repeated_marginal_ids(document)
    assert all(id(header) in preserved for header in headers)
    footers = [page.pdf_paragraph[2] for page in pages]
    assert all(id(footer) in preserved for footer in footers), "page numbers differ only by digits"
    assert not any(id(body) in preserved for body in bodies)
    assert id(unique_edge) not in preserved


def test_repeated_marginal_threshold_scales_with_document_length():
    pages = []
    legend_paragraphs = []
    header_paragraphs = []
    for number in range(10):
        paragraphs = []
        if number in (6, 7):
            legend = _paragraph("New Dataset", 760, 772)
            legend_paragraphs.append(legend)
            paragraphs.append(legend)
        if number < 9:
            header = _paragraph("2022 IEEE Conference Header", 760, 772)
            header_paragraphs.append(header)
            paragraphs.append(header)
        pages.append(_page(paragraphs))

    preserved = runner.collect_repeated_marginal_ids(SimpleNamespace(page=pages))
    assert not any(id(paragraph) in preserved for paragraph in legend_paragraphs)
    assert all(id(paragraph) in preserved for paragraph in header_paragraphs)

    short_document = SimpleNamespace(
        page=[
            _page([_paragraph("Repeated short document header", 760, 772)]),
            _page([_paragraph("Repeated short document header", 760, 772)]),
            _page([]),
        ]
    )
    short_preserved = runner.collect_repeated_marginal_ids(short_document)
    assert all(
        id(page.pdf_paragraph[0]) in short_preserved
        for page in short_document.page[:2]
    )


def _placeholder_item():
    return {
        "required_placeholders": [
            {"token": "<style id='1'>", "count": 1},
            {"token": "</style>", "count": 1},
            {"token": "{v1}", "count": 1},
        ]
    }


def test_formula_placeholder_moved_into_style_span_is_rejected():
    source = "The loss <style id='1'>decreases quickly</style> as {v1} grows over training."
    kept = "损失<style id='1'>迅速下降</style>，随着{v1}在训练中增大而变化明显。"
    moved = "损失<style id='1'>迅速下降{v1}</style>，随着训练进行而变化明显。"
    item = _placeholder_item()

    ok = runner.validate_translation_output(source, kept, item, len)
    assert ok["usable"], ok
    bad = runner.validate_translation_output(source, moved, item, len)
    assert not bad["usable"]
    assert any(issue.get("reason") == "formula_nesting_changed" for issue in bad["checks"]["placeholder_issues"])


def test_fragment_links_name_item_ids_in_the_same_request():
    first = {"id": 0, "input": "first half"}
    second = {"id": 1, "input": "second half"}
    meta_first = {"fragment": True, "fragment_id": "cross-column:a:b", "fragment_part": "first", "fragment_sibling_paragraph": "p2"}
    meta_second = {"fragment": True, "fragment_id": "cross-column:a:b", "fragment_part": "second", "fragment_sibling_paragraph": "p1"}
    runner._link_fragment_items([(first, "p1", meta_first), (second, "p2", meta_second)])
    assert first["continues_in"] == 1 and "continued_from" not in first
    assert second["continued_from"] == 0, "id 0 is a valid link target"
    assert first["fragment_part"] == "first" and second["fragment_part"] == "second"

    # Sibling not in this request (split by packing or a repair batch boundary).
    lone = {"id": 7, "input": "first half", "continues_in": "stale-debug-id"}
    runner._link_fragment_items([(lone, "p1", meta_first)])
    assert "continues_in" not in lone and "continued_from" not in lone
    assert lone["fragment_id"] == "cross-column:a:b" and lone["fragment_part"] == "first"
    assert runner._is_fragment_item(lone)


def test_kept_abbreviations_are_not_counted_as_untranslated_english():
    source = "Supported application protocols are HTTP, HTTPS, DNS, TLS, SSH, FTP, SMTP and IMAP protocols."
    translated = "支持的应用协议包括 HTTP、HTTPS、DNS、TLS、SSH、FTP、SMTP 和 IMAP 协议。"
    result = runner.validate_translation_output(source, translated, {}, len)
    assert result["usable"], result

    prose = "we evaluate the impact of labeling errors on standard machine learning algorithms and the datasets"
    untranslated = "we evaluate the impact of labeling errors on standard machine learning algorithms 和数据集"
    bad = runner.validate_translation_output(prose, untranslated, {}, len)
    assert not bad["usable"] and bad["checks"]["source_english_residue_ratio"] >= 0.72


def test_tiny_labels_skip_the_length_ratio_check():
    result = runner.validate_translation_output("CSV Volume", "CSV 数据量大小", {}, lambda s: max(1, len(s) // 2))
    assert result["usable"], result


def test_roman_numeral_reference_headings_are_detected():
    for heading in ("VIII. REFERENCES", "VII REFERENCES", "IV. References", "7. References", "A. Bibliography", "References"):
        assert runner._paragraph_is_reference_heading(SimpleNamespace(unicode=heading)), heading
    for text in ("References to prior work are discussed below.", "VIII. RESULTS"):
        assert not runner._paragraph_is_reference_heading(SimpleNamespace(unicode=text)), text


class _ProviderError(Exception):
    status_code = 400
    request_id = "req_123abc"
    type = "invalid_request_error"


def test_diagnostics_log_keeps_only_a_bounded_redacted_error_summary():
    key = 'sk-AB/CD"EF1234567890'
    echoed_body = json.dumps(
        {"error": {"message": "bad request", "echo": {"api_key": key, "messages": [{"content": "Secret paper text " * 50}]}}}
    )
    error = _ProviderError(f"Error code: 400 - {echoed_body}")

    summary = runner.log_safe_error(error, key)
    assert len(summary) <= 160
    for variant in (key, json.dumps(key)[1:-1], key.replace("/", "\\/")):
        assert variant not in summary
    assert "Secret paper text" not in summary
    assert "400" in summary and "req_123abc" in summary

    with tempfile.TemporaryDirectory() as temp:
        log_path = Path(temp) / "job.jsonl"
        runner._write_job_log(
            {"job_log_path": str(log_path)},
            {"event": "request", "error": str(error), "reason": "Bearer " + key},
            key,
        )
        content = log_path.read_text(encoding="utf-8")
    assert key not in content and json.dumps(key)[1:-1] not in content
    assert "Secret paper text" not in content
    record = json.loads(content)
    assert len(record["error"]) <= 160 and len(record["reason"]) <= 160

    # Redaction happens before truncation: a key straddling the 80-char cut is masked.
    preview = runner._text_preview("x" * 70 + key + " tail", key)
    assert key[:10] not in preview


class _EchoingError(Exception):
    status_code = 400
    type = "invalid_request_error"


def test_log_summary_never_keeps_echoed_metadata_or_bodies():
    key = "sk-live-0123456789abcdefABCDEF"
    error = _EchoingError("plain text body: Secret paper text continues here and here")
    error.request_id = key + "-tail"  # provider echoed the key into request_id
    summary = runner.log_safe_error(error, key)
    assert key[:12] not in summary and "Secret paper text" not in summary
    assert "request_id=[omitted]" in summary and "HTTP 400" in summary

    malformed = runner.log_safe_error('{"broken json echo": "Secret paper text', key)
    assert "Secret paper text" not in malformed

    transport = runner.log_safe_error(ConnectionError("Connection error."), key)
    assert "Connection error." in transport

    with tempfile.TemporaryDirectory() as temp:
        log_path = Path(temp) / "job.jsonl"
        runner._write_job_log(
            {"job_log_path": str(log_path)},
            {"event": "item_outcome", "reason": "模型返回空译文，已进入合并补译。"},
            key,
        )
        record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["reason"] == "模型返回空译文，已进入合并补译。", "own reasons stay readable"


def test_ieee_xplore_in_body_text_is_translated_but_edge_stamp_is_preserved():
    page = _page([])
    body = _paragraph("We searched IEEE Xplore and ACM Digital Library for related work published after 2015.", 300, 420)
    assert not runner._paragraph_is_marginal_or_stamp(body, page)
    stamp = _paragraph("Downloaded on October 15,2026 at 13:42:49 UTC from IEEE Xplore. Restrictions apply.", 15, 25)
    assert runner._paragraph_is_marginal_or_stamp(stamp, page)
    licence = _paragraph("Authorized licensed use limited to: Fuzhou University.", 300, 320)
    assert runner._paragraph_is_marginal_or_stamp(licence, page), "explicit licence sentence counts anywhere"


def test_verbatim_outputs_are_kept_without_content_rules():
    for phrase in ("of the system design", "in all of our experiments", "as shown in the table"):
        result = runner.validate_translation_output(phrase, phrase, {}, len)
        assert result["usable"] and result["checks"]["kept_verbatim"], phrase
    fragment = {"fragment_id": "cross-column:a:b", "fragment_part": "second"}
    result = runner.validate_translation_output(
        "of the system design", "of the system design", fragment, len
    )
    assert result["usable"] and result["checks"]["kept_verbatim"]
    for identifier in ("HTTP", "CIC-IDS-2017", "pcap", "Random Forest"):
        result = runner.validate_translation_output(identifier, identifier, {}, len)
        assert result["usable"] and result["checks"]["kept_verbatim"], identifier
    words = lambda text: max(1, len(text.split()))  # rough token count: 3 → 1
    mixed = runner.validate_translation_output("of the system", "系统的", {}, words)
    assert mixed["usable"]


def test_repair_single_pass_never_resends_after_truncation():
    items = [{"id": 0, "input": "first"}, {"id": 1, "input": "second"}]
    sent = []

    def request(batch):
        sent.append([item["id"] for item in batch])
        return "[", "length"

    budget = runner.RequestBudget(10)
    results = runner.translate_json_items_with_splitting(
        items, request, budget, repack_remaining=lambda pending: [pending], stop_when_capped=True, single_pass=True
    )
    assert sent == [[0, 1]], sent
    assert all(result.get("_ztf_failed") for result in results)

    # A truncated answer confirms its complete prefix; the rest is not re-sent.
    sent.clear()
    items3 = items + [{"id": 2, "input": "third"}]

    def partial(batch):
        sent.append([item["id"] for item in batch])
        return '[{"id": 0, "output": "第一"}, {"id": 1, "output": "第二"}, {"id": 2, "output": "第', "length"

    results = runner.translate_json_items_with_splitting(
        items3, partial, runner.RequestBudget(10), repack_remaining=lambda pending: [pending], single_pass=True
    )
    assert sent == [[0, 1, 2]], sent
    by_id = {str(result["id"]): result for result in results}
    assert by_id["0"]["output"] == "第一" and not by_id["0"].get("_ztf_failed")
    assert by_id["1"].get("_ztf_failed") and by_id["2"].get("_ztf_failed"), "last parsed object is never confirmed"


def test_repair_batch_error_keeps_earlier_batch_results():
    items = [{"id": 0, "input": "first"}, {"id": 1, "input": "second"}]
    calls = []
    errors = []

    def request(batch):
        calls.append([item["id"] for item in batch])
        if batch[0]["id"] == 1:
            raise ConnectionError("Connection error.")
        return '[{"id": 0, "output": "第一段"}]', "stop"

    results = runner.translate_json_items_with_splitting(
        items,
        request,
        runner.RequestBudget(10),
        repack_remaining=lambda pending: [[item] for item in pending],
        stop_when_capped=True,
        single_pass=True,
        on_batch_error=errors.append,
    )
    assert calls == [[0], [1]]
    by_id = {str(result["id"]): result for result in results}
    assert by_id["0"]["output"] == "第一段" and not by_id["0"].get("_ztf_failed")
    assert by_id["1"].get("_ztf_failed")
    assert len(errors) == 1 and isinstance(errors[0], ConnectionError)


def test_style_markup_is_not_counted_as_untranslated_english():
    source = (
        "Supported protocols in the evaluated system include <style id='1'>HTTP</style>, "
        "<style id='2'>HTTPS</style>, <style id='3'>DNS</style> and <style id='4'>TLS</style> "
        "for secure network communication between hosts"
    )
    translated = (
        "所评估系统支持的协议包括<style id='1'>HTTP</style>、<style id='2'>HTTPS</style>、"
        "<style id='3'>DNS</style>和<style id='4'>TLS</style>，用于主机之间的安全网络通信"
    )
    item = {
        "required_placeholders": [{"token": f"<style id='{n}'>", "count": 1} for n in range(1, 5)]
        + [{"token": "</style>", "count": 4}]
    }
    result = runner.validate_translation_output(source, translated, item, lambda s: max(1, len(s) // 3))
    assert result["usable"], result
    assert result["checks"]["source_english_residue_ratio"] < 0.72


def test_source_literal_lookalikes_may_be_kept_verbatim():
    source = "The format string %s and the name {Alice} appear in [[0]] samples of the study corpus."
    translated = "格式字符串 %s 和名字 {Alice} 出现在研究语料的 [[0]] 个样本中。"
    result = runner.validate_translation_output(source, translated, {}, lambda s: max(1, len(s) // 3))
    assert result["usable"], result
    added = runner.validate_translation_output(source, translated + " %d", {}, lambda s: max(1, len(s) // 3))
    assert not added["usable"], "a literal the source does not have is still an issue"


def test_placeholder_diagnostics_are_redacted_before_truncation():
    key = "sk-" + "Z" * 60
    with tempfile.TemporaryDirectory() as temp:
        log_path = Path(temp) / "job.jsonl"
        runner._write_job_log(
            {"job_log_path": str(log_path)},
            {
                "event": "item_outcome",
                "placeholder_issues": [{"token": f"<style id='{key}'>", "reason": "unsupported_style_opener"}],
                "checks": {"placeholder_issues": [{"actual_spelling": f"<style id='{key}'>"}]},
            },
            key,
        )
        content = log_path.read_text(encoding="utf-8")
    assert "Z" * 10 not in content, content
    record = json.loads(content)
    assert len(record["placeholder_issues"][0]["token"]) <= 40


class _PlainTextEcho(Exception):
    status_code = 500


def test_fatal_error_log_uses_the_fixed_summary():
    key = "sk-test-abcdefghijklmnop"
    error = _PlainTextEcho("Internal error while processing: Secret paper text " * 4 + key)
    summary = runner.log_safe_error(error, key)
    assert "Secret paper text" not in summary and key not in summary and "HTTP 500" in summary


def test_punctuation_only_english_echo_is_rejected_everywhere():
    # Rough token count: one per English word plus one per Chinese character.
    words = lambda text: max(1, len(re.findall(r"[A-Za-z]+", text)) + len(re.findall("[一-鿿]", text)))
    source = "The data is in the form of an array."
    echo = "The data is in the form of an array,"
    assert not runner.validate_translation_output(source, echo, {}, words)["usable"]
    fragment = {"fragment_id": "cross-page:a:b", "fragment_part": "first"}
    assert not runner.validate_translation_output(source, echo, fragment, words)["usable"]
    repair_item = {"id": 0, "input": source, "repair_reason": "empty"}
    assert not runner.validate_translation_output(source, echo, repair_item, words)["usable"]
    long_source = (
        "We observe that the corrected datasets lead to better generalization "
        "of the trained models across all of the evaluated attack classes."
    )
    assert not runner.validate_translation_output(long_source, long_source.replace(".", "!"), {}, words)["usable"]
    assert runner.validate_translation_output(source, "数据以数组形式存储。", {}, words)["usable"]


def _reference_document(texts):
    page = _page([_paragraph(text, 300, 320) for text in texts])
    return SimpleNamespace(page=[page]), page.pdf_paragraph


def test_reference_region_ends_at_numbered_and_titled_sections():
    for end_heading in ("9. Acknowledgments", "VIII. ACKNOWLEDGMENTS", "Appendix A Additional results", "Appendix B: Proofs"):
        document, paragraphs = _reference_document(
            ["References", "[1] A. Author, Some title, 2020.", end_heading, "Body text after the references."]
        )
        reference_ids = runner.collect_reference_paragraph_ids(document, False)
        assert id(paragraphs[1]) in reference_ids, end_heading
        assert id(paragraphs[3]) not in reference_ids, f"text after {end_heading!r} must be translated"


def test_bare_style_tags_are_rejected():
    source = "The loss <style id='1'>decreases</style> quickly during training of the model."
    item = {"required_placeholders": [{"token": "<style id='1'>", "count": 1}, {"token": "</style>", "count": 1}]}
    words = lambda text: max(1, len(text) // 3)
    good = "损失在模型训练过程中<style id='1'>下降</style>得很快。"
    assert runner.validate_translation_output(source, good, item, words)["usable"]
    for extra in ("<style>", "<style class=x>", "</style>"):
        bad = runner.validate_translation_output(source, good + extra, item, words)
        assert not bad["usable"], extra


def test_body_text_near_the_page_top_is_not_a_stamp():
    page = _page([])
    # Reviewer's case: a body paragraph whose box reaches into the top band.
    body = _paragraph("We downloaded the datasets and compared their features in our experiments.", 665, 734)
    assert not runner._paragraph_is_marginal_or_stamp(body, page)
    assert not runner._is_in_page_edge_band(body, page)
    searched = _paragraph("We searched IEEE Xplore for related work.", 712, 724)
    assert not runner._paragraph_is_marginal_or_stamp(searched, page), "inside the band but no stamp phrasing"
    stamp = _paragraph(
        "Authorized licensed use limited to: Fuzhou University. Downloaded on October 15,2026 at 13:42:49 UTC "
        "from IEEE Xplore. Restrictions apply.",
        14,
        24,
    )
    assert runner._paragraph_is_marginal_or_stamp(stamp, page)
    doi = _paragraph("DOI: 10.1109/CNS56114.2022.9947235", 30, 40)
    assert runner._paragraph_is_marginal_or_stamp(doi, page)


def test_urls_emails_and_author_lines_may_stay_verbatim():
    words = lambda text: max(1, len(re.findall(r"[A-Za-z]+", text)) + len(re.findall("[一-鿿]", text)))
    for text in (
        "distrinet-research.be/CNS2022",
        "{v1}https://intrusion-detection.distrinet-research.be/ CNS2022/",
        "Lisa Liu{v1}, Gints Engelen{v2}, Timothy Lynar {l.liu, t.lynar, d.essam}@adfa.edu.au",
    ):
        result = runner.validate_translation_output(text, text, {}, words)
        assert result["usable"], (text, result)
    # Exact prose echoes are retained by the same comparison as any other text.
    prose = "see the project page at https://example.org for the data and the code"
    prose_echo = runner.validate_translation_output(prose, prose, {}, words)
    assert prose_echo["usable"] and prose_echo["checks"]["kept_verbatim"]
    # Exact output equality applies to headings and labels as it does to other text.
    for label_like in (
        "II. BACKGROUND AND RELATED WORK",
        "DDoS - LOIC-UDP",
        "Web Attack - XSS",
    ):
        assert runner.validate_translation_output(label_like, label_like, {}, words)["usable"], label_like
    # Bare domains outside any fixed TLD list.
    for domain in ("inf.ethz.ch", "kth.se/research/data", "data.example.us"):
        assert runner.validate_translation_output(domain, domain, {}, words)["usable"], domain


class _ProviderBadRequest(Exception):
    status_code = 400

    def __init__(self, message, body):
        super().__init__(message)
        self.body = body


def test_provider_message_is_only_a_fixed_category():
    key = "sk-diag-0123456789abcdef"
    request = "Translate this: Patient name Alice Smith.\nThe labelling logic was reverse engineered."
    cases = {
        "max_tokens is too large for this model": "context_or_max_tokens",
        "Rate limit reached for requests": "quota_or_rate_limit",
        "The model `gpt-x` does not exist": "model_unavailable",
        "Invalid input: Alice Smith": "invalid_request",
        "Incorrect API key provided: " + key: "authentication",
        "something odd happened": "unclassified",
    }
    for message, category in cases.items():
        error = _ProviderBadRequest("Error code: 400", {"error": {"message": message}})
        assert runner.provider_message_for_log(error, key, request) == category, message
    # Whatever the provider says, no part of it reaches the log record.
    with tempfile.TemporaryDirectory() as temp:
        log_path = Path(temp) / "job.jsonl"
        echo = _ProviderBadRequest("Error code: 400", {"error": {"message": "Invalid input: Alice Smith\\nThe labelling"}})
        runner._write_job_log(
            {"job_log_path": str(log_path)},
            {"event": "request", "error": runner.log_safe_error(echo, key),
             "provider_message": runner.provider_message_for_log(echo, key, request)},
            key,
        )
        content = log_path.read_text(encoding="utf-8")
    assert "Alice" not in content and "labelling" not in content and key not in content


def test_verbatim_outputs_and_existing_short_label_policy():
    long_counter = lambda text: max(1, len(text) // 2)  # > 20 tokens like the real tokenizer (36)
    byline = "Lisa Liu{v1}, Gints Engelen{v2}, Timothy Lynar {l.liu, t.lynar, d.essam}@adfa.edu.au"
    assert long_counter(byline) > 20
    result = runner.validate_translation_output(byline, byline, {}, long_counter)
    assert result["usable"], result
    for marked in ("John Smith1 and Jane Doe2", "A. Smith*, B. Jones†"):
        assert runner.validate_translation_output(marked, marked, {}, long_counter)["usable"], marked
    # The Liu 2022 author/affiliation block from the user's run stays as it is.
    liu = (
        "Lisa Liu{v1}, Gints Engelen{v2}, Timothy Lynar Daryl Essam Wouter Joosen "
        "Universityof New South Wales, Canberra{v3}l.liuthorrold, t.lynar, "
        "d.essam{v4}@adfa.edu.au{v5}imec-DistriNet, KU Leuven{v6}"
        "gints.engelen, wouter.joosen{v7}@kuleuven.be"
    )
    assert runner.validate_translation_output(liu, liu, {}, long_counter)["usable"]
    # Tokens carrying placeholders are not plain words.
    formula_list = "mean{v1} variance{v2} skewness{v3} kurtosis{v4}"
    assert runner.verbatim_output_allowed(formula_list)
    # Abbreviations with a full stop are not words; a sentence's last word is.
    assert runner.verbatim_output_allowed("avg. std. min. max.")
    assert not runner.verbatim_output_allowed("of the system design.")
    # A formatted short label translated with little change is not "too similar".
    label = "<style id='1'>10</style><style id='3'>5 </style>Web Attack - XSS"
    translated = "<style id='1'>10</style><style id='3'>5 </style>Web 攻击 - XSS"
    assert runner.validate_translation_output(label, translated, {}, long_counter)["usable"]
    # Placeholder checks still apply to verbatim bylines.
    item = {"required_placeholders": [{"token": "{v1}", "count": 1}, {"token": "{v2}", "count": 1}]}
    dropped = "Lisa Liu, Gints Engelen{v2}, Timothy Lynar {l.liu, t.lynar, d.essam}@adfa.edu.au"
    assert not runner.validate_translation_output(byline, dropped, item, long_counter)["usable"]


class _UnsupportedParameter(Exception):
    status_code = 400

    def __init__(self, param, message):
        super().__init__(f"Error code: 400 - {message}")
        self.body = {"error": {"message": message, "type": "invalid_request_error", "param": param, "code": "unsupported_parameter"}}


class _FakeCompletions:
    def __init__(self, reject):
        self.reject = set(reject)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        for name in self.reject:
            if name in kwargs:
                if name == "max_tokens":
                    raise _UnsupportedParameter(
                        "max_tokens",
                        "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.",
                    )
                if name == "max_completion_tokens":
                    raise _UnsupportedParameter(
                        "max_completion_tokens",
                        "Unsupported parameter: 'max_completion_tokens' is not supported with this model. Use 'max_tokens' instead.",
                    )
                raise _UnsupportedParameter(name, f"Unsupported value: '{name}' does not support 0.3 with this model.")
        return "ok"


def _fake_client(reject):
    completions = _FakeCompletions(reject)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_completion_adapts_to_unsupported_token_and_sampling_parameters():
    client, completions = _fake_client({"max_tokens"})
    state = {"token_param": "max_tokens"}
    assert runner.create_completion_adapting(client, state, 4096, model="m", messages=[]) == "ok"
    assert [("max_tokens" in c, c.get("max_completion_tokens")) for c in completions.calls] == [(True, None), (False, 4096)]
    assert state["token_param"] == "max_completion_tokens"
    # Learned for the rest of the job: the next request goes straight through.
    runner.create_completion_adapting(client, state, 4096, model="m", messages=[])
    assert len(completions.calls) == 3 and "max_tokens" not in completions.calls[2]

    # A previously learned setting can be reversed when the provider routes to
    # a backend that now rejects max_completion_tokens.
    client, completions = _fake_client({"max_completion_tokens"})
    state = {"token_param": "max_completion_tokens"}
    assert runner.create_completion_adapting(client, state, 4096, model="m", messages=[]) == "ok"
    assert [
        ("max_completion_tokens" in call, call.get("max_tokens"))
        for call in completions.calls
    ] == [(True, None), (False, 4096)]
    assert state["token_param"] == "max_tokens"

    reverse_message = "Unsupported parameter: 'max_completion_tokens' is not supported with this model. Use 'max_tokens' instead."
    assert runner.parameter_adaptation(_UnsupportedParameter("max_completion_tokens", reverse_message)) == "max_tokens"
    assert runner.parameter_adaptation(_UnsupportedParameter(None, reverse_message)) == "max_tokens"
    assert runner.parameter_adaptation(
        _UnsupportedParameter(None, "Unsupported request. Use 'max_tokens' instead of 'max_completion_tokens'.")
    ) is None

    client, completions = _fake_client({"max_tokens", "temperature"})
    state = {"token_param": "max_tokens"}
    assert runner.create_completion_adapting(client, state, 100, model="m", messages=[], temperature=0.3) == "ok"
    assert len(completions.calls) == 3 and state["drop_sampling"] is True
    assert "temperature" not in completions.calls[-1] and completions.calls[-1]["max_completion_tokens"] == 100


def test_other_errors_are_not_retried_and_the_slot_is_counted_once():
    class _NotFound(Exception):
        status_code = 404
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(_NotFound("no")))))
    budget = runner.RequestBudget(5)
    try:
        runner.request_once(budget, lambda: runner.create_completion_adapting(client, {}, 10, model="m", messages=[]))
    except _NotFound:
        pass
    else:
        raise AssertionError("404 must propagate")
    assert budget.requests == 1

    client, completions = _fake_client({"max_tokens"})
    budget = runner.RequestBudget(5)
    runner.request_once(budget, lambda: runner.create_completion_adapting(client, {"token_param": "max_tokens"}, 10, model="m", messages=[]))
    assert budget.requests == 1 and len(completions.calls) == 2, "the no-cost rejected attempt shares one counted slot"
    assert runner.parameter_adaptation(ConnectionError("x")) is None


if __name__ == "__main__":
    test_first_page_author_block_is_preserved()
    test_completion_adapts_to_unsupported_token_and_sampling_parameters()
    test_other_errors_are_not_retried_and_the_slot_is_counted_once()
    test_verbatim_outputs_and_existing_short_label_policy()
    test_urls_emails_and_author_lines_may_stay_verbatim()
    test_provider_message_is_only_a_fixed_category()
    test_body_text_near_the_page_top_is_not_a_stamp()
    test_fatal_error_log_uses_the_fixed_summary()
    test_punctuation_only_english_echo_is_rejected_everywhere()
    test_reference_region_ends_at_numbered_and_titled_sections()
    test_bare_style_tags_are_rejected()
    test_repair_single_pass_never_resends_after_truncation()
    test_repair_batch_error_keeps_earlier_batch_results()
    test_style_markup_is_not_counted_as_untranslated_english()
    test_source_literal_lookalikes_may_be_kept_verbatim()
    test_placeholder_diagnostics_are_redacted_before_truncation()
    test_log_summary_never_keeps_echoed_metadata_or_bodies()
    test_ieee_xplore_in_body_text_is_translated_but_edge_stamp_is_preserved()
    test_verbatim_outputs_are_kept_without_content_rules()
    test_repeated_running_headers_are_preserved_even_when_labeled_body_text()
    test_repeated_marginal_threshold_scales_with_document_length()
    test_formula_placeholder_moved_into_style_span_is_rejected()
    test_fragment_links_name_item_ids_in_the_same_request()
    test_kept_abbreviations_are_not_counted_as_untranslated_english()
    test_tiny_labels_skip_the_length_ratio_check()
    test_roman_numeral_reference_headings_are_detected()
    test_diagnostics_log_keeps_only_a_bounded_redacted_error_summary()
    print("review fix regression cases passed")
