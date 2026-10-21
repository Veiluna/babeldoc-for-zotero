"""Content rules for table cells: keep identifiers, translate meaningful text."""

from __future__ import annotations

import re


_ABBREVIATION_RE = re.compile(r"^(?:[A-Z]{2,}[0-9]*|[A-Z][a-z]*[A-Z][A-Za-z0-9]*)$")
_CODE_RE = re.compile(r"(?:https?://|www\.|[_/\\@#$%^&*+=<>{}|~]|\b\d)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_NUMBER_RE = re.compile(r"^[\d\s.,%+\-–—/()]+$")
_SENTENCE_END_RE = re.compile(r"[.!?;:。！？；：]\s*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def _normalize(text: object) -> str:
    return " ".join(str(text or "").split())


def is_identifier(text: str) -> bool:
    """Numbers, abbreviations, URLs and code-like identifiers stay verbatim."""
    value = _normalize(text)
    if not value:
        return True
    if _NUMBER_RE.fullmatch(value):
        return True
    if _CODE_RE.search(value):
        return True
    if " " not in value and re.search(r"[_\-]", value):
        return True
    tokens = value.split()
    if tokens and all(
        token.isupper() or token.isdigit() or _ABBREVIATION_RE.fullmatch(token)
        for token in re.split(r"[\s/\-]+", value)
        if token
    ):
        return True
    return False


def classify_table_cell(text: object, role: str = "body") -> dict[str, str]:
    """Decide whether a table cell is translated, and record why.

    Header cells carry category names ("Network Scans"), so ordinary words are
    translated even when short.  Body cells keep short values (names, products,
    metrics) and translate only full sentences/descriptions.
    """
    value = _normalize(text)
    if not value:
        return {"action": "keep", "reason": "empty"}
    if is_identifier(value):
        return {"action": "keep", "reason": "identifier"}
    words = _WORD_RE.findall(value)
    if role == "header":
        if not words:
            return {"action": "keep", "reason": "no_words"}
        return {"action": "translate", "reason": "header"}
    if len(words) <= 3:
        return {"action": "keep", "reason": "short_value"}
    if len(words) >= 4 or _SENTENCE_END_RE.search(value):
        return {"action": "translate", "reason": "sentence"}
    return {"action": "translate", "reason": "descriptive"}


def meaningful_chinese(source: object, translated: object, min_cjk: int = 2) -> bool:
    """Whether the model returned real Chinese rather than an echo or noise."""
    translated_text = _normalize(translated)
    if not translated_text:
        return False
    if translated_text == _normalize(source):
        return False
    if translated_text.isdigit():
        return False
    return len(_CJK_WORD_RE.findall(translated_text)) >= 1 and len(
        _CJK_RE.findall(translated_text)
    ) >= min_cjk
