#!/usr/bin/env python3
"""PDF quality gate for the 0.6.0 output-fidelity work (ticket T01).

The tool opens a source PDF and a BabelDOC-output PDF, inspects the translated
side (the right half of a bilingual page, or the whole page for a monolingual
output), and prints one PASS/FAIL line per requirement with concrete evidence:

1. translated-side links: count, anchor coverage, and destination pages;
2. Figure 4 axis labels (page 10);
3. Figure 5 Chinese row labels, right-aligned (page 11);
4. duplicated commas ("[0, 1], ," or ", ,");
5. Table 2 rotated column headers translated (page 11);
6. Table 3 full sentences translated while proper nouns survive (page 13).

The PDFs are opened read-only.  No model API is called.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pymupdf

DEFAULT_SOURCE = (
    r"G:\Program Files\Zotero\datas\storage\JYCINM3Z\P05.pdf"
)
DEFAULT_TRANSLATED = (
    r"C:\Users\A\Desktop\论文\IDS · 安全告警降噪"
    r"\01 数据集与评测"
    r"\Landauer 等 - 2024 - Introducing a New Alert Data Set for "
    r"Multi-Step Attack Analysis_1.pdf"
)

# 0-based page indexes in the paper used by the fixture.
PAGE_FIG4 = 9       # Figure 4, PDF page 10
PAGE_FIG5 = 10      # Figure 5, PDF page 11
PAGE_TABLE2 = 10    # Table 2, PDF page 11
PAGE_TABLE3 = 12    # Table 3, PDF page 13

ALIGN_TOLERANCE = 2.0
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
DETECTOR_RE = re.compile(r"\b[AWS]-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b")
EFFECTIVE_RE = re.compile(r"[0-9A-Za-z.]")
ABBR_RE = re.compile(r"^[AWS]-[A-Za-z0-9-]+$")


@dataclass
class CharBox:
    char: str
    bbox: pymupdf.Rect


@dataclass
class TextRun:
    page_index: int
    bbox: pymupdf.Rect
    text: str
    chars: list[CharBox]


@dataclass
class SourceRow:
    page: int
    bbox: pymupdf.Rect
    text: str
    size: float


@dataclass(frozen=True)
class AnchorSpec:
    key: str
    pattern: str
    target: str
    cluster: bool = False


ANCHORS: tuple[AnchorSpec, ...] = (
    AnchorSpec("3.1", r"第\s*3\.1\s*节", "3.1"),
    AnchorSpec("3.2", r"第\s*3\.2\s*节", "3.2"),
    AnchorSpec("3.4", r"第\s*3\.4\s*节", "3.4"),
    AnchorSpec("[4, 7, 18]", r"\[\s*4\s*,\s*7\s*,\s*18\s*\]", "refs", cluster=True),
    AnchorSpec("策略10", r"策略\s*10", "ref10"),
    AnchorSpec("公式1", r"公式\s*1", "eq1"),
    AnchorSpec("第4节", r"第\s*4\s*节", "4"),
    AnchorSpec("附录B", r"附录\s*B", "B"),
)


def _rect_contains(rect: pymupdf.Rect, point: pymupdf.Point) -> bool:
    return rect.x0 <= point.x <= rect.x1 and rect.y0 <= point.y <= rect.y1


def _center(rect: pymupdf.Rect) -> pymupdf.Point:
    return pymupdf.Point((rect.x0 + rect.x1) / 2.0, (rect.y0 + rect.y1) / 2.0)


def _clip_intersects(bbox: pymupdf.Rect, clip: pymupdf.Rect) -> bool:
    return (
        bbox.x1 >= clip.x0
        and bbox.x0 <= clip.x1
        and bbox.y1 >= clip.y0
        and bbox.y0 <= clip.y1
    )


def extract_blocks(
    doc: pymupdf.Document, page_index: int, clip: pymupdf.Rect
) -> list[TextRun]:
    """Return text blocks whose left edge is inside *clip*.

    The left-edge test keeps the source half of a bilingual page out of the
    translated-side scan; ordinary block clipping is used for vertical bounds.
    """
    page = doc[page_index]
    runs: list[TextRun] = []
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:
            continue
        chars: list[CharBox] = []
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    chars.append(CharBox(char["c"], pymupdf.Rect(char["bbox"])))
        if not chars:
            continue
        bbox = pymupdf.Rect(block["bbox"])
        if bbox.x0 < clip.x0 - 1.0 or not _clip_intersects(bbox, clip):
            continue
        runs.append(
            TextRun(page_index, bbox, "".join(c.char for c in chars), chars)
        )
    runs.sort(key=lambda run: (round(run.bbox.y0, 1), run.bbox.x0))
    return runs


def extract_lines(
    doc: pymupdf.Document, page_index: int, clip: pymupdf.Rect
) -> list[TextRun]:
    """Return text lines intersecting *clip* (line bounding boxes are exact)."""
    page = doc[page_index]
    runs: list[TextRun] = []
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars: list[CharBox] = []
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    chars.append(CharBox(char["c"], pymupdf.Rect(char["bbox"])))
            if not chars:
                continue
            bbox = pymupdf.Rect(line["bbox"])
            if not _clip_intersects(bbox, clip):
                continue
            text = "".join(c.char for c in chars)
            if not text.strip():
                continue
            runs.append(TextRun(page_index, bbox, text, chars))
    runs.sort(key=lambda run: (round(run.bbox.y0, 1), run.bbox.x0))
    return runs


class Context:
    """Opened documents plus the geometry of the translated side."""

    def __init__(self, source_path: Path, translated_path: Path) -> None:
        for path in (source_path, translated_path):
            if not path.exists():
                raise FileNotFoundError(f"PDF not found: {path}")
        self.source_path = source_path
        self.translated_path = translated_path
        self.source = pymupdf.open(str(source_path))
        self.translated = pymupdf.open(str(translated_path))
        if self.source.page_count == 0 or self.translated.page_count == 0:
            raise ValueError("PDF has no pages")

        self.source_width = float(self.source[0].rect.width)
        self.translated_width = float(self.translated[0].rect.width)
        self.bilingual = self.translated_width >= self.source_width * 1.5
        # In a bilingual PDF the translated page starts at the horizontal
        # midpoint.  The source text is expected to use the same page width.
        self.offset = self.translated_width / 2.0 if self.bilingual else 0.0
        self.side_width = self.translated_width - self.offset
        self._blocks: dict[int, list[TextRun]] = {}
        self._lines: dict[tuple[int, float, float, float, float], list[TextRun]] = {}
        self._page_chars: dict[int, list[CharBox]] = {}

    def close(self) -> None:
        self.source.close()
        self.translated.close()

    def side_clip(self) -> pymupdf.Rect:
        return pymupdf.Rect(
            self.offset,
            0.0,
            self.translated_width,
            float(self.translated[0].rect.height),
        )

    def side_blocks(self, page_index: int) -> list[TextRun]:
        if page_index not in self._blocks:
            self._blocks[page_index] = extract_blocks(
                self.translated, page_index, self.side_clip()
            )
        return self._blocks[page_index]

    def side_lines(self, page_index: int, clip: pymupdf.Rect) -> list[TextRun]:
        key = (
            page_index,
            round(clip.x0, 1),
            round(clip.y0, 1),
            round(clip.x1, 1),
            round(clip.y1, 1),
        )
        if key not in self._lines:
            self._lines[key] = extract_lines(self.translated, page_index, clip)
        return self._lines[key]

    def page_chars(self, page_index: int) -> list[CharBox]:
        if page_index not in self._page_chars:
            chars: list[CharBox] = []
            for block in self.translated[page_index].get_text("rawdict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        for char in span.get("chars", []):
                            chars.append(
                                CharBox(char["c"], pymupdf.Rect(char["bbox"]))
                            )
            self._page_chars[page_index] = chars
        return self._page_chars[page_index]

    def cover_text(self, page_index: int, rect: pymupdf.Rect) -> str:
        picked = [
            c.char
            for c in self.page_chars(page_index)
            if _rect_contains(rect, _center(c.bbox))
        ]
        return " ".join("".join(picked).split())[:160]


def source_rows(doc: pymupdf.Document) -> list[SourceRow]:
    rows: list[SourceRow] = []
    for page_index in range(doc.page_count):
        for block in doc[page_index].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(str(s.get("text", "")) for s in spans).strip()
                if not text:
                    continue
                size = max((float(s.get("size", 0.0)) for s in spans), default=0.0)
                rows.append(
                    SourceRow(page_index, pymupdf.Rect(line["bbox"]), text, size)
                )
    return rows


def _heading_pair(rows: list[SourceRow], label: str) -> SourceRow | None:
    """Join a numbered/lettered heading split over two text lines."""
    for index, row in enumerate(rows):
        if row.text != label or row.size < 9.0:
            continue
        for next_row in rows[index + 1 : index + 4]:
            if next_row.page != row.page:
                continue
            if abs(next_row.bbox.y0 - row.bbox.y0) > 2.0:
                continue
            if not re.match(r"^[A-Z][A-Z -]{3,}$", next_row.text):
                continue
            bbox = pymupdf.Rect(row.bbox)
            bbox.include_rect(next_row.bbox)
            return SourceRow(
                row.page,
                bbox,
                f"{row.text} {next_row.text}",
                max(row.size, next_row.size),
            )
    return None


def locate_targets(rows: list[SourceRow]) -> dict[str, SourceRow | None]:
    """Find the definition page of each anchor inside the source PDF."""

    def first(predicate, start: int = 0) -> SourceRow | None:
        for row in rows:
            if row.page < start:
                continue
            if predicate(row):
                return row
        return None

    targets: dict[str, SourceRow | None] = {}
    for number in ("3.1", "3.2", "3.4"):
        targets[number] = first(
            lambda row, n=number: bool(
                re.match(rf"^{re.escape(n)}(?:\s|$)", row.text)
            )
            and len(row.text) <= 60
        )

    targets["4"] = _heading_pair(rows, "4") or first(
        lambda row: (row.text == "4" and row.size >= 9.5)
        or bool(re.match(r"^4\s+[A-Z][A-Z ]{3,}$", row.text))
    )
    targets["B"] = _heading_pair(rows, "B") or first(
        lambda row: bool(re.match(r"^B\s+[A-Z][A-Z ]{3,}$", row.text))
    )

    references = first(lambda row: row.text == "REFERENCES")
    ref_start = references.page if references is not None else 0
    for number in (4, 7, 10, 18):
        targets[f"ref{number}"] = first(
            lambda row, n=number: bool(re.match(rf"^\[{n}\]", row.text)),
            start=ref_start,
        )

    targets["eq1"] = first(lambda row: row.text == "(1)")
    return targets


def target_evidence(row: SourceRow | None) -> dict[str, object]:
    if row is None:
        return {"page": None, "text": None}
    return {"page": row.page + 1, "text": row.text[:90]}


def _link_info(
    ctx: Context, page_index: int, link: dict, expected_page: int | None
) -> dict[str, object]:
    rect = pymupdf.Rect(link["from"])
    target = link.get("page")
    info: dict[str, object] = {
        "cover": ctx.cover_text(page_index, rect),
        "from": [round(v, 1) for v in rect],
        "kind": "goto" if link.get("kind") == pymupdf.LINK_GOTO else "uri",
        "target_page": (target + 1) if target is not None else None,
        "target_page_0": target,
        "expected_page": (expected_page + 1) if expected_page is not None else None,
        "uri": (link.get("uri") or "")[:100],
    }
    info["target_ok"] = (
        target == expected_page if expected_page is not None else False
    )
    return info


def _inspect_chars(
    ctx: Context,
    page_index: int,
    chars: list[CharBox],
    expected_page: int | None,
    label: str,
) -> dict[str, object]:
    page = ctx.translated[page_index]
    links = page.get_links()
    missing: list[str] = []
    found: dict[tuple[float, ...], dict[str, object]] = {}

    for char in chars:
        point = _center(char.bbox)
        hits = [
            link for link in links if _rect_contains(pymupdf.Rect(link["from"]), point)
        ]
        if not hits:
            missing.append(char.char)
        for link in hits:
            key = tuple(round(v, 1) for v in link["from"])
            if key not in found:
                found[key] = _link_info(ctx, page_index, link, expected_page)

    wrong_targets = [info for info in found.values() if not info["target_ok"]]
    if expected_page is None:
        reason = "源 PDF 中未定位到目标页"
    elif not found:
        reason = "无链接"
    elif missing:
        reason = f"覆盖不完整，缺少字符 {missing!r}"
    elif wrong_targets:
        reason = "链接目标页错误"
    else:
        reason = ""

    return {
        "label": label,
        "chars": "".join(c.char for c in chars),
        "expected_page": (expected_page + 1) if expected_page is not None else None,
        "missing": missing,
        "links": sorted(found.values(), key=lambda item: item["from"]),
        "ok": bool(found) and not missing and not wrong_targets and expected_page is not None,
        "reason": reason,
    }


def inspect_occurrence(
    ctx: Context,
    run: TextRun,
    match: re.Match[str],
    spec: AnchorSpec,
    targets: dict[str, SourceRow | None],
) -> dict[str, object]:
    anchor_chars = run.chars[match.start() : match.end()]
    context_text = run.text[max(0, match.start() - 35) : match.end() + 35]
    context_text = " ".join(context_text.split())
    result: dict[str, object] = {
        "page": run.page_index + 1,
        "context": context_text,
        "text": match.group(0),
        "groups": [],
        "ok": False,
        "reason": "",
    }

    if spec.cluster:
        groups = []
        for number in re.finditer(r"\d+", match.group(0)):
            chars = anchor_chars[number.start() : number.end()]
            expected_row = targets.get(f"ref{number.group()}")
            expected_page = expected_row.page if expected_row is not None else None
            groups.append(
                _inspect_chars(
                    ctx,
                    run.page_index,
                    chars,
                    expected_page,
                    f"[{number.group()}]",
                )
            )
        result["groups"] = groups
        result["ok"] = bool(groups) and all(group["ok"] for group in groups)
        failed = [group["label"] for group in groups if not group["ok"]]
        result["reason"] = "" if result["ok"] else f"引用簇失败: {failed}"
        return result

    effective = [char for char in anchor_chars if EFFECTIVE_RE.fullmatch(char.char)]
    expected_row = targets.get(spec.target)
    expected_page = expected_row.page if expected_row is not None else None
    group = _inspect_chars(ctx, run.page_index, effective, expected_page, spec.key)
    result["groups"] = [group]
    result["ok"] = group["ok"]
    result["reason"] = group["reason"]
    return result


def check_links(
    ctx: Context, targets: dict[str, SourceRow | None], target_rows: dict[str, dict]
) -> dict[str, object]:
    total = 0
    uri_count = 0
    for page_index in range(ctx.translated.page_count):
        for link in ctx.translated[page_index].get_links():
            rect = pymupdf.Rect(link["from"])
            if rect.x0 < ctx.offset - 1.0:
                continue
            total += 1
            if link.get("kind") == pymupdf.LINK_URI:
                uri_count += 1

    anchors = []
    passed_anchors = 0
    for spec in ANCHORS:
        occurrences = []
        for page_index in range(ctx.translated.page_count):
            for run in ctx.side_blocks(page_index):
                for match in re.finditer(spec.pattern, run.text):
                    occurrences.append(
                        inspect_occurrence(ctx, run, match, spec, targets)
                    )
        ok = bool(occurrences) and all(item["ok"] for item in occurrences)
        if ok:
            passed_anchors += 1
        anchor_evidence: dict[str, object] = {
            "anchor": spec.key,
            "target": target_rows.get(spec.target),
            "occurrences": occurrences,
            "ok": ok,
        }
        if spec.cluster:
            anchor_evidence["targets"] = {
                f"ref{n}": target_rows.get(f"ref{n}") for n in (4, 7, 18)
            }
        anchors.append(anchor_evidence)

    failed = [anchor for anchor in anchors if not anchor["ok"]]
    details = [f"译文侧链接数: {total} (页内 {total - uri_count}, URI {uri_count})"]
    for anchor in anchors:
        status = "PASS" if anchor["ok"] else "FAIL"
        details.append(f"{anchor['anchor']}: {len(anchor['occurrences'])} 处 -> {status}")
        target = anchor.get("target") or {}
        if target and target.get("page"):
            details.append(
                f"  源目标页: p{target['page']} ({str(target.get('text'))[:55]})"
            )
        for occurrence in anchor["occurrences"]:
            if occurrence["ok"]:
                continue
            details.append(
                f"  p{occurrence['page']}: {occurrence['context'][:90]}"
            )
            details.append(f"    -> {occurrence['reason']}")
            for group in occurrence["groups"]:
                covers = ", ".join(
                    f"{link['cover']!r}->p{link['target_page']}"
                    for link in group["links"]
                ) or "(无)"
                details.append(
                    f"    {group['label']}: 锚字符 {group['chars']!r}; "
                    f"链接 {covers}; 期望 p{group['expected_page']}"
                )
    if len(details) > 80:
        details = details[:80] + ["  ...（证据过长，已截断）"]

    status = "PASS" if passed_anchors == len(ANCHORS) else "FAIL"
    summary = (
        f"译文侧链接 {total} 个；{len(ANCHORS)} 个锚点中 {passed_anchors} 个通过"
    )
    return {
        "id": "links",
        "name": "译文链接",
        "status": status,
        "summary": summary,
        "evidence": {
            "translated_link_count": total,
            "uri_link_count": uri_count,
            "anchors": anchors,
            "failed_anchors": [anchor["anchor"] for anchor in failed],
        },
        "details": details,
    }


def _detector_labels(doc: pymupdf.Document, page_index: int, clip: pymupdf.Rect) -> list[str]:
    labels: list[str] = []
    for run in extract_lines(doc, page_index, clip):
        labels.extend(DETECTOR_RE.findall(run.text))
    return sorted(set(labels))


def check_figure4(ctx: Context) -> dict[str, object]:
    if (
        PAGE_FIG4 >= ctx.source.page_count
        or PAGE_FIG4 >= ctx.translated.page_count
    ):
        return {
            "id": "figure4",
            "name": "图4 轴标签",
            "status": "FAIL",
            "summary": f"PDF 页数不足，无法检查第 {PAGE_FIG4 + 1} 页",
            "evidence": {},
            "details": [f"源 {ctx.source.page_count} 页 / 译文 {ctx.translated.page_count} 页"],
        }

    source_clip = pymupdf.Rect(0.0, 150.0, ctx.source_width, 235.0)
    translated_clip = pymupdf.Rect(
        ctx.offset, 150.0, ctx.offset + ctx.side_width, 235.0
    )
    source_labels = _detector_labels(ctx.source, PAGE_FIG4, source_clip)
    translated_labels = _detector_labels(
        ctx.translated, PAGE_FIG4, translated_clip
    )

    status = "PASS" if translated_labels else "FAIL"
    summary = (
        f"源图4 轴标签 {len(source_labels)} 个；"
        f"译文侧图4 区域 {len(translated_labels)} 个"
    )
    details = [
        f"检查页: p{PAGE_FIG4 + 1}，译文侧 y=150..235",
        summary,
        f"源标签样例: {', '.join(source_labels[:10]) or '(无)'}",
        f"译标签样例: {', '.join(translated_labels[:10]) or '(无)'}",
    ]
    if status == "FAIL":
        details.append(
            "译文图4 区域没有 A-Acc/W-Acc/A-Dns 等轴标签（横坐标文字整体未绘制）"
        )
    return {
        "id": "figure4",
        "name": "图4 轴标签",
        "status": status,
        "summary": summary,
        "evidence": {
            "page": PAGE_FIG4 + 1,
            "source_label_count": len(source_labels),
            "translated_label_count": len(translated_labels),
            "source_sample": source_labels[:15],
            "translated_sample": translated_labels[:15],
        },
        "details": details,
    }


def _figure5_source_labels(ctx: Context) -> list[TextRun]:
    clip = pymupdf.Rect(0.0, 80.0, 130.0, 185.0)
    labels = []
    for run in extract_lines(ctx.source, PAGE_FIG5, clip):
        text = run.text.strip()
        if run.bbox.x1 <= 80.0:
            continue
        if not re.search(r"[A-Za-z]", text):
            continue
        if "-" in text:
            continue
        if len(text) > 40:
            continue
        labels.append(run)
    return labels


def _figure5_translated_labels(ctx: Context) -> list[TextRun]:
    clip = pymupdf.Rect(ctx.offset, 80.0, ctx.offset + 170.0, 185.0)
    labels = []
    for run in ctx.side_lines(PAGE_FIG5, clip):
        text = run.text.strip()
        if not CJK_RE.search(text):
            continue
        if len(text) > 30:
            continue
        labels.append(run)
    return labels


def check_figure5(ctx: Context) -> dict[str, object]:
    if (
        PAGE_FIG5 >= ctx.source.page_count
        or PAGE_FIG5 >= ctx.translated.page_count
    ):
        return {
            "id": "figure5",
            "name": "图5 行标签右对齐",
            "status": "FAIL",
            "summary": f"PDF 页数不足，无法检查第 {PAGE_FIG5 + 1} 页",
            "evidence": {},
            "details": [f"源 {ctx.source.page_count} 页 / 译文 {ctx.translated.page_count} 页"],
        }

    source_labels = _figure5_source_labels(ctx)
    translated_labels = _figure5_translated_labels(ctx)
    source_x1 = [run.bbox.x1 for run in source_labels]
    translated_x1 = [run.bbox.x1 for run in translated_labels]

    expected_x1 = (
        statistics.median(source_x1) + ctx.offset if source_x1 else None
    )
    spread = (
        max(translated_x1) - min(translated_x1) if translated_x1 else None
    )
    deviations = (
        [abs(x1 - expected_x1) for x1 in translated_x1]
        if expected_x1 is not None
        else []
    )
    max_deviation = max(deviations) if deviations else None
    count_ok = len(translated_labels) >= 3
    spread_ok = spread is not None and spread <= ALIGN_TOLERANCE
    target_ok = (
        bool(deviations) and max_deviation is not None
        and max_deviation <= ALIGN_TOLERANCE
    )
    status = "PASS" if count_ok and spread_ok and target_ok else "FAIL"
    if translated_x1 and expected_x1 is not None:
        summary = (
            f"源行标签 {len(source_labels)} 个，右边界 {expected_x1:.1f}pt；"
            f"译文中文字标签 {len(translated_labels)} 个，"
            f"x1 范围 {min(translated_x1):.1f}..{max(translated_x1):.1f}pt"
        )
    else:
        summary = f"源行标签 {len(source_labels)} 个；译文中文行标签 {len(translated_labels)} 个"
    details = [
        f"检查页: p{PAGE_FIG5 + 1}，译文侧 y=80..185",
        summary,
    ]
    if spread is not None:
        deviation_text = (
            f"，最大偏差 {max_deviation:.1f}pt"
            if max_deviation is not None
            else ""
        )
        details.append(
            f"x1 极差 {spread:.1f}pt (容差 {ALIGN_TOLERANCE}pt){deviation_text}"
        )
    details.extend(
        f"  {run.text.strip()!r} x1={run.bbox.x1:.1f} y0={run.bbox.y0:.1f}"
        for run in translated_labels
    )
    if status == "FAIL":
        if not count_ok:
            details.append("译文侧未找到足够的中文行标签")
        else:
            details.append("中文行标签右边界不一致，图5 行标签失去右对齐")
    return {
        "id": "figure5",
        "name": "图5 行标签右对齐",
        "status": status,
        "summary": summary,
        "evidence": {
            "page": PAGE_FIG5 + 1,
            "source_label_count": len(source_labels),
            "source_x1_median": round(expected_x1 - ctx.offset, 1)
            if expected_x1 is not None
            else None,
            "expected_x1": round(expected_x1, 1) if expected_x1 is not None else None,
            "translated_label_count": len(translated_labels),
            "x1_spread": round(spread, 2) if spread is not None else None,
            "max_deviation": round(max_deviation, 2)
            if max_deviation is not None
            else None,
            "tolerance": ALIGN_TOLERANCE,
            "labels": [
                {
                    "text": run.text.strip(),
                    "x0": round(run.bbox.x0, 1),
                    "x1": round(run.bbox.x1, 1),
                    "y0": round(run.bbox.y0, 1),
                }
                for run in translated_labels
            ],
        },
        "details": details,
    }


def check_commas(ctx: Context) -> dict[str, object]:
    findings = []
    for page_index in range(ctx.translated.page_count):
        for run in ctx.side_blocks(page_index):
            normalized = run.text.replace("，", ",")
            for match in re.finditer(r",\s*,", normalized):
                start = max(0, match.start() - 45)
                end = min(len(run.text), match.end() + 45)
                findings.append(
                    {
                        "page": page_index + 1,
                        "matched": run.text[match.start() : match.end()],
                        "normalized": match.group(0),
                        "context": " ".join(run.text[start:end].split()),
                        "bbox": [round(v, 1) for v in run.bbox],
                    }
                )
                if len(findings) >= 40:
                    break
    status = "PASS" if not findings else "FAIL"
    summary = f"重复逗号命中 {len(findings)} 处" if findings else "未发现重复逗号"
    details = [summary]
    for finding in findings[:12]:
        details.append(
            f"  p{finding['page']}: {finding['context'][:110]} "
            f"(matched {finding['matched']!r})"
        )
    if len(findings) > 12:
        details.append(f"  ... 另有 {len(findings) - 12} 处")
    if findings:
        details.append('已知缺陷模式 "[0, 1], ," / ", ," 仍存在')
    return {
        "id": "commas",
        "name": "重复逗号",
        "status": status,
        "summary": summary,
        "evidence": {
            "count": len(findings),
            "findings": findings,
        },
        "details": details,
    }


def check_table2(ctx: Context) -> dict[str, object]:
    if (
        PAGE_TABLE2 >= ctx.source.page_count
        or PAGE_TABLE2 >= ctx.translated.page_count
    ):
        return {
            "id": "table2",
            "name": "表2 旋转列标题",
            "status": "FAIL",
            "summary": f"PDF 页数不足，无法检查第 {PAGE_TABLE2 + 1} 页",
            "evidence": {},
            "details": [f"源 {ctx.source.page_count} 页 / 译文 {ctx.translated.page_count} 页"],
        }

    source_clip = pymupdf.Rect(360.0, 260.0, 560.0, 316.5)
    translated_clip = pymupdf.Rect(
        ctx.offset + 350.0, 260.0, ctx.offset + 600.0, 316.5
    )
    source_titles = [
        run.text.strip()
        for run in extract_lines(ctx.source, PAGE_TABLE2, source_clip)
        if len(run.text.strip()) >= 3
    ]
    translated_lines = [
        run
        for run in ctx.side_lines(PAGE_TABLE2, translated_clip)
        if len(run.text.strip()) >= 2
    ]
    translated_titles = [run.text.strip() for run in translated_lines]
    cjk_texts = [
        text for text in translated_titles if CJK_RE.search(text)
    ]
    cjk_chars = sum(
        1 for text in translated_titles for char in text if CJK_RE.match(char)
    )
    status = "PASS" if len(cjk_texts) >= 2 else "FAIL"
    summary = (
        f"源列标题 {len(source_titles)} 个；"
        f"译文表头 {len(translated_titles)} 个，其中含 CJK {len(cjk_texts)} 个"
    )
    details = [
        f"检查页: p{PAGE_TABLE2 + 1}，表2 表头区域",
        summary,
        f"源标题: {', '.join(source_titles[:15])}",
        f"译标题: {', '.join(translated_titles[:15])}",
    ]
    if status == "FAIL":
        details.append("表2 旋转列标题仍为英文，未翻译")
    return {
        "id": "table2",
        "name": "表2 旋转列标题",
        "status": status,
        "summary": summary,
        "evidence": {
            "page": PAGE_TABLE2 + 1,
            "source_titles": source_titles,
            "translated_titles": translated_titles,
            "cjk_title_count": len(cjk_texts),
            "cjk_titles": cjk_texts,
            "cjk_char_count": cjk_chars,
        },
        "details": details,
    }


def _table3_source_map(ctx: Context) -> dict[str, str]:
    clip = pymupdf.Rect(60.0, 100.0, 300.0, 415.0)
    lines = extract_lines(ctx.source, PAGE_TABLE3, clip)
    abbreviations: dict[str, float] = {}
    descriptions: dict[float, str] = {}
    for run in lines:
        text = run.text.strip()
        if ABBR_RE.fullmatch(text) and run.bbox.x0 > 230.0:
            abbreviations[text] = run.bbox.y0
        elif run.bbox.x0 < 230.0 and len(text) >= 2:
            descriptions[run.bbox.y0] = text

    mapping: dict[str, str] = {}
    for abbreviation, y in abbreviations.items():
        candidates = [
            (abs(y - desc_y), desc)
            for desc_y, desc in descriptions.items()
        ]
        if not candidates:
            continue
        difference, description = min(candidates)
        if difference <= 6.0:
            mapping[abbreviation] = description
    return mapping


def check_table3(ctx: Context) -> dict[str, object]:
    if (
        PAGE_TABLE3 >= ctx.source.page_count
        or PAGE_TABLE3 >= ctx.translated.page_count
    ):
        return {
            "id": "table3",
            "name": "表3 整句翻译",
            "status": "FAIL",
            "summary": f"PDF 页数不足，无法检查第 {PAGE_TABLE3 + 1} 页",
            "evidence": {},
            "details": [f"源 {ctx.source.page_count} 页 / 译文 {ctx.translated.page_count} 页"],
        }

    source_map = _table3_source_map(ctx)
    probes = [
        abbreviation
        for abbreviation in (
            "A-Acc-Chr1",
            "A-Acc-Chr2",
            "A-Acc-Clc",
            "A-Acc-Ent1",
            "A-Acc-Ent3",
            "A-Acc-Frq",
            "A-Aud-Com1",
            "A-Aud-Com2",
            "A-Dns-Chr",
            "A-Dns-Clc1",
            "A-Dns-Val1",
            "A-Mon-Avg",
        )
        if abbreviation in source_map
    ]

    clip = pymupdf.Rect(
        ctx.offset + 50.0, 100.0, ctx.offset + 310.0, 415.0
    )
    translated_lines = ctx.side_lines(PAGE_TABLE3, clip)
    translated_by_abbr: dict[str, TextRun] = {}
    for run in translated_lines:
        text = run.text.strip()
        if ABBR_RE.fullmatch(text) and run.bbox.x0 > ctx.offset + 230.0:
            translated_by_abbr[text] = run

    results = []
    for abbreviation in probes:
        source_description = source_map[abbreviation]
        description = ""
        if abbreviation in translated_by_abbr:
            abbr_run = translated_by_abbr[abbreviation]
            candidates = [
                run
                for run in translated_lines
                if run.bbox.x1 < abbr_run.bbox.x0 - 5.0
                and abs(run.bbox.y0 - abbr_run.bbox.y0) <= 6.0
                and len(run.text.strip()) >= 2
            ]
            if candidates:
                candidates.sort(
                    key=lambda run: abs(run.bbox.y0 - abbr_run.bbox.y0)
                )
                description = candidates[0].text.strip()
        has_cjk = bool(CJK_RE.search(description))
        results.append(
            {
                "abbreviation": abbreviation,
                "source_description": source_description,
                "translated_description": description,
                "has_cjk": has_cjk,
            }
        )

    translated_text = " ".join(run.text for run in translated_lines)
    source_text = " ".join(
        run.text
        for run in extract_lines(
            ctx.source, PAGE_TABLE3, pymupdf.Rect(60.0, 100.0, 300.0, 415.0)
        )
    )
    preserved = {
        "Apache": "Apache" in translated_text,
        "A-Acc-Chr1": "A-Acc-Chr1" in translated_text,
        "A-Mon-Avg": "A-Mon-Avg" in translated_text,
    }
    if "CIP" in source_text:
        preserved["CIP"] = "CIP" in translated_text

    translated_count = sum(1 for item in results if item["has_cjk"])
    status = (
        "PASS"
        if results
        and translated_count == len(results)
        and all(preserved.values())
        else "FAIL"
    )
    summary = (
        f"抽查 {len(results)} 条整句描述：{translated_count} 条含 CJK，"
        f"{len(results) - translated_count} 条未翻译"
    )
    details = [f"检查页: p{PAGE_TABLE3 + 1}，表3 区域", summary]
    for item in results:
        status_text = "PASS" if item["has_cjk"] else "FAIL"
        details.append(
            f"  {item['abbreviation']}: {status_text} "
            f"{item['translated_description'][:80]!r}"
        )
    details.append(
        "专名/缩写保留: "
        + ", ".join(f"{name}={value}" for name, value in preserved.items())
    )
    if "CIP" not in source_text:
        details.append("CIP 未在源表3 中出现，跳过该专名")
    if status == "FAIL":
        details.append("表3 仍有整句描述未翻译（部分翻译、部分未翻译）")
    return {
        "id": "table3",
        "name": "表3 整句翻译",
        "status": status,
        "summary": summary,
        "evidence": {
            "page": PAGE_TABLE3 + 1,
            "probe_count": len(results),
            "translated_count": translated_count,
            "probes": results,
            "preserved": preserved,
        },
        "details": details,
    }


def build_report(ctx: Context) -> dict[str, object]:
    rows = source_rows(ctx.source)
    targets = locate_targets(rows)
    target_rows = {
        key: target_evidence(targets.get(key))
        for key in (
            "3.1",
            "3.2",
            "3.4",
            "4",
            "B",
            "ref4",
            "ref7",
            "ref10",
            "ref18",
            "eq1",
        )
    }
    checks = [
        check_links(ctx, targets, target_rows),
        check_figure4(ctx),
        check_figure5(ctx),
        check_commas(ctx),
        check_table2(ctx),
        check_table3(ctx),
    ]
    failed = [check for check in checks if check["status"] == "FAIL"]
    return {
        "source": str(ctx.source_path),
        "translated": str(ctx.translated_path),
        "source_pages": ctx.source.page_count,
        "translated_pages": ctx.translated.page_count,
        "bilingual": ctx.bilingual,
        "translated_side": "right half (offset=%.1f)" % ctx.offset
        if ctx.bilingual
        else "whole page",
        "targets": target_rows,
        "checks": checks,
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "status": "PASS" if not failed else "FAIL",
    }


def render_text(report: dict[str, object]) -> str:
    lines = [
        f"PDF quality check: {report['status']}",
        f"source: {report['source']}",
        f"translated: {report['translated']}",
        f"pages: {report['source_pages']} -> {report['translated_pages']}; "
        f"bilingual={report['bilingual']}; side={report['translated_side']}",
    ]
    checks = report["checks"]
    assert isinstance(checks, list)
    for index, check in enumerate(checks, 1):
        assert isinstance(check, dict)
        lines.append("")
        lines.append(
            f"[{index}/{len(checks)}] {check['name']} .......... {check['status']}"
        )
        lines.append(f"  {check['summary']}")
        for detail in check.get("details", []):
            lines.append(f"  {detail}")
    lines.append("")
    lines.append(
        f"RESULT: {report['status']} "
        f"({report['failed']}/{len(checks)} failed)"
    )
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    # Avoid UnicodeEncodeError for mathematical glyphs in the evidence when
    # stdout is a legacy Windows code page.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=(
            "Check a BabelDOC bilingual PDF against its source PDF. "
            "Exit code 0 only when every check passes."
        )
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="source PDF")
    parser.add_argument(
        "--translated", default=DEFAULT_TRANSLATED, help="translated (bilingual) PDF"
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON"
    )
    args = parser.parse_args(argv)

    try:
        ctx = Context(Path(args.source), Path(args.translated))
    except Exception as exc:  # opening errors are user-facing, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        report = build_report(ctx)
    finally:
        ctx.close()

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
