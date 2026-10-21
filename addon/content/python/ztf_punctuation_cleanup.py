"""Remove duplicated punctuation left after placeholder restoration.

BabelDOC restores source placeholders (citation brackets, formulas, inline
styles) after our text-level punctuation normalisation, so a model output such
as "[4, 7, 12, 18], ，阻碍" keeps both the source ASCII comma and the Chinese
comma.  This module removes the redundant comma glyph from the finished PDF
text layer and leaves the Chinese punctuation and every other glyph untouched.
"""

from __future__ import annotations

import os

import pymupdf


_CJK_PUNCTUATION = "，；。！？、"
_COMMA_LIKE = ",，"
_WHITESPACE = " \t\r\n\u3000"


def _text_chars(page: pymupdf.Page) -> list[tuple[str, pymupdf.Rect]]:
    chars: list[tuple[str, pymupdf.Rect]] = []
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    try:
                        chars.append((char["c"], pymupdf.Rect(char["bbox"])))
                    except (KeyError, TypeError, ValueError):
                        continue
    return chars


def _removal_indices(text: str) -> list[int]:
    """Indexes of comma glyphs that duplicate a following CJK punctuation."""
    removals: list[int] = []
    length = len(text)
    index = 0
    while index < length:
        current = text[index]
        if current not in _COMMA_LIKE:
            index += 1
            continue
        cursor = index + 1
        while cursor < length and text[cursor] in _WHITESPACE:
            cursor += 1
        if cursor >= length:
            index += 1
            continue
        following = text[cursor]
        if following in _COMMA_LIKE:
            # Two commas in a row: keep the Chinese one when there is one.
            if current == "," and following == "，":
                removals.append(index)
            else:
                removals.append(cursor)
            index = cursor + 1
            continue
        if current == "," and following in _CJK_PUNCTUATION:
            removals.append(index)
            index = cursor + 1
            continue
        index += 1
    # A run of identical Chinese punctuation keeps only its first glyph.
    for position in range(1, length):
        if (
            text[position] == text[position - 1]
            and text[position] in _CJK_PUNCTUATION
            and position not in removals
        ):
            removals.append(position)
    return removals


def _redaction_rect(rect: pymupdf.Rect) -> pymupdf.Rect:
    """The comma's bounding box, inset so it cannot cover a touching glyph.

    MuPDF removes a glyph when a redaction rectangle covers most of its bbox.
    Touching CJK neighbours share the bbox edge, so insetting by a fraction of
    a point removes the comma without touching the Chinese punctuation.
    """
    inset = 0.2
    return pymupdf.Rect(
        rect.x0 + inset, rect.y0 + inset, rect.x1 - inset, rect.y1 - inset
    )


def _translated_offset(source_path: str, output_path: str) -> float:
    """Left edge of the translated half in a bilingual PDF, else 0."""
    try:
        with pymupdf.open(source_path) as source, pymupdf.open(output_path) as output:
            if not len(source) or not len(output):
                return 0.0
            source_width = float(source[0].rect.width)
            output_width = float(output[0].rect.width)
            if source_width > 0 and output_width >= source_width * 1.5:
                return output_width / 2.0
    except Exception:
        return 0.0
    return 0.0


def clean_duplicate_punctuation(output_path: str, source_path: str = "") -> dict:
    """Delete redundant comma glyphs from the finished PDF and save in place."""
    offset = _translated_offset(source_path, output_path) if source_path else 0.0
    document = pymupdf.open(output_path)
    stats: dict = {"pages": 0, "removed": 0, "failed": False}
    try:
        modified = False
        for page_index in range(document.page_count):
            page = document[page_index]
            chars = _text_chars(page)
            if not chars:
                continue
            text = "".join(value for value, _ in chars)
            removals = [
                index for index in _removal_indices(text)
                if index < len(chars)
                and (
                    offset <= 0.0
                    or (chars[index][1].x0 + chars[index][1].x1) / 2.0 >= offset
                )
            ]
            if not removals:
                continue
            stats["pages"] += 1
            for index in removals:
                try:
                    page.add_redact_annot(_redaction_rect(chars[index][1]), fill=False)
                except Exception:
                    continue
            try:
                page.apply_redactions(images=0, graphics=0)
            except Exception:
                stats["failed"] = True
                continue
            stats["removed"] += len(removals)
            modified = True
        if modified:
            temporary = output_path + ".puncttmp"
            try:
                document.save(temporary, garbage=3, deflate=True)
            except Exception:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
                raise
            document.close()
            document = None
            os.replace(temporary, output_path)
        return stats
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
