"""Translate and vertically redraw rotated table column headers.

BabelDOC 0.6.4 does not translate the 90-degree column headers of some tables:
they survive in the output PDF as rotated English text.  This module finds such
header groups in the source PDF, matches the still-English groups in the output
PDF (including bilingual side-by-side output), and redraws one translated
character per line with upright glyphs while keeping the original reading
direction.  Translation is supplied by the caller; this module never performs
network I/O.
"""

from __future__ import annotations

import json
import math
import os
import re

import pymupdf


ROTATED_ANGLE_TOLERANCE = 6.0
HEADER_GROUP_MIN = 4

# A group of rotated headers must span at least this much horizontally.  Four
# short labels next to each other are wider than this; a stray rotated axis
# label is not.
_HEADER_GROUP_MIN_WIDTH = 80.0
# Rotated headers belonging to one header row overlap vertically.  A small gap
# is tolerated so slightly different baselines still cluster together.
_Y_GAP = 8.0
# Two x-runs of headers farther apart than this belong to different tables (or
# to the two halves of a bilingual page).
_X_RUN_GAP = 24.0
_MIN_HEADER_SIZE = 2.0
_MAX_HEADER_SIZE = 12.0
_MIN_TEXT_LENGTH = 2
_MAX_TEXT_LENGTH = 40
_MIN_FONT_SIZE = 3.5
# MuPDF reports a CJK character inserted with the built-in china-s font as a
# full em square: bbox top = baseline - size, bbox bottom = baseline + 0.2 size.
_GLYPH_TOP_FACTOR = 1.0
_GLYPH_BOTTOM_FACTOR = 0.2
_REDACT_PADDING = 0.3

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LETTER_RE = re.compile(r"[A-Za-z]")


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _within_angle(angle: float, target: float) -> bool:
    difference = abs(((angle - target + 180.0) % 360.0) - 180.0)
    return difference <= ROTATED_ANGLE_TOLERANCE


def _line_angle(line: dict) -> float:
    direction = line.get("dir") or (1.0, 0.0)
    try:
        dx = float(direction[0])
        dy = float(direction[1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    return math.degrees(math.atan2(-dy, dx)) % 360.0


def _is_rotated_header(text: str, angle: float, size: float) -> bool:
    if not text or len(text) < _MIN_TEXT_LENGTH or len(text) > _MAX_TEXT_LENGTH:
        return False
    if not _LETTER_RE.search(text) or _CJK_RE.search(text):
        return False
    if not math.isfinite(size) or size < _MIN_HEADER_SIZE or size > _MAX_HEADER_SIZE:
        return False
    return _within_angle(angle, 90.0) or _within_angle(angle, 270.0)


def _page_headers(page: pymupdf.Page) -> list[dict]:
    headers = []
    try:
        data = page.get_text("dict")
    except Exception:
        return headers
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            text = _clean_text(
                "".join(str(span.get("text") or "") for span in spans)
            )
            if not spans:
                continue
            try:
                size = float(spans[0].get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            angle = _line_angle(line)
            if not _is_rotated_header(text, angle, size):
                continue
            try:
                rect = pymupdf.Rect(line["bbox"])
            except (KeyError, TypeError, ValueError):
                continue
            if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                continue
            if not all(
                math.isfinite(value)
                for value in (rect.x0, rect.y0, rect.x1, rect.y1)
            ):
                continue
            headers.append(
                {
                    "text": text,
                    "rect": rect,
                    "size": size,
                    "angle": angle,
                }
            )
    return headers


def _cluster_by_y(headers: list[dict]) -> list[dict]:
    clusters: list[dict] = []
    for header in sorted(
        headers, key=lambda item: (item["rect"].y0, item["rect"].x0)
    ):
        rect = header["rect"]
        target = None
        for cluster in clusters:
            if (
                rect.y0 <= cluster["y1"] + _Y_GAP
                and rect.y1 >= cluster["y0"] - _Y_GAP
            ):
                target = cluster
                break
        if target is None:
            clusters.append(
                {"y0": rect.y0, "y1": rect.y1, "headers": [header]}
            )
        else:
            target["headers"].append(header)
            target["y0"] = min(target["y0"], rect.y0)
            target["y1"] = max(target["y1"], rect.y1)
    return clusters


def _group_width(headers: list[dict]) -> float:
    if not headers:
        return 0.0
    return max(header["rect"].x1 for header in headers) - min(
        header["rect"].x0 for header in headers
    )


def find_rotated_header_groups(doc) -> list[dict]:
    """Return groups of short English labels rotated by 90 or 270 degrees."""
    groups: list[dict] = []
    for page_index, page in enumerate(doc):
        headers = _page_headers(page)
        if len(headers) < HEADER_GROUP_MIN:
            continue
        for cluster in _cluster_by_y(headers):
            members = sorted(
                cluster["headers"],
                key=lambda item: (item["rect"].x0, item["rect"].y0),
            )
            if len(members) < HEADER_GROUP_MIN:
                continue
            if _group_width(members) < _HEADER_GROUP_MIN_WIDTH:
                continue
            groups.append({"page_index": page_index, "headers": members})
    return groups


def _x_runs(headers: list[dict], page_width: float) -> list[list[dict]]:
    threshold = max(_X_RUN_GAP, page_width * 0.04)
    runs: list[list[dict]] = []
    for header in sorted(
        headers, key=lambda item: (item["rect"].x0, item["rect"].y0)
    ):
        if not runs:
            runs.append([header])
            continue
        previous = runs[-1][-1]["rect"]
        if header["rect"].x0 - previous.x1 > threshold:
            runs.append([header])
        else:
            runs[-1].append(header)
    return runs


def _shift_rect(rect: pymupdf.Rect, dx: float) -> pymupdf.Rect:
    return pymupdf.Rect(
        rect.x0 + dx, rect.y0, rect.x1 + dx, rect.y1
    )


def _match_source_group(
    run: list[dict], source_groups: list[dict], offset_x: float
) -> dict | None:
    run_texts = {_clean_text(header["text"]).casefold() for header in run}
    if not run_texts:
        return None
    run_x0 = min(header["rect"].x0 for header in run) - offset_x
    run_y0 = min(header["rect"].y0 for header in run)
    run_y1 = max(header["rect"].y1 for header in run)
    best = None
    best_key = None
    for group in source_groups:
        source_headers = group["headers"]
        source_texts = {
            _clean_text(header["text"]).casefold()
            for header in source_headers
        }
        overlap = len(run_texts & source_texts)
        required = max(
            2, int(round(min(len(run_texts), len(source_texts)) * 0.5))
        )
        if overlap < required:
            continue
        source_y0 = min(header["rect"].y0 for header in source_headers)
        source_y1 = max(header["rect"].y1 for header in source_headers)
        if min(run_y1, source_y1) - max(run_y0, source_y0) <= -_Y_GAP:
            continue
        source_x0 = min(header["rect"].x0 for header in source_headers)
        distance = abs(run_x0 - source_x0)
        key = (overlap, -distance)
        if best_key is None or key > best_key:
            best = group
            best_key = key
    return best


def _cjk_region(
    run: list[dict], source_group: dict, offset_x: float
) -> pymupdf.Rect:
    rects = [header["rect"] for header in run]
    rects.extend(
        _shift_rect(header["rect"], offset_x)
        for header in source_group["headers"]
    )
    union = pymupdf.Rect(rects[0])
    for rect in rects[1:]:
        union |= rect
    return pymupdf.Rect(
        union.x0 - 1.0, union.y0 - 1.0, union.x1 + 1.0, union.y1 + 1.0
    )


def _contains_cjk(page: pymupdf.Page, rect: pymupdf.Rect) -> bool:
    try:
        data = page.get_text("dict", clip=rect)
    except Exception:
        return False
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if _CJK_RE.search(str(span.get("text") or "")):
                    return True
    return False


def untranslated_headers(source_path: str, output_path: str) -> list[dict]:
    """Return output-side rotated header groups that remain in English."""
    with pymupdf.open(source_path) as source:
        with pymupdf.open(output_path) as output:
            if not len(source) or not len(output):
                return []
            source_width = float(source[0].rect.width)
            output_width = float(output[0].rect.width)
            dual = (
                source_width > 0
                and output_width >= source_width * 1.5
            )
            offset_x = source_width if dual else 0.0

            source_by_page: dict[int, list[dict]] = {}
            for group in find_rotated_header_groups(source):
                source_by_page.setdefault(group["page_index"], []).append(
                    group
                )

            result: list[dict] = []
            for group in find_rotated_header_groups(output):
                page_index = group["page_index"]
                if page_index >= len(output):
                    continue
                page = output[page_index]
                headers = group["headers"]
                if dual:
                    # The left half of a bilingual page is the untouched source
                    # page; only the translation half may still need work.
                    headers = [
                        header
                        for header in headers
                        if header["rect"].x0 >= offset_x - 0.5
                    ]
                if len(headers) < HEADER_GROUP_MIN:
                    continue
                source_groups = source_by_page.get(page_index, [])
                if not source_groups:
                    continue
                for run in _x_runs(headers, page.rect.width):
                    if len(run) < HEADER_GROUP_MIN:
                        continue
                    if _group_width(run) < _HEADER_GROUP_MIN_WIDTH:
                        continue
                    source_group = _match_source_group(
                        run, source_groups, offset_x
                    )
                    if source_group is None:
                        continue
                    region = _cjk_region(run, source_group, offset_x)
                    if _contains_cjk(page, region):
                        continue
                    result.append(
                        {"page_index": page_index, "headers": run}
                    )
            return result


def build_prompt_text(headers: list[str]) -> str:
    """Build the one-shot user prompt for translating table column headers."""
    items = [str(header) for header in headers]
    return (
        "下面是一篇论文表格的竖排列标题（英文）。请把它们翻译成简体中文，"
        "用于在 PDF 中逐字竖排重绘。\n"
        "要求：\n"
        "1. 只输出一个 JSON 字符串数组，数组长度必须为 %d，"
        "顺序与输入完全一致；\n"
        "2. 数组的每个元素都必须是非空字符串；\n"
        "3. 只保留技术缩写和专有名词的原文（例如 WordPress、Webshell、"
        "Dirb、Shell），其余内容译为简体中文；\n"
        "4. 标题尽量简短，不要解释，不要 Markdown 代码围栏，"
        "不要输出 JSON 以外的任何文字。\n"
        "输入标题：\n%s"
        % (len(items), json.dumps(items, ensure_ascii=False))
    )


def _clean_translation_list(value: object, count: int) -> list[str] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    result = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if not text:
            return None
        result.append(text)
    return result


def parse_response(text: str, count: int) -> list[str] | None:
    """Parse a JSON string array from a model reply."""
    if not isinstance(text, str) or count < 0:
        return None
    candidate = text.strip().lstrip("\ufeff")
    if not candidate:
        return None
    decoder = json.JSONDecoder()
    for index, character in enumerate(candidate):
        if character != "[":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, list):
            cleaned = _clean_translation_list(value, count)
            if cleaned is not None:
                return cleaned
    return None


def is_valid_translation(original: str, translated: str) -> bool:
    """Whether a translation is real Chinese and not an echo of the source."""
    if not isinstance(translated, str):
        return False
    value = translated.strip()
    if not value or not _CJK_RE.search(value):
        return False
    if not "".join(value.split()):
        return False
    original_text = _clean_text(original).casefold()
    translated_text = _clean_text(value).casefold()
    return translated_text != original_text


def _normalize_angle(angle: float) -> float:
    return angle % 360.0


def _draw_vertical_header(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
    size: float,
    angle: float,
) -> None:
    glyphs = [character for character in text if not character.isspace()]
    if not glyphs:
        raise ValueError("empty translation")
    available = max(0.0, rect.height)
    needed = (len(glyphs) + _GLYPH_BOTTOM_FACTOR) * size
    if available > 0 and needed > available:
        size = max(
            _MIN_FONT_SIZE,
            available / (len(glyphs) + _GLYPH_BOTTOM_FACTOR),
        )
    if not math.isfinite(size) or size <= 0:
        raise ValueError("invalid font size")
    step = size
    center_x = (rect.x0 + rect.x1) / 2.0
    upward = _within_angle(_normalize_angle(angle), 90.0)
    for index, character in enumerate(glyphs):
        if upward:
            baseline = rect.y1 - index * step - _GLYPH_BOTTOM_FACTOR * size
        else:
            baseline = rect.y0 + index * step + _GLYPH_TOP_FACTOR * size
        page.insert_text(
            (center_x - size / 2.0, baseline),
            character,
            fontname="china-s",
            fontsize=size,
            color=(0, 0, 0),
        )


def _header_rect(header: dict) -> pymupdf.Rect:
    value = header.get("rect")
    if value is None:
        raise ValueError("missing rect")
    rect = pymupdf.Rect(value)
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        raise ValueError("invalid rect")
    if not all(
        math.isfinite(number)
        for number in (rect.x0, rect.y0, rect.x1, rect.y1)
    ):
        raise ValueError("invalid rect")
    return rect


def apply_translations(
    output_path: str, groups: list[dict], translations: list[str]
) -> dict:
    """Redact the English headers and draw the Chinese replacements."""
    stats = {
        "groups": len(groups),
        "redacted": 0,
        "drawn": 0,
        "failed": [],
    }
    values = list(translations or [])
    entries_by_page: dict[int, list[tuple[dict, str, float, float]]] = {}
    cursor = 0
    for group in groups:
        try:
            page_index = int(group.get("page_index", 0))
        except (TypeError, ValueError):
            page_index = -1
        for header in group.get("headers") or []:
            original = _clean_text(header.get("text"))
            translation = values[cursor] if cursor < len(values) else None
            cursor += 1
            if not is_valid_translation(original, translation):
                stats["failed"].append(
                    {"text": original, "reason": "invalid_translation"}
                )
                continue
            try:
                rect = _header_rect(header)
            except (TypeError, ValueError):
                stats["failed"].append(
                    {"text": original, "reason": "invalid_rect"}
                )
                continue
            try:
                size = float(header.get("size") or 5.0)
                angle = float(header.get("angle") or 90.0)
            except (TypeError, ValueError):
                stats["failed"].append(
                    {"text": original, "reason": "invalid_geometry"}
                )
                continue
            if (
                not math.isfinite(size)
                or size <= 0
                or not math.isfinite(angle)
            ):
                stats["failed"].append(
                    {"text": original, "reason": "invalid_geometry"}
                )
                continue
            entries_by_page.setdefault(page_index, []).append(
                (header, translation, size, angle)
            )

    if not entries_by_page:
        return stats

    document = pymupdf.open(output_path)
    try:
        modified = False
        for page_index, entries in entries_by_page.items():
            if page_index < 0 or page_index >= len(document):
                for header, _, _, _ in entries:
                    stats["failed"].append(
                        {
                            "text": _clean_text(header.get("text")),
                            "reason": "invalid_page",
                        }
                    )
                continue
            page = document[page_index]
            annotated = []
            for entry in entries:
                header, _, _, _ = entry
                try:
                    rect = _header_rect(header)
                    redact_rect = pymupdf.Rect(
                        rect.x0 - _REDACT_PADDING,
                        rect.y0 - _REDACT_PADDING,
                        rect.x1 + _REDACT_PADDING,
                        rect.y1 + _REDACT_PADDING,
                    )
                    annotation = page.add_redact_annot(
                        redact_rect, fill=False
                    )
                except Exception:
                    stats["failed"].append(
                        {
                            "text": _clean_text(header.get("text")),
                            "reason": "redact_failed",
                        }
                    )
                    continue
                annotated.append((entry, annotation))
            if not annotated:
                continue
            try:
                applied = page.apply_redactions(images=0, graphics=0)
            except Exception:
                applied = False
            if applied is False:
                for entry, annotation in annotated:
                    header, _, _, _ = entry
                    try:
                        page.delete_annot(annotation)
                    except Exception:
                        pass
                    stats["failed"].append(
                        {
                            "text": _clean_text(header.get("text")),
                            "reason": "redact_failed",
                        }
                    )
                continue
            modified = True
            stats["redacted"] += len(annotated)
            for entry, _ in annotated:
                header, translation, size, angle = entry
                rect = _header_rect(header)
                try:
                    _draw_vertical_header(
                        page, rect, translation, size, angle
                    )
                except Exception:
                    stats["failed"].append(
                        {
                            "text": _clean_text(header.get("text")),
                            "reason": "draw_failed",
                        }
                    )
                    continue
                stats["drawn"] += 1
        if modified:
            temporary = output_path + ".table2tmp"
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


def restore_rotated_headers(source_path: str, output_path: str, translate) -> dict:
    """Translate still-English rotated headers and redraw them vertically."""
    groups = untranslated_headers(source_path, output_path)
    if not groups:
        return {
            "skipped": "no_untranslated_headers",
            "groups": 0,
            "requested": 0,
            "translated": 0,
        }
    headers = [
        header["text"]
        for group in groups
        for header in group["headers"]
    ]
    try:
        response = translate(headers)
    except Exception:
        return {"skipped": "translate_error"}
    if response is None:
        return {"skipped": "translate_returned_none"}
    if isinstance(response, str):
        translations = parse_response(response, len(headers))
        if translations is None:
            return {"skipped": "parse_failed"}
    elif isinstance(response, (list, tuple)):
        translations = _clean_translation_list(list(response), len(headers))
        if translations is None:
            return {"skipped": "translation_count_mismatch"}
    else:
        return {"skipped": "invalid_translate_result"}

    for original, translated in zip(headers, translations):
        if not is_valid_translation(original, translated):
            return {"skipped": "invalid_translation"}

    stats = apply_translations(output_path, groups, translations)
    stats["requested"] = len(headers)
    stats["translated"] = stats.get("drawn", 0)
    return stats
