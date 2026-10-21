"""Restore hyperlinks in a BabelDOC monolingual PDF.

BabelDOC rebuilds pages with show_pdf_page and never copies link annotations.
This helper reads every source link, locates the corresponding citation text
on the re-typeset page, inserts a new annotation at the actual translated
text, and writes a mapping file that the future bilingual output (#19) can
reuse.  It depends only on PyMuPDF and the standard library.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from typing import Any, Callable, Iterable

import pymupdf


MAPPING_VERSION = 1

KIND_NONE = 0
KIND_GOTO = 1
KIND_URI = 2
KIND_LAUNCH = 3
KIND_NAMED = 4
KIND_GOTOR = 5
_KIND_NAMES = {0: "none", 1: "goto", 2: "uri", 3: "launch", 4: "named", 5: "gotor"}

_URI_PREFIX_RE = re.compile(r"^(?:https?|ftp|file|mailto):", re.I)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/\S+")
_BRACKET_RE = re.compile(r"\[\s*(\d{1,4})\s*\]")
_PAREN_RE = re.compile(r"\(\s*(\d{1,4}[a-zA-Z]?)\s*\)")
_LABELLED_NUMBER_RE = re.compile(
    r"(?i)\b(Fig(?:ure)?|Table|Tab|Eq(?:uation)?|Sec(?:tion)?)\.?\s*"
    r"\(?(\d{1,4}[a-zA-Z]?(?:\([a-zA-Z0-9]+\))?)\)?"
)
_NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,4}(?:\([a-zA-Z0-9]+\))?[a-zA-Z]?)(?![\w])")
# 复合编号（3.1、3.4.2、12.1）；链接矩形有时只覆盖其中一段（".1" 或 "3"）。
_COMPOUND_NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){1,3})(?![\w.])")
# 引用簇（[4, 7, 18] / (4, 7, 18)），用于按同段顺序配对数字引用。
_CITATION_GROUP_RE = re.compile(r"[\[［(（]\s*\d{1,4}(?:\s*[,;，；]\s*\d{1,4})+\s*[\]］)）]")
_ROMAN_RE = re.compile(r"(?<![\w.])([IVXLCDM]{1,7})(?![\w])")
_ORCID_RE = re.compile(r"^https?://orcid\.org/", re.I)
_MEANINGFUL_TEXT_RE = re.compile(r"[0-9A-Za-z\u00C0-\u024F]")
_SENTENCE_PUNCTUATION = ".,;:)]}、，。；：）】》"


def _as_rect(value: Any) -> pymupdf.Rect:
    if isinstance(value, pymupdf.Rect):
        return pymupdf.Rect(value)
    return pymupdf.Rect(*[float(part) for part in value])


def _rect_list(rect: Any) -> list[float]:
    rect = pymupdf.Rect(rect)
    return [round(float(rect.x0), 3), round(float(rect.y0), 3),
            round(float(rect.x1), 3), round(float(rect.y1), 3)]


def _point_list(point: Any) -> list[float]:
    point = pymupdf.Point(point)
    return [round(float(point.x), 3), round(float(point.y), 3)]


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _valid_point(value: Any) -> bool:
    try:
        point = pymupdf.Point(value)
    except (TypeError, ValueError, IndexError):
        return False
    return _finite(point.x) and _finite(point.y)


def _page_text_rect(page: Any) -> pymupdf.Rect:
    """Unrotated CropBox-relative text area (text/link insertion space)."""
    crop = page.cropbox
    return pymupdf.Rect(0.0, 0.0, float(crop.width), float(crop.height))


def _visible_rect_to_unrotated(rect: Any, page: Any) -> pymupdf.Rect:
    """Convert a Page.get_links() rectangle to text coordinates.

    get_links returns the visible page space (rotation applied); text
    extraction, paragraph boxes and insert_link use unrotated space.
    CropBox offsets are already common to both coordinate systems.
    """
    return pymupdf.Rect(rect) * page.derotation_matrix


def _visible_point_to_unrotated(point: Any, page: Any) -> pymupdf.Point:
    return pymupdf.Point(point) * page.derotation_matrix


def _unrotated_rect_to_pdf(rect: Any, page: Any) -> pymupdf.Rect:
    """Unrotated CropBox-relative text rect -> PDF annotation user space."""
    rect = pymupdf.Rect(rect)
    crop = page.cropbox
    media = page.mediabox
    x0 = rect.x0 + crop.x0
    x1 = rect.x1 + crop.x0
    y0 = media.y1 - (rect.y1 + crop.y0)
    y1 = media.y1 - (rect.y0 + crop.y0)
    return pymupdf.Rect(x0, y0, x1, y1)


def _unrotated_point_to_pdf(point: Any, page: Any) -> pymupdf.Point:
    point = pymupdf.Point(point)
    crop = page.cropbox
    media = page.mediabox
    return pymupdf.Point(point.x + crop.x0, media.y1 - (point.y + crop.y0))


def _number_text(value: float) -> str:
    return ("%.4f" % float(value)).rstrip("0").rstrip(".") or "0"


def _pdf_rect_string(rect: Any) -> str:
    rect = pymupdf.Rect(rect)
    return "[{} {} {} {}]".format(*[_number_text(value) for value in
                                    (rect.x0, rect.y0, rect.x1, rect.y1)])


def _normalized_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def _preview(text: str, limit: int = 60) -> str:
    return _normalized_text(text)[:limit]


def _is_citation_number(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}(?:\([a-zA-Z0-9]+\))?[a-zA-Z]?", text or ""))


def _label_from_line(line: str) -> str:
    labelled = _LABELLED_NUMBER_RE.search(line or "")
    if not labelled:
        return ""
    label = labelled.group(1).lower()
    if label.startswith("fig"):
        return "figure"
    if label.startswith("tab"):
        return "table"
    if label.startswith("eq"):
        return "equation"
    if label.startswith("sec"):
        return "section"
    return ""


def _compound_from_under_or_line(under: str, line: str) -> str:
    """Recover a full compound number (3.1) even when the link rect clips it ('.1')."""
    match = _COMPOUND_NUMBER_RE.search(under or "")
    if match:
        return match.group(1)
    fragment = re.sub(r"\D", "", under or "")
    if not fragment:
        return ""
    for candidate in _COMPOUND_NUMBER_RE.finditer(line or ""):
        digits = candidate.group(1).replace(".", "")
        if fragment in digits and len(digits) <= len(fragment) + 2:
            return candidate.group(1)
    return ""


def _anchor_from_text(under: str, line: str) -> dict[str, Any]:
    """Describe the source clickable anchor from the text under/around it.

    The complete text under the link rect wins.  Compound numbers are recovered
    from the surrounding line when the rectangle only covers a fragment
    ( ".1" or "3" of "3.1"), and citation groups keep the number order.
    """
    under = _normalized_text(under)
    line = _normalized_text(line)
    bare = under.strip(_SENTENCE_PUNCTUATION).strip()

    # "Figure 3" names the same number as its clickable text even when the
    # same line band also mentions "Sect. 3.1"; it must not become "3.1".
    labelled_near = _LABELLED_NUMBER_RE.search(line)
    bare_number = re.fullmatch(r"(\d{1,4})", bare or "")
    if (
        labelled_near
        and bare_number
        and labelled_near.group(2).split("(")[0] == bare_number.group(1)
        # "Section 3.1" keeps its compound tail right after the captured "3".
        and line[labelled_near.end():labelled_near.end() + 1] != "."
    ):
        number = labelled_near.group(2)
        return {"type": "number", "text": number, "number": number,
                "label": _label_from_line(line), "context": line[:160]}

    compound = _compound_from_under_or_line(under, line)
    if compound and not _english_prose_around(under, compound):
        return {"type": "number", "text": compound, "number": compound,
                "label": _label_from_line(line), "context": line[:160]}

    bracket = _BRACKET_RE.search(under)
    if bracket and not _MEANINGFUL_TEXT_RE.search(
            under.replace(bracket.group(0), "").strip(_SENTENCE_PUNCTUATION)):
        return {"type": "bracket", "text": "[%s]" % bracket.group(1),
                "number": bracket.group(1), "label": "reference"}

    paren = _PAREN_RE.search(under)
    if paren and not _MEANINGFUL_TEXT_RE.search(
            under.replace(paren.group(0), "").strip(_SENTENCE_PUNCTUATION)):
        return {"type": "paren", "text": "(%s)" % paren.group(1),
                "number": paren.group(1), "label": "equation"}

    if bare and _is_citation_number(bare):
        return {"type": "number", "text": bare, "number": bare,
                "label": _label_from_line(line)}

    labelled = _LABELLED_NUMBER_RE.search(line)
    if labelled:
        token = labelled.group(2)
        number = _NUMBER_RE.search(under)
        if number and _is_citation_number(number.group(1)):
            token = number.group(1)
        return {"type": "number", "text": token, "number": token,
                "label": _label_from_line(line)}

    roman = _ROMAN_RE.search(under)
    if roman:
        return {"type": "roman", "text": roman.group(1), "number": roman.group(1),
                "label": "section"}

    number = _NUMBER_RE.search(under)
    if not number:
        # A superscript citation rect can clip the preceding letter, so the
        # extracted text reads "t10" for "strategy 10".  Accept a trailing
        # digit run in that case, but only for short clickable text.
        if len(under) <= 12:
            number = re.search(r"(?<![0-9])(\d{1,4})\s*$", under)
    if number:
        return {"type": "number", "text": number.group(1), "number": number.group(1),
                "label": ""}

    text = bare or under.strip()
    if text:
        return {"type": "text", "text": text, "number": "", "label": "",
                "context": line[:160]}
    return {"type": "graphics", "text": "", "number": "", "label": ""}


def _english_prose_around(under: str, compound: str) -> bool:
    """Whether the link rect holds English prose besides the compound number.

    A clipped fragment such as ".1" or a Chinese prefix such as "第3.1节" is
    not prose; it still belongs to the compound number anchor.
    """
    remainder = under.replace(compound, " ", 1)
    remainder = re.sub(r"[\d\s.]+", " ", remainder).strip(_SENTENCE_PUNCTUATION).strip()
    return bool(re.search(r"[A-Za-z]{2,}", remainder))


def _anchor_from_uri(under: str, uri: str) -> dict[str, Any]:
    under = _normalized_text(under)
    bracket = _BRACKET_RE.fullmatch(under)
    if bracket:
        return {"type": "bracket", "text": "[%s]" % bracket.group(1),
                "number": bracket.group(1), "label": "reference"}
    paren = _PAREN_RE.fullmatch(under)
    if paren:
        return {"type": "paren", "text": "(%s)" % paren.group(1),
                "number": paren.group(1), "label": "equation"}
    if _ORCID_RE.match(uri) or not _MEANINGFUL_TEXT_RE.search(under):
        return {"type": "graphics", "text": "", "number": "", "label": "graphics"}
    text = under.strip().rstrip(_SENTENCE_PUNCTUATION)
    if not text or len(text) < 4:
        return {"type": "graphics", "text": "", "number": "", "label": "graphics"}
    return {"type": "text", "text": text, "number": "", "label": "url"}


def _line_context(page: Any, rect: pymupdf.Rect) -> str:
    band = pymupdf.Rect(max(0.0, rect.x0 - 160), rect.y0 - 2,
                        min(_page_text_rect(page).x1, rect.x1 + 160), rect.y1 + 2)
    try:
        return page.get_textbox(band)
    except Exception:
        return ""


def _resolve_destination(doc: Any, link: dict[str, Any]) -> dict[str, Any]:
    """Resolve a source destination without trusting named-dest coordinates."""
    kind = int(link.get("kind", KIND_NONE))
    if kind == KIND_URI:
        uri = str(link.get("uri") or "")
        if not uri:
            return {"type": "invalid", "reason": "empty_uri"}
        return {"type": "uri", "uri": uri}
    if kind == KIND_GOTOR:
        return {"type": "unsupported", "reason": "external_file_link"}
    if kind == KIND_LAUNCH:
        return {"type": "unsupported", "reason": "launch_link"}
    if kind not in (KIND_GOTO, KIND_NAMED):
        return {"type": "unsupported", "reason": "link_kind_%s" % _KIND_NAMES.get(kind, kind)}

    nameddest = link.get("nameddest")
    if not nameddest and kind == KIND_NAMED:
        raw_to = link.get("to")
        if isinstance(raw_to, str):
            nameddest = raw_to
    if nameddest:
        try:
            pno, x, y = doc.resolve_link("#nameddest=%s" % nameddest)
        except Exception:
            pno, x, y = -1, float("nan"), float("nan")
        if isinstance(pno, int) and pno >= 0 and _finite(x) and _finite(y):
            return {"type": "goto", "page": pno, "point": [float(x), float(y)],
                    "nameddest": str(nameddest)}
        return {"type": "invalid", "reason": "named_destination_unresolved",
                "nameddest": str(nameddest)}

    pno = link.get("page", -1)
    point = link.get("to")
    if isinstance(pno, int) and pno >= 0 and _valid_point(point):
        try:
            target_page = doc[pno]
            unrotated = _visible_point_to_unrotated(point, target_page)
        except Exception:
            return {"type": "invalid", "reason": "target_page_unavailable"}
        return {"type": "goto", "page": pno, "point": _point_list(unrotated)}
    return {"type": "invalid", "reason": "missing_destination"}


def _paragraph_box(paragraph: dict[str, Any]) -> pymupdf.Rect | None:
    box = paragraph.get("source_box") or paragraph.get("output_box")
    if not box:
        return None
    try:
        return _as_rect(box)
    except (TypeError, ValueError):
        return None


def _find_source_paragraph(rect: pymupdf.Rect, page_index: int,
                           paragraphs: list[dict[str, Any]]) -> dict[str, Any] | None:
    best = None
    best_score = 0.0
    center = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    for paragraph in paragraphs:
        if paragraph.get("source_page") != page_index:
            continue
        box = _paragraph_box(paragraph)
        if box is None:
            continue
        inter = pymupdf.Rect(rect) & box
        area = max(0.0, inter.width) * max(0.0, inter.height)
        if area <= 0:
            continue
        score = area / max(1.0, rect.width * rect.height)
        if box.contains(center):
            score += 1.0
        if score > best_score:
            best_score = score
            best = paragraph
    return best if best_score >= 0.35 else None


def _find_target_paragraph(point: list[float] | None, page_index: int,
                           paragraphs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not point:
        return None
    x, y = float(point[0]), float(point[1])
    best = None
    best_area = float("inf")
    for paragraph in paragraphs:
        if paragraph.get("source_page") != page_index:
            continue
        box = _paragraph_box(paragraph)
        if box is None:
            continue
        if box.x0 - 3 <= x <= box.x1 + 3 and box.y0 - 8 <= y <= box.y1 + 3:
            area = max(0.0, box.width) * max(0.0, box.height)
            if area < best_area:
                best_area = area
                best = paragraph
    return best


def _paragraph_text_bbox(paragraph: dict[str, Any]) -> pymupdf.Rect | None:
    box = paragraph.get("output_text_bbox") or paragraph.get("output_box")
    if not box:
        return None
    try:
        return _as_rect(box)
    except (TypeError, ValueError):
        return None


def _region_for_link(paragraph: dict[str, Any] | None, page: Any) -> pymupdf.Rect:
    if paragraph is not None:
        box = _paragraph_text_bbox(paragraph)
        if box is not None and box.width > 0 and box.height > 0:
            return pymupdf.Rect(box.x0 - 2, box.y0 - 2, box.x1 + 2, box.y1 + 2) & _page_text_rect(page)
    return _page_text_rect(page)


def _text_chars(page: Any, region: pymupdf.Rect) -> list[tuple[str, pymupdf.Rect]]:
    chars: list[tuple[str, pymupdf.Rect]] = []
    try:
        raw = page.get_text("rawdict", clip=region)
    except Exception:
        return chars
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    value = char.get("c", "")
                    box = char.get("bbox")
                    if not value or not box:
                        continue
                    try:
                        chars.append((value, pymupdf.Rect(box)))
                    except (TypeError, ValueError):
                        continue
    return chars


def _token_pattern(token: str) -> re.Pattern[str]:
    token = unicodedata.normalize("NFKC", token or "")
    parts = []
    for char in token:
        if char.isspace():
            continue
        if char == "[":
            parts.append(r"[\[\uFF3B]")
        elif char == "]":
            parts.append(r"[\]\uFF3D]")
        elif char == "(":
            parts.append(r"[(（]")
        elif char == ")":
            parts.append(r"[)）]")
        else:
            parts.append(re.escape(char))
    body = r"\s*".join(parts)
    if not body:
        return re.compile(r"(?!x)x")
    if _is_citation_number(token) and not token.startswith(("[", "(")):
        # A leading dot is part of a longer number ("3.1"); matching its tail
        # would let a citation for "1" steal the "1" of "3.1".
        return re.compile(r"(?<![0-9A-Za-z.])" + body + r"(?![0-9A-Za-z])")
    return re.compile(body, re.I)


def _match_rects(chars: list[tuple[str, pymupdf.Rect]], pattern: re.Pattern[str],
                 limit: int = 40) -> list[dict[str, Any]]:
    if not chars:
        return []
    text = "".join(value for value, _ in chars)
    matches = []
    for match in pattern.finditer(text):
        boxes = [chars[index][1] for index in range(match.start(), match.end())]
        if not boxes:
            continue
        rect = pymupdf.Rect(boxes[0])
        for box in boxes[1:]:
            rect |= box
        if rect.width <= 0.2 or rect.height <= 0.2:
            continue
        matches.append({"rect": rect, "start": match.start(), "end": match.end()})
        if len(matches) >= limit:
            break
    return matches


def _reading_order(matches: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(matches, key=lambda item: (round(item["rect"].y0, 1), item["rect"].x0))


def _find_cluster_rect(page: Any, region: pymupdf.Rect,
                       cluster: dict[str, Any]) -> pymupdf.Rect | None:
    """Locate the Nth number inside a citation cluster such as [4, 7, 18]."""
    numbers = [int(value) for value in (cluster.get("numbers") or [])]
    index = int(cluster.get("index") or 0)
    if not numbers or index >= len(numbers):
        return None
    chars = _text_chars(page, region)
    if not chars:
        return None
    text = "".join(value for value, _ in chars)
    for match in _CITATION_GROUP_RE.finditer(text):
        group_numbers = [int(value) for value in re.findall(r"\d{1,4}", match.group(0))]
        if group_numbers != numbers:
            continue
        digits = list(re.finditer(r"\d{1,4}", text[match.start():match.end()]))
        if index >= len(digits):
            continue
        number_match = digits[index]
        start = match.start() + number_match.start()
        end = match.start() + number_match.end()
        boxes = [chars[i][1] for i in range(start, min(end, len(chars)))]
        if not boxes:
            continue
        rect = pymupdf.Rect(boxes[0])
        for box in boxes[1:]:
            rect |= box
        if rect.width > 0.2 and rect.height > 0.2:
            return rect
    return None


def _anchor_match_candidates(page: Any, anchor: dict[str, Any],
                             region: pymupdf.Rect) -> list[dict[str, Any]]:
    if anchor.get("type") == "graphics":
        return []
    token = str(anchor.get("text") or "")
    if not token:
        return []
    chars = _text_chars(page, region)
    matches = _match_rects(chars, _token_pattern(token))
    if not matches and anchor.get("type") == "text":
        words = token.split()
        if len(words) > 3:
            token = " ".join(words[:3])
            matches = _match_rects(chars, _token_pattern(token))
    return _reading_order(matches)


def _relative_position(rect: pymupdf.Rect, box: pymupdf.Rect) -> tuple[float, float]:
    width = max(1e-6, box.width)
    height = max(1e-6, box.height)
    return (((rect.x0 + rect.x1) / 2 - box.x0) / width,
            ((rect.y0 + rect.y1) / 2 - box.y0) / height)


def _expected_output_point(source_rect: pymupdf.Rect, source_box: pymupdf.Rect | None,
                           output_box: pymupdf.Rect | None) -> pymupdf.Point | None:
    if source_box is None or output_box is None:
        return None
    rel_x, rel_y = _relative_position(source_rect, source_box)
    return pymupdf.Point(output_box.x0 + rel_x * output_box.width,
                         output_box.y0 + rel_y * output_box.height)


def _distance(point: pymupdf.Point, rect: pymupdf.Rect) -> float:
    dx = max(rect.x0 - point.x, 0.0, point.x - rect.x1)
    dy = max(rect.y0 - point.y, 0.0, point.y - rect.y1)
    return math.hypot(dx, dy)


def _fallback_anchor_rect(
    output_paragraph: dict[str, Any] | None,
    source_rect: pymupdf.Rect,
    expected: pymupdf.Point | None,
    anchor: dict[str, Any],
    page: Any,
) -> tuple[pymupdf.Rect | None, str]:
    """Locate a citation whose glyphs did not survive text extraction.

    BabelDOC renders some standalone numbers and roman section numbers as
    formula glyphs.  The translated page then has no extractable text for the
    citation even though it is visible.  Prefer the nearest formula box in the
    mapped paragraph; otherwise place a small rectangle at the paragraph
    relative position and record that it is a geometry fallback.
    """
    if output_paragraph is not None:
        boxes = output_paragraph.get("output_formula_boxes") or []
        if boxes and expected is not None:
            candidates = []
            for raw in boxes:
                try:
                    candidates.append(_as_rect(raw))
                except (TypeError, ValueError):
                    continue
            if candidates:
                nearest = min(candidates, key=lambda rect: _distance(expected, rect))
                if _distance(expected, nearest) <= 36:
                    return nearest, "formula_box"
    if expected is not None:
        half_width = min(30.0, max(3.0, source_rect.width / 2))
        half_height = min(20.0, max(3.0, source_rect.height / 2))
        page_rect = _page_text_rect(page)
        rect = pymupdf.Rect(
            max(page_rect.x0, expected.x - half_width),
            max(page_rect.y0, expected.y - half_height),
            min(page_rect.x1, expected.x + half_width),
            min(page_rect.y1, expected.y + half_height),
        )
        if rect.width > 0.5 and rect.height > 0.5:
            return rect, "geometry"
    return None, "anchor_not_found"


_GUIDE_EN_RE = re.compile(
    r"(?:Equation|Eq\.?|Figure|Fig\.?|Table|Section|Sect\.?|Appendix)\s*$",
    re.I,
)
_GUIDE_CJK_SUFFIX = ("式", "图", "表", "节", "录")


def _anchor_guide_score(text: str, start: int) -> int:
    """Prefer a number that follows its "Eq./Fig./表/公式" guide word.

    The source anchor line is not always mapped to the right output paragraph,
    so a bare number such as the "1" of "Eq. 1" competes with numbers elsewhere
    on the page.  The guide word directly before the number is a strong signal.
    """
    prefix = text[max(0, start - 16):start]
    if _GUIDE_EN_RE.search(prefix):
        return 2
    stripped = prefix.rstrip()
    if stripped and stripped[-1] in _GUIDE_CJK_SUFFIX:
        return 2
    return 0


def _guided_candidate_rect(
    page: Any, anchor: dict[str, Any], region: pymupdf.Rect,
    candidates: list[dict[str, Any]],
) -> pymupdf.Rect | None:
    """Return the uniquely guided number candidate; never guess between ties."""
    if anchor.get("type") != "number" or len(candidates) < 2:
        return None
    chars = _text_chars(page, region)
    if not chars:
        return None
    text = "".join(value for value, _ in chars)
    scored = [
        (_anchor_guide_score(text, candidate["start"]), candidate)
        for candidate in candidates
    ]
    best = max(score for score, _ in scored)
    if best <= 0:
        return None
    winners = [candidate for score, candidate in scored if score == best]
    if len(winners) != 1:
        return None
    return winners[0]["rect"]


def _find_anchor_rect(page: Any, anchor: dict[str, Any], region: pymupdf.Rect,
                      expected: pymupdf.Point | None, ordinal: int | None,
                      group_size: int | None) -> tuple[pymupdf.Rect | None, str]:
    candidates = _anchor_match_candidates(page, anchor, region)
    if not candidates:
        return None, "anchor_not_found_region"
    if ordinal is not None and group_size is not None and len(candidates) == group_size:
        return candidates[ordinal]["rect"], ""
    guided = _guided_candidate_rect(page, anchor, region, candidates)
    if guided is not None:
        return guided, ""
    if expected is not None:
        ranked = sorted(candidates, key=lambda item: _distance(expected, item["rect"]))
        if len(ranked) == 1:
            return ranked[0]["rect"], ""
        first = _distance(expected, ranked[0]["rect"])
        second = _distance(expected, ranked[1]["rect"])
        if abs(second - first) < 0.5:
            return None, "anchor_ambiguous"
        return ranked[0]["rect"], ""
    if len(candidates) == 1:
        return candidates[0]["rect"], ""
    return None, "anchor_ambiguous"


def _graphics_region_preserved(page: Any, rect: pymupdf.Rect) -> bool:
    """True when the output page still paints a drawing or image in rect.

    Used only for text-free clickable areas (ORCID icons, a table rendered as
    vector graphics).  Copying the source rectangle blindly is not acceptable;
    the rectangle must correspond to preserved non-text artwork.
    """
    try:
        for drawing in page.get_drawings():
            drawing_rect = drawing.get("rect")
            if drawing_rect is not None and pymupdf.Rect(drawing_rect).intersects(rect):
                return True
    except Exception:
        pass
    try:
        for image in page.get_image_info():
            if pymupdf.Rect(image["bbox"]).intersects(rect):
                return True
    except Exception:
        pass
    return False


def _graphics_rect(source_rect: pymupdf.Rect, output_page: Any,
                   source_page: Any) -> tuple[pymupdf.Rect | None, str]:
    if abs(source_page.rect.width - output_page.rect.width) > 1 or abs(
        source_page.rect.height - output_page.rect.height
    ) > 1:
        return None, "page_geometry_changed"
    rect = pymupdf.Rect(source_rect)
    if not _page_text_rect(output_page).contains(rect):
        return None, "graphics_outside_page"
    if not _graphics_region_preserved(output_page, rect):
        return None, "graphics_not_preserved"
    return rect, ""


def _page_map(source_doc: Any, output_doc: Any) -> dict[int, int]:
    return {index: index for index in range(min(len(source_doc), len(output_doc)))}


def _target_anchor_text(source_doc: Any, destination: dict[str, Any]) -> dict[str, Any] | None:
    """Describe the text at a resolved destination point on the source page."""
    point = destination.get("point")
    if not point:
        return None
    try:
        page = source_doc[int(destination["page"])]
    except (KeyError, TypeError, IndexError):
        return None
    page_rect = _page_text_rect(page)
    band = pymupdf.Rect(
        max(0.0, float(point[0]) - 6),
        max(0.0, float(point[1]) - 6),
        min(page_rect.x1, float(point[0]) + 220),
        min(page_rect.y1, float(point[1]) + 24),
    )
    try:
        under = page.get_textbox(band)
    except Exception:
        return None
    line = _line_context(page, band)
    anchor = _anchor_from_text(under, line)
    if anchor.get("type") == "graphics":
        return None
    return anchor


def _target_anchor_point(output_page: Any, target_paragraph: dict[str, Any] | None,
                         anchor: dict[str, Any] | None,
                         destination: dict[str, Any]) -> list[float] | None:
    """Find the destination text on the output page and return its top-left."""
    if anchor is None:
        return None
    region = _page_text_rect(output_page)
    if target_paragraph is not None:
        paragraph_region = _region_for_link(target_paragraph, output_page)
        if paragraph_region.width > 1 and paragraph_region.height > 1:
            region = paragraph_region
    candidates = _anchor_match_candidates(output_page, anchor, region)
    if not candidates and target_paragraph is None and anchor.get("type") in (
            "bracket", "paren"):
        # A page-wide search is only safe for citation/equation tokens when
        # the destination paragraph itself was not captured.  Bare figure
        # numbers must never jump to an unrelated occurrence on the page.
        candidates = _anchor_match_candidates(
            output_page, anchor, _page_text_rect(output_page))
    if not candidates:
        return None
    expected = None
    if target_paragraph is not None:
        source_box = _paragraph_box(target_paragraph)
        output_box = _paragraph_text_bbox(target_paragraph)
        point = destination.get("point")
        if source_box is not None and output_box is not None and point:
            anchor_rect = pymupdf.Rect(
                float(point[0]), float(point[1]),
                float(point[0]) + 1, float(point[1]) + 1,
            )
            expected = _expected_output_point(anchor_rect, source_box, output_box)
    if expected is not None:
        nearest = min(candidates, key=lambda item: _distance(expected, item["rect"]))
        return [nearest["rect"].x0, nearest["rect"].y0]
    return [candidates[0]["rect"].x0, candidates[0]["rect"].y0]


def _target_text_candidates(output_page: Any, text: str,
                            region: pymupdf.Rect, limit: int = 20) -> list[dict[str, Any]]:
    token = str(text or "").strip()
    if not token:
        return []
    chars = _text_chars(output_page, region)
    if not chars:
        return []
    return _reading_order(_match_rects(chars, _token_pattern(token), limit=limit))


def _target_point_for_link(destination: dict[str, Any],
                           paragraphs: list[dict[str, Any]], output_doc: Any,
                           page_mapping: dict[int, int], source_doc: Any,
                           anchor_text: str = ""
                           ) -> tuple[int | None, list[float] | None, str]:
    if destination.get("type") != "goto":
        return None, None, ""
    source_target_page = int(destination["page"])
    output_target_page = page_mapping.get(source_target_page)
    if output_target_page is None:
        return None, None, "target_page_missing"
    output_target_page_obj = output_doc[output_target_page]
    point = destination.get("point")

    # 目标页上的近似位置：源/输出页面的相对坐标。用于在重排后的页面里
    # 选最近的编号候选，也用于没有可用锚文本时的兜底。
    approx = None
    if point:
        source_page = source_doc[source_target_page]
        output_page = output_doc[output_target_page]
        sx = float(point[0]) / max(1e-6, source_page.cropbox.width)
        sy = float(point[1]) / max(1e-6, source_page.cropbox.height)
        approx = pymupdf.Point(
            max(0.0, min(output_page.cropbox.width, sx * output_page.cropbox.width)),
            max(0.0, min(output_page.cropbox.height, sy * output_page.cropbox.height)),
        )

    token = str(anchor_text or "").strip()
    numbered = bool(token) and (
        bool(_COMPOUND_NUMBER_RE.fullmatch(token)) or _is_citation_number(token)
    )
    if numbered:
        candidates = _target_text_candidates(
            output_target_page_obj, token, _page_text_rect(output_target_page_obj))
        if candidates:
            if approx is not None:
                nearest = min(candidates, key=lambda item: _distance(approx, item["rect"]))
                if _distance(approx, nearest["rect"]) <= max(
                        72.0, output_target_page_obj.rect.height * 0.35):
                    return output_target_page, [nearest["rect"].x0, nearest["rect"].y0], "target_anchor_search"
            elif len(candidates) == 1:
                return output_target_page, [
                    candidates[0]["rect"].x0, candidates[0]["rect"].y0
                ], "target_anchor_search"

    target_paragraph = _find_target_paragraph(point, source_target_page, paragraphs)
    target_anchor = _target_anchor_text(source_doc, destination)
    anchor_point = _target_anchor_point(
        output_target_page_obj, target_paragraph, target_anchor, destination)
    if anchor_point is not None:
        return output_target_page, anchor_point, ""
    if target_paragraph is not None:
        out_box = target_paragraph.get("output_text_bbox") or target_paragraph.get("output_box")
        if out_box:
            try:
                out_rect = _as_rect(out_box)
                return output_target_page, [out_rect.x0, out_rect.y0], ""
            except (TypeError, ValueError):
                pass
    if approx is not None:
        return output_target_page, [approx.x, approx.y], "target_point_approximate"
    return output_target_page, [0.0, 0.0], "target_point_missing"


def _link_xrefs(page: Any) -> set[int]:
    try:
        return {
            int(xref) for xref, kind, _name in page.annot_xrefs()
            if kind == pymupdf.PDF_ANNOT_LINK
        }
    except Exception:
        return set()


def _new_link_xref(page: Any, before: set[int]) -> int:
    added = sorted(_link_xrefs(page) - before)
    if added:
        return added[-1]
    try:
        xrefs = [int(xref) for xref, kind, _name in page.annot_xrefs()
                 if kind == pymupdf.PDF_ANNOT_LINK]
    except Exception:
        return 0
    return xrefs[-1] if xrefs else 0


def _insert_link(doc: Any, page: Any, rect: pymupdf.Rect, destination: dict[str, Any],
                 target_page: int | None, target_point: list[float] | None) -> int:
    link: dict[str, Any] = {
        "kind": KIND_GOTO if destination.get("type") == "goto" else KIND_URI,
        "from": rect,
    }
    if destination.get("type") == "uri":
        link["uri"] = destination["uri"]
    else:
        link["page"] = int(target_page)
        link["to"] = pymupdf.Point(target_point)
        link["zoom"] = 0
    before = _link_xrefs(page)
    page.insert_link(link)
    xref = _new_link_xref(page, before)
    if xref <= 0:
        return xref
    doc.xref_set_key(xref, "Rect", _pdf_rect_string(_unrotated_rect_to_pdf(rect, page)))
    if destination.get("type") == "goto":
        target_xref = doc.page_xref(int(target_page))
        target_page_obj = doc[int(target_page)]
        pdf_point = _unrotated_point_to_pdf(target_point, target_page_obj)
        left, top = _number_text(pdf_point.x), _number_text(pdf_point.y)
        doc.xref_set_key(
            xref, "A", "<</S /GoTo /D [%d 0 R /XYZ %s %s 0]>>" %
            (target_xref, left, top))
    return xref


def _source_links(source_doc: Any, paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page_index, page in enumerate(source_doc):
        for link in page.get_links():
            rect = _visible_rect_to_unrotated(link.get("from"), page)
            try:
                under = page.get_textbox(rect)
            except Exception:
                under = ""
            line = _line_context(page, rect)
            destination = _resolve_destination(source_doc, link)
            if destination.get("type") == "uri":
                anchor = _anchor_from_uri(under, destination.get("uri", ""))
            else:
                anchor = _anchor_from_text(under, line)
            paragraph = _find_source_paragraph(rect, page_index, paragraphs)
            records.append({
                "kind": _KIND_NAMES.get(int(link.get("kind", KIND_NONE)), "unknown"),
                "kind_number": int(link.get("kind", KIND_NONE)),
                "source_page": page_index,
                "source_from": _rect_list(link.get("from")),
                "source_from_unrotated": _rect_list(rect),
                "source_text": _preview(under, 120),
                "source_line": _preview(line, 200),
                "anchor": anchor,
                "destination": destination,
                "source_paragraph": paragraph,
                "link": link,
            })
    return records


def _citation_numbers(line: str) -> tuple[str, list[int]]:
    for match in _CITATION_GROUP_RE.finditer(line or ""):
        numbers = [int(value) for value in re.findall(r"\d{1,4}", match.group(0))]
        if len(numbers) >= 2:
            return match.group(0), numbers
    return "", []


def _assign_group_ordinals(records: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    """Assign source-order ordinals for duplicate anchors and citation clusters."""
    groups: dict[tuple[int, str], list[int]] = {}
    for index, record in enumerate(records):
        paragraph = record.get("source_paragraph")
        anchor = record.get("anchor") or {}
        text = str(anchor.get("text") or "")
        if paragraph is None or not text:
            continue
        key = (int(paragraph["id"]), text)
        # [4, 7, 18] 里的每个数字链接属于同一个簇：按括号列表分组、
        # 按源阅读顺序配对译文中的数字，避免 4 被 7/18 挤掉或错位。
        if anchor.get("type") == "number" and text.isdigit():
            group_text, numbers = _citation_numbers(record.get("source_line", ""))
            if numbers and int(text) in numbers:
                key = (int(paragraph["id"]), "cluster:" + group_text)
                record["_cluster"] = {
                    "text": group_text,
                    "numbers": numbers,
                    "index": numbers.index(int(text)),
                }
        groups.setdefault(key, []).append(index)
    result: dict[int, tuple[int, int]] = {}
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        ordered = sorted(indexes, key=lambda index: (
            records[index].get("source_from_unrotated", [0, 0, 0, 0])[1],
            records[index].get("source_from_unrotated", [0, 0, 0, 0])[0]))
        for ordinal, index in enumerate(ordered):
            result[index] = (ordinal, len(ordered))
    return result


def _restore_one(record: dict[str, Any], output_doc: Any, source_doc: Any,
                 paragraphs: list[dict[str, Any]], page_mapping: dict[int, int],
                 ordinals: dict[int, tuple[int, int]], output_record_index: int
                 ) -> dict[str, Any]:
    destination = record["destination"]
    source_page_index = int(record["source_page"])
    output_page_index = page_mapping.get(source_page_index)
    record = dict(record)
    record["output"] = {}
    if destination.get("type") == "unsupported":
        record["status"] = "unsupported"
        record["reason"] = destination.get("reason", "unsupported")
        return record
    if destination.get("type") == "invalid":
        record["status"] = "unresolved"
        record["reason"] = destination.get("reason", "invalid_destination")
        return record
    if output_page_index is None:
        record["status"] = "unresolved"
        record["reason"] = "output_page_missing"
        return record

    output_page = output_doc[output_page_index]
    source_page = source_doc[source_page_index]
    paragraph = record.get("source_paragraph")
    output_paragraph = None
    if paragraph is not None:
        for candidate in paragraphs:
            if candidate.get("id") == paragraph.get("id"):
                output_paragraph = candidate
                break
    if (output_paragraph is not None and
            output_paragraph.get("output_page") != output_page_index):
        output_paragraph = None

    region = _region_for_link(output_paragraph, output_page)
    anchor = record.get("anchor") or {}
    ordinal_pair = ordinals.get(output_record_index)
    ordinal = ordinal_pair[0] if ordinal_pair else None
    group_size = ordinal_pair[1] if ordinal_pair else None
    expected = _expected_output_point(
        _as_rect(record["source_from_unrotated"]),
        _paragraph_box(paragraph) if paragraph else None,
        _paragraph_text_bbox(output_paragraph) if output_paragraph else None)

    cluster_rect = None
    cluster = record.get("_cluster")
    if cluster and anchor.get("type") == "number":
        cluster_rect = _find_cluster_rect(output_page, region, cluster)

    if anchor.get("type") == "graphics" and (
            destination.get("type") == "uri"
            or (destination.get("type") == "goto"
                and output_paragraph is None and expected is None)):
        rect, reason = _graphics_rect(
            _as_rect(record["source_from_unrotated"]), output_page, source_page)
        if rect is None:
            record["status"] = "unresolved"
            record["reason"] = reason
            return record
        record["anchor"]["mapped"] = "graphics_geometry"
    elif cluster_rect is not None:
        rect, reason = cluster_rect, ""
        record["mapped_by"] = "citation_cluster"
        record["anchor"]["mapped"] = "citation_cluster"
    else:
        rect, reason = _find_anchor_rect(
            output_page, anchor, region, expected, ordinal, group_size)
        if rect is None and region != _page_text_rect(output_page):
            if anchor.get("type") in ("bracket", "paren", "roman", "number") or (
                    anchor.get("type") == "text" and
                    len(str(anchor.get("text", ""))) >= 6):
                rect, reason = _find_anchor_rect(
                    output_page, anchor, _page_text_rect(output_page),
                    expected, ordinal, group_size)
                if rect is not None:
                    reason = ""
        if rect is None and anchor.get("type") in (
                "number", "roman", "graphics"):
            rect, fallback = _fallback_anchor_rect(
                output_paragraph,
                _as_rect(record["source_from_unrotated"]),
                expected,
                anchor,
                output_page,
            )
            if rect is not None:
                reason = ""
                record["mapped_by"] = fallback
                record["anchor"]["mapped"] = fallback
        if rect is None:
            record["status"] = "unresolved"
            record["reason"] = reason
            return record

    if destination.get("type") == "uri":
        xref = _insert_link(output_doc, output_page, rect, destination, None, None)
        record["output"] = {"page": output_page_index, "from": _rect_list(rect),
                            "uri": destination["uri"], "xref": xref}
    else:
        target_page, target_point, target_reason = _target_point_for_link(
            destination, paragraphs, output_doc, page_mapping, source_doc,
            anchor_text=str((record.get("anchor") or {}).get("text") or ""))
        if target_page is None or target_point is None:
            record["status"] = "unresolved"
            record["reason"] = target_reason or "target_missing"
            return record
        xref = _insert_link(output_doc, output_page, rect, destination,
                            target_page, target_point)
        record["output"] = {
            "page": output_page_index, "from": _rect_list(rect),
            "target_page": target_page, "target_to": _point_list(target_point),
            "xref": xref,
        }
        if target_reason:
            record["target_note"] = target_reason
    record["status"] = "restored"
    record["reason"] = ""
    return record


def _mapping_payload(source_path: str, output_path: str, source_doc: Any,
                     output_doc: Any, records: list[dict[str, Any]],
                     stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": MAPPING_VERSION,
        "source": os.path.abspath(source_path),
        "output": os.path.abspath(output_path),
        "source_pages": len(source_doc),
        "output_pages": len(output_doc),
        "paragraphs": [
            {key: value for key, value in paragraph.items()
             if key in ("id", "source_page", "source_box", "source_text",
                        "output_page", "output_box", "output_text_bbox",
                        "output_formula_boxes")}
            for paragraph in paragraphs
        ] if (paragraphs := stats.get("_paragraphs") or []) else [],
        "stats": {key: value for key, value in stats.items()
                  if key not in ("records", "_paragraphs")},
        "links": [
            {key: value for key, value in record.items()
             if key not in ("link", "source_paragraph")}
            for record in records
        ],
    }


def restore_links(source_path: str, output_path: str,
                  paragraph_map: dict[str, Any] | list[dict[str, Any]] | None = None,
                  mapping_path: str | None = None,
                  log: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Restore all source links in output_path in place and write a mapping."""
    if isinstance(paragraph_map, dict):
        paragraphs = list(paragraph_map.get("paragraphs") or [])
    else:
        paragraphs = list(paragraph_map or [])

    source_doc = pymupdf.open(source_path)
    output_doc = pymupdf.open(output_path)
    try:
        if source_doc.is_encrypted:
            source_doc.authenticate("")
        if output_doc.is_encrypted:
            output_doc.authenticate("")
        records = _source_links(source_doc, paragraphs)
        ordinals = _assign_group_ordinals(records)
        page_mapping = _page_map(source_doc, output_doc)
        restored_records = []
        for index, record in enumerate(records):
            try:
                restored = _restore_one(record, output_doc, source_doc, paragraphs,
                                        page_mapping, ordinals, index)
            except Exception as error:
                restored = dict(record)
                restored["output"] = {}
                restored["status"] = "unresolved"
                restored["reason"] = "restore_error"
                restored["error"] = type(error).__name__
            restored_records.append(restored)
            if log:
                try:
                    log({"event": "link_record",
                         "page": restored.get("source_page", -1) + 1,
                         "text": restored.get("anchor", {}).get("text", ""),
                         "kind": restored.get("kind", ""),
                         "status": restored.get("status", ""),
                         "reason": restored.get("reason", "")})
                except Exception:
                    pass

        if restored_records:
            output_doc.saveIncr()
        stats = {
            "paragraphs": len(paragraphs),
            "source": len(restored_records),
            "restored": sum(1 for item in restored_records if item["status"] == "restored"),
            "unresolved": sum(1 for item in restored_records if item["status"] == "unresolved"),
            "unsupported": sum(1 for item in restored_records if item["status"] == "unsupported"),
            "by_kind": {name: sum(1 for item in restored_records if item.get("kind") == name)
                        for name in sorted({item.get("kind", "") for item in restored_records})},
            "unresolved_records": [
                {"page": item.get("source_page", -1) + 1,
                 "text": item.get("anchor", {}).get("text", ""),
                 "kind": item.get("kind", ""), "reason": item.get("reason", "")}
                for item in restored_records if item["status"] == "unresolved"],
            "unsupported_records": [
                {"page": item.get("source_page", -1) + 1,
                 "text": item.get("anchor", {}).get("text", ""),
                 "kind": item.get("kind", ""), "reason": item.get("reason", "")}
                for item in restored_records if item["status"] == "unsupported"],
        }
        if mapping_path is None:
            mapping_path = output_path + ".links.json"
        stats["_paragraphs"] = paragraphs
        payload = _mapping_payload(source_path, output_path, source_doc, output_doc,
                                   restored_records, stats)
        try:
            directory = os.path.dirname(os.path.abspath(mapping_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(mapping_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            stats["mapping_path"] = mapping_path
        except OSError:
            stats["mapping_path"] = ""
            stats["mapping_error"] = "write_failed"
        stats["records"] = restored_records
        return stats
    finally:
        output_doc.close()
        source_doc.close()


def restore_dual_links(source_path: str, dual_path: str, mono_mapping_path: str,
                       mapping_path: str | None = None,
                       log: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Restore GOTO/URI links on both halves of a side-by-side dual PDF.

    The left half is the untouched source page, so source link rectangles apply.
    The right half is the translated page, so the monolingual mapping's output
    rectangles are shifted by one page width.  Each side keeps its own target
    side, matching the source link's intent.
    """
    with open(mono_mapping_path, encoding="utf-8") as handle:
        mono_payload = json.load(handle)
    mono_links = mono_payload.get("links") or []
    source_doc = pymupdf.open(source_path)
    dual_doc = pymupdf.open(dual_path)
    records: list[dict[str, Any]] = []
    try:
        if len(dual_doc) != len(source_doc):
            raise ValueError("双语文档页数与原文不一致")
        halves: list[float] = []
        for index in range(len(dual_doc)):
            source_width = float(source_doc[index].rect.width)
            dual_width = float(dual_doc[index].rect.width)
            # BabelDOC 的左半是原页，右半从原文页宽处开始；总宽可能
            # 因混合页面尺寸大于两倍，不能用总宽的一半做偏移。
            if dual_width < 2 * source_width - 1.5:
                raise ValueError("双语文档不是左右并排布局")
            halves.append(source_width)

        def insert(page_index: int, rect: Any, destination: dict[str, Any],
                   target_page: int | None, target_point: list[float] | None,
                   side: str, text: str) -> None:
            try:
                _insert_link(dual_doc, dual_doc[page_index], rect, destination,
                             target_page, target_point)
                records.append({"side": side, "page": page_index,
                                "from": _rect_list(rect), "text": text,
                                "status": "restored"})
            except Exception as error:
                records.append({"side": side, "page": page_index,
                                "from": _rect_list(rect), "text": text,
                                "status": "unresolved",
                                "error": type(error).__name__})

        for record in mono_links:
            if record.get("status") != "restored":
                records.append({"side": "both",
                                "status": record.get("status", "unresolved"),
                                "text": record.get("source_text", ""),
                                "reason": record.get("reason", "")})
                continue
            source_page = int(record.get("source_page", 0))
            if source_page >= len(dual_doc):
                continue
            if source_doc[source_page].rotation:
                # 旋转页左半坐标需要额外变换；记录为不支持，不猜造。
                records.append({"side": "both", "status": "unsupported",
                                "text": record.get("source_text", ""),
                                "reason": "rotated_source_page"})
                continue
            destination = record.get("destination") or {}
            text = str(record.get("source_text", ""))
            left_rect = pymupdf.Rect(record.get("source_from") or [0, 0, 0, 0])
            output = record.get("output") or {}
            if destination.get("type") == "uri":
                uri = destination.get("uri", "")
                if not uri:
                    continue
                insert(source_page, left_rect, {"type": "uri", "uri": uri},
                       None, None, "left", text)
                if output:
                    right_page = int(output.get("page", source_page))
                    offset = halves[right_page]
                    rect = pymupdf.Rect(output.get("from") or [0, 0, 0, 0])
                    rect = pymupdf.Rect(rect.x0 + offset, rect.y0,
                                        rect.x1 + offset, rect.y1)
                    insert(right_page, rect, {"type": "uri", "uri": uri},
                           None, None, "right", text)
                continue
            if not output:
                continue
            target_page = destination.get("page")
            target_point = destination.get("point")
            right_target_page = output.get("target_page")
            right_target_point = output.get("target_to")
            if target_page is None or target_point is None:
                continue
            insert(source_page, left_rect, {"type": "goto"},
                   int(target_page), list(target_point), "left", text)
            if right_target_page is None or right_target_point is None:
                continue
            right_page = int(output.get("page", source_page))
            offset = halves[right_page]
            rect = pymupdf.Rect(output.get("from") or [0, 0, 0, 0])
            rect = pymupdf.Rect(rect.x0 + offset, rect.y0,
                                rect.x1 + offset, rect.y1)
            insert(right_page, rect, {"type": "goto"},
                   int(right_target_page),
                   [float(right_target_point[0]) + offset,
                    float(right_target_point[1])],
                   "right", text)

        temp_path = dual_path + ".links.tmp"
        dual_doc.save(temp_path, garbage=0, deflate=True)
        dual_doc.close()
        os.replace(temp_path, dual_path)
        stats = {
            "source": len(mono_links),
            "restored": sum(1 for item in records if item.get("status") == "restored"),
            "unresolved": sum(1 for item in records if item.get("status") == "unresolved"),
            "unsupported": sum(1 for item in records if item.get("status") == "unsupported"),
            "left": sum(1 for item in records if item.get("side") == "left"),
            "right": sum(1 for item in records if item.get("side") == "right"),
        }
        if mapping_path is None:
            mapping_path = dual_path + ".links.json"
        payload = {"version": MAPPING_VERSION,
                   "source": os.path.abspath(source_path),
                   "output": os.path.abspath(dual_path),
                   "stats": stats, "links": records}
        try:
            directory = os.path.dirname(os.path.abspath(mapping_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(mapping_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            stats["mapping_path"] = mapping_path
        except OSError:
            stats["mapping_path"] = ""
        return stats
    finally:
        try:
            dual_doc.close()
        except Exception:
            pass
        source_doc.close()
