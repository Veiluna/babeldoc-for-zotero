"""Guard rich-text spans while repairing a paragraph (#16).

BabelDOC 0.6.4 turns every style run that differs from the paragraph's base
style into a pair of `<style id='N'>` / `</style>` placeholders
(`ILTranslator.get_translate_input`, `disable_rich_text_translate=False`). A
model can keep the number of tags while moving them, so a count check alone
cannot protect a citation: the blue span may end up around translated prose
while the original number loses its colour.

This module keeps two kinds of runs:

* *protected* runs are citation-like text (numbers, bracketed references and
  figure/table/equation markers). Their exact source text must survive inside
  the same span, so a broken citation boundary is detectable.
* *preserved* runs are link-coloured runs or protected runs. They keep their
  style when the one repair pass simplifies the paragraph: only ordinary
  italic/bold/local styles are unwrapped into the paragraph's body style.

The module intentionally works on duck-typed BabelDOC placeholder objects so
it can be copied next to the runner and tested without importing BabelDOC.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


_STYLE_TAG_RE = re.compile(r"<style\b[^>]*>|</style\s*>", re.I)
_STYLE_OPEN_RE = re.compile(r"<style id='(?P<id>[^']+)'>", re.I)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_NUMBER_RE = re.compile(r"^[\s\[\](){}0-9.,;:!?+\-\u2013\u2014/]+$")
_MARKER_RE = re.compile(
    r"^(?:refs?|references?|figs?|figures?|tables?|tabs?|eqs?|equations?|"
    r"secs?|sections?|chapters?|appendix|算法|图|表|式|第)\.?"
    r"\s*[\[(]?\s*[A-Za-z]?\d[\w.\-\u2013\u2014]*\s*[\])]?(?:\s*[节章])?$",
    re.I,
)


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def _span_text(placeholder: Any) -> str:
    composition = getattr(placeholder, "composition", None)
    characters = getattr(composition, "pdf_character", ()) or ()
    return "".join(str(getattr(char, "char_unicode", "") or "") for char in characters)


def _span_instruction(placeholder: Any) -> str:
    composition = getattr(placeholder, "composition", None)
    style = getattr(composition, "pdf_style", None)
    state = getattr(style, "graphic_state", None)
    return str(getattr(state, "passthrough_per_char_instruction", "") or "")


def _rgb_from_instruction(instruction: str) -> tuple[float, float, float] | None:
    """Return the last fill colour in a BabelDOC graphic-state instruction."""
    tokens = str(instruction or "").split()
    colour: tuple[float, float, float] | None = None
    numbers: list[float] = []
    for token in tokens:
        try:
            numbers.append(float(token))
            continue
        except ValueError:
            pass
        operator = token.lower()
        if operator in {"rg", "g", "k"} and numbers:
            if operator == "rg" and len(numbers) >= 3:
                colour = (numbers[-3], numbers[-2], numbers[-1])
            elif operator == "g":
                colour = (numbers[-1], numbers[-1], numbers[-1])
            elif operator == "k" and len(numbers) >= 4:
                cyan, magenta, yellow, black = numbers[-4:]
                colour = (
                    (1.0 - cyan) * (1.0 - black),
                    (1.0 - magenta) * (1.0 - black),
                    (1.0 - yellow) * (1.0 - black),
                )
        numbers = []
    return colour


def is_link_style(instruction: str) -> bool:
    """Whether a style's fill colour is a hyperlink blue rather than body black."""
    colour = _rgb_from_instruction(instruction)
    if colour is None:
        return False
    red, green, blue = colour
    return blue >= 0.55 and blue - max(red, green) >= 0.25


def is_citation_text(text: Any) -> bool:
    """Numbers, bracketed references and figure/table/equation markers."""
    compact = _compact(text)
    if not compact or len(compact) > 40:
        return False
    return bool(_NUMBER_RE.fullmatch(compact) or _MARKER_RE.fullmatch(compact))


def is_protected_span(text: Any, instruction: str = "") -> bool:
    """A span whose exact text must stay bound to its style.

    Exact matching is limited to numeric/bracketed citation text: a link span
    such as "Fig. 6" may legitimately be translated to "图 6", so it is kept in
    the simplified input (when link-coloured) but not forced to be verbatim.
    That still catches the reported failure, where a blue "6" span was moved
    around ordinary translated prose.
    """
    del instruction  # Colour alone is not enough to require an exact text match.
    compact = _compact(text)
    return bool(compact) and bool(_NUMBER_RE.fullmatch(compact))


def is_preserved_span(text: Any, instruction: str = "") -> bool:
    """A span kept while simplifying ordinary local styles."""
    return is_protected_span(text, instruction) or is_link_style(instruction)


def _rich_placeholders(placeholders: Iterable[Any]) -> list[Any]:
    result = []
    for placeholder in placeholders or ():
        if hasattr(placeholder, "left_placeholder") and hasattr(
            placeholder, "right_placeholder"
        ):
            result.append(placeholder)
    return result


def protected_span_specs(placeholders: Iterable[Any]) -> list[dict[str, str]]:
    """Describe citation spans for output validation; empty when none exist."""
    specs: list[dict[str, str]] = []
    for placeholder in _rich_placeholders(placeholders):
        text = _span_text(placeholder)
        if not is_protected_span(text, _span_instruction(placeholder)):
            continue
        specs.append(
            {
                "open": str(getattr(placeholder, "left_placeholder", "") or ""),
                "close": str(getattr(placeholder, "right_placeholder", "") or ""),
                "text": text,
            }
        )
    return specs


def _normalized_span_text(text: str) -> str:
    return _compact(text)


def citation_span_issues(
    output: str, specs: Iterable[dict[str, str]]
) -> list[dict[str, Any]]:
    """Report citation spans whose text changed or now covers ordinary prose."""
    specs_by_open = {
        str(spec.get("open", "")): spec
        for spec in specs or ()
        if str(spec.get("open", ""))
    }
    if not specs_by_open:
        return []
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack: list[tuple[str, int]] = []
    for match in _STYLE_TAG_RE.finditer(output or ""):
        token = match.group(0)
        if token.lower().startswith("</"):
            if not stack:
                continue
            opening, content_start = stack.pop()
            spec = specs_by_open.get(opening)
            if spec is None:
                continue
            seen.add(opening)
            actual = output[content_start:match.start()]
            if _normalized_span_text(actual) != _normalized_span_text(spec.get("text", "")):
                issues.append(
                    {
                        "token": opening,
                        "reason": "citation_span_changed",
                        "expected": spec.get("text", ""),
                        "actual": actual[:40],
                    }
                )
            elif _CJK_RE.search(actual):
                issues.append(
                    {
                        "token": opening,
                        "reason": "citation_span_changed",
                        "expected": spec.get("text", ""),
                        "actual": actual[:40],
                    }
                )
        else:
            opening = token
            # Preserve the canonical spelling when the source used spacing/case
            # variants that BabelDOC would not restore anyway.
            if opening not in specs_by_open:
                normalized = _STYLE_OPEN_RE.match(opening)
                if normalized is not None:
                    opening = f"<style id='{normalized.group('id')}'>"
            stack.append((opening, match.end()))
    for opening in specs_by_open:
        if opening not in seen:
            issues.append(
                {
                    "token": opening,
                    "reason": "citation_span_missing",
                    "expected": specs_by_open[opening].get("text", ""),
                }
            )
    return issues


def simplify_rich_text_source(
    source: str, placeholders: Iterable[Any]
) -> tuple[str, list[Any], dict[str, int]]:
    """Unwrap ordinary local styles, keeping citation and formula spans.

    Returns the simplified text, the placeholders that remain (formula
    placeholders and preserved rich-text spans), and a small report.
    """
    placeholders = list(placeholders or ())
    rich_by_open = {
        str(getattr(placeholder, "left_placeholder", "") or ""): placeholder
        for placeholder in _rich_placeholders(placeholders)
    }
    kept_rich: set[int] = set()
    parts: list[str] = []
    stack: list[bool] = []
    position = 0
    removed = 0
    kept = 0
    for match in _STYLE_TAG_RE.finditer(source or ""):
        token = match.group(0)
        parts.append(source[position:match.start()])
        position = match.end()
        if token.lower().startswith("</"):
            if stack:
                keep = stack.pop()
            else:
                keep = True
            if keep:
                kept += 1
                parts.append(token)
            else:
                removed += 1
            continue
        placeholder = rich_by_open.get(token)
        keep = True
        if placeholder is not None:
            keep = is_preserved_span(
                _span_text(placeholder), _span_instruction(placeholder)
            )
            if keep:
                kept_rich.add(id(placeholder))
        stack.append(keep)
        if keep:
            kept += 1
            parts.append(token)
        else:
            removed += 1
    parts.append(source[position:])
    kept_placeholders = [
        placeholder
        for placeholder in placeholders
        if id(placeholder) in kept_rich
        or not (
            hasattr(placeholder, "left_placeholder")
            and hasattr(placeholder, "right_placeholder")
        )
    ]
    return "".join(parts), kept_placeholders, {
        "removed_style_tags": removed,
        "kept_style_tags": kept,
        "kept_placeholders": len(kept_placeholders),
    }


def style_related_issue(checks: Any) -> bool:
    """Whether a failed main-pass output may improve with simplified styles."""
    if not isinstance(checks, dict):
        return False
    if checks.get("apply_error") or checks.get("restore_failed"):
        return True
    issues = checks.get("placeholder_issues") or []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        reason = str(issue.get("reason", ""))
        token = str(issue.get("token", "")).strip().lower()
        if reason in {
            "citation_span_changed",
            "citation_span_missing",
            "unexpected_style_tag",
            "unsupported_style_opener",
            "formula_nesting_changed",
        }:
            return True
        if token.startswith("<style") or token.startswith("</style"):
            return True
    return False
