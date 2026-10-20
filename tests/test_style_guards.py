"""Unit regressions for rich-text style guards and citation boundaries (#16).

Run with: python tests/test_style_guards.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
RUNNER_PATH = ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
guards = runner._style_guards()


def rich(token: str, text: str, instruction: str = "") -> SimpleNamespace:
    character = SimpleNamespace(char_unicode=text)
    composition = SimpleNamespace(
        pdf_character=[character],
        pdf_style=SimpleNamespace(
            graphic_state=SimpleNamespace(passthrough_per_char_instruction=instruction)
        ),
    )
    return SimpleNamespace(
        left_placeholder=f"<style id='{token}'>",
        right_placeholder="</style>",
        composition=composition,
    )


def formula(token: str) -> SimpleNamespace:
    return SimpleNamespace(placeholder=f"{{{token}}}")


def test_link_colour_detection_accepts_blue_and_rejects_black() -> None:
    assert guards.is_link_style("0 0 1 RG 0 0 1 rg")
    assert guards.is_link_style("0.0000000000 0.4784313725 1.0000000000 rg")
    assert guards.is_link_style("1 1 0 0 k")  # CMYK pure blue
    assert not guards.is_link_style("")
    assert not guards.is_link_style("0 g 0 G")
    assert not guards.is_link_style("0.2 0.2 0.2 rg")


def test_citation_text_covers_numbers_references_and_markers() -> None:
    for text in ("6", "[40]", "(3)", "Fig. 6", "Table 2", "Eq. (3)", "图 3", "第 2 节"):
        assert guards.is_citation_text(text), text
    for text in ("local styles", "restoration", "This paper studies", "HTTP"):
        assert not guards.is_citation_text(text), text

    # Only numbers and bracketed bibliography indices are forced verbatim;
    # "Fig. 6" may become "图 6" while staying inside its link-coloured span.
    assert guards.is_protected_span("6")
    assert guards.is_protected_span("[40]")
    assert not guards.is_protected_span("Fig. 6")
    assert guards.is_preserved_span("Fig. 6", "0 0 1 RG 0 0 1 rg")
    assert not guards.is_preserved_span("Fig. 6", "0 g 0 G")


def test_simplify_keeps_citation_and_formula_but_unwraps_local_styles() -> None:
    placeholders = [
        rich("1", "local styles"),
        rich("3", "6", "0 0 1 RG 0 0 1 rg"),
        rich("5", "restoration"),
        formula("7"),
    ]
    source = (
        "We study <style id='1'>local styles</style> in "
        "<style id='3'>6</style>{v7} and <style id='5'>restoration</style>."
    )
    simplified, kept, report = guards.simplify_rich_text_source(source, placeholders)
    assert simplified == "We study local styles in <style id='3'>6</style>{v7} and restoration."
    assert report["removed_style_tags"] == 4
    assert report["kept_style_tags"] == 2
    assert [getattr(item, "left_placeholder", None) for item in kept] == [
        "<style id='3'>",
        None,
    ]
    assert guards.protected_span_specs(placeholders) == [
        {"open": "<style id='3'>", "close": "</style>", "text": "6"}
    ]


def test_citation_span_must_keep_its_own_text() -> None:
    specs = [{"open": "<style id='3'>", "close": "</style>", "text": "6"}]
    good = "检测器如图<style id='3'>6</style>所示，在实验中表现最好。"
    assert guards.citation_span_issues(good, specs) == []
    moved = "检测器如图6<style id='3'>所示，在实验中表现最好</style>。"
    issues = guards.citation_span_issues(moved, specs)
    assert issues and issues[0]["reason"] == "citation_span_changed"
    missing = "检测器如图6所示，在实验中表现最好。"
    assert guards.citation_span_issues(missing, specs)[0]["reason"] == "citation_span_missing"


def test_translation_validation_rejects_a_moved_citation_even_with_equal_counts() -> None:
    source = (
        "The loss <style id='1'>decreases quickly</style> as reported in "
        "<style id='3'>[40]</style> for every method in the study."
    )
    item = {
        "required_placeholders": [
            {"token": "<style id='1'>", "count": 1},
            {"token": "<style id='3'>", "count": 1},
            {"token": "</style>", "count": 2},
        ],
        "protected_spans": [
            {"open": "<style id='3'>", "close": "</style>", "text": "[40]"}
        ],
    }
    good = "损失<style id='1'>快速下降</style>，如<style id='3'>[40]</style>中针对每种方法所述。"
    result = runner.validate_translation_output(source, good, item, lambda text: max(1, len(text) // 3))
    assert result["usable"], result
    moved = "损失<style id='1'>快速下降</style>，如[40]<style id='3'>中针对每种方法所述</style>。"
    result = runner.validate_translation_output(source, moved, item, lambda text: max(1, len(text) // 3))
    assert not result["usable"]
    assert result["reason"].startswith("译文中的引用样式")
    assert any(
        issue.get("reason") == "citation_span_changed"
        for issue in result["checks"]["placeholder_issues"]
    )


def test_structural_failure_only_covers_restore_and_placeholder_problems() -> None:
    assert runner._is_structural_quality_failure(
        {"placeholder_issues": [{"reason": "citation_span_changed"}]}
    )
    assert runner._is_structural_quality_failure({"apply_error": True})
    assert runner._is_structural_quality_failure({"restore_failed": True})
    assert not runner._is_structural_quality_failure({"non_empty": False})
    assert not runner._is_structural_quality_failure({"fragment_group_invalid": True})


def test_style_related_issue_selects_simplification_only_for_styles() -> None:
    assert guards.style_related_issue(
        {"placeholder_issues": [{"token": "<style id='1'>", "expected": 1, "actual": 0}]}
    )
    assert guards.style_related_issue(
        {"placeholder_issues": [{"reason": "citation_span_changed"}]}
    )
    assert guards.style_related_issue({"apply_error": True})
    assert not guards.style_related_issue(
        {"placeholder_issues": [{"token": "{v1}", "expected": 1, "actual": 0}]}
    )
    assert not guards.style_related_issue({"non_empty": False})


if __name__ == "__main__":
    test_link_colour_detection_accepts_blue_and_rejects_black()
    test_citation_text_covers_numbers_references_and_markers()
    test_simplify_keeps_citation_and_formula_but_unwraps_local_styles()
    test_citation_span_must_keep_its_own_text()
    test_translation_validation_rejects_a_moved_citation_even_with_equal_counts()
    test_structural_failure_only_covers_restore_and_placeholder_problems()
    test_style_related_issue_selects_simplification_only_for_styles()
    print("style guard regression cases passed")
