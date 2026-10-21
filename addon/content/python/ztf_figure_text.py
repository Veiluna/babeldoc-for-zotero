"""Restore figure-internal labels and right-align translated row labels.

BabelDOC 0.6.4 drops some heatmap axis labels when it converts figure text into
layout elements (visible even with --skip-translation).  The labels are mostly
identifiers (A-Acc-Chr1, W-All-Mul1), so the correct output keeps them verbatim
at their original positions.  Translated row labels next to a figure lose the
source right alignment; this module re-anchors those labels to the source right
edge.
"""

from __future__ import annotations

import math
import os
import re

import pymupdf


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALNUM_RE = re.compile(r"[0-9A-Za-z]")
_LABEL_RE = re.compile(r"^[\w.\-/()+]{1,40}$")


def _text(value: str) -> str:
    return " ".join(str(value or "").split())


def _is_short_label(value: str) -> bool:
    text = _text(value)
    if not text or len(text) > 40:
        return False
    if not _ALNUM_RE.search(text):
        return False
    return bool(_LABEL_RE.fullmatch(text)) or len(text.split()) <= 4


def _figure_regions(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Cluster short text blocks into figure-like horizontal bands."""
    blocks = []
    for block in page.get_text("blocks"):
        text = _text(block[4])
        if _is_short_label(text):
            blocks.append(block)
    blocks.sort(key=lambda block: (block[1], block[0]))
    clusters: list[dict] = []
    for block in blocks:
        target = None
        for cluster in clusters:
            if block[1] <= cluster["y1"] + 10 and block[3] >= cluster["y0"] - 10:
                target = cluster
                break
        if target is None:
            clusters.append({
                "x0": block[0], "y0": block[1], "x1": block[2], "y1": block[3],
                "count": 1,
            })
        else:
            target["x0"] = min(target["x0"], block[0])
            target["x1"] = max(target["x1"], block[2])
            target["y0"] = min(target["y0"], block[1])
            target["y1"] = max(target["y1"], block[3])
            target["count"] += 1
    regions = []
    for cluster in clusters:
        width = cluster["x1"] - cluster["x0"]
        height = cluster["y1"] - cluster["y0"]
        if cluster["count"] >= 8 and width >= page.rect.width * 0.4 and height >= 40:
            regions.append(pymupdf.Rect(
                cluster["x0"] - 2, cluster["y0"] - 2,
                cluster["x1"] + 2, cluster["y1"] + 2,
            ))
    return regions


def _shift(rect: pymupdf.Rect, dx: float, dy: float) -> pymupdf.Rect:
    return pymupdf.Rect(rect.x0 + dx, rect.y0 + dy, rect.x1 + dx, rect.y1 + dy)


def _source_lines(page: pymupdf.Page, region: pymupdf.Rect) -> list[dict]:
    lines = []
    try:
        data = page.get_text("dict", clip=region)
    except Exception:
        return lines
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text or not _ALNUM_RE.search(text) or len(text) > 60:
                continue
            direction = line.get("dir") or (1.0, 0.0)
            angle = math.degrees(math.atan2(-float(direction[1]), float(direction[0])))
            span = line.get("spans", [{}])[0]
            lines.append({
                "text": _text(text),
                "rect": pymupdf.Rect(line["bbox"]),
                "size": float(span.get("size") or 8.0),
                "angle": angle,
            })
    return lines


def _restore_region(source_page: pymupdf.Page, output_page: pymupdf.Page,
                    region: pymupdf.Rect, offset_x: float) -> dict:
    stats = {"restored_labels": 0, "aligned_labels": 0}
    output_region = _shift(region, offset_x, 0)
    source_lines = _source_lines(source_page, region)
    if not source_lines:
        return stats

    # 1) Put back identifier labels that BabelDOC dropped.
    for line in source_lines:
        if _CJK_RE.search(line["text"]):
            continue
        if not _is_short_label(line["text"]):
            continue
        existing = output_page.search_for(line["text"], clip=output_region)
        if existing:
            continue
        point = pymupdf.Point(
            line["rect"].x0 + offset_x, line["rect"].y1 - 1.0
        )
        angle = float(line.get("angle") or 0.0)
        try:
            if abs(angle) <= 5:
                output_page.insert_text(
                    point, line["text"], fontname="helv",
                    fontsize=max(4.0, line["size"]), color=(0, 0, 0),
                )
            else:
                # insert_text 只接受 0/90/180/270；morph 支持任意角度旋转。
                output_page.insert_text(
                    point, line["text"], fontname="helv",
                    fontsize=max(4.0, line["size"]), color=(0, 0, 0),
                    morph=(point, pymupdf.Matrix(angle)),
                )
            stats["restored_labels"] += 1
        except Exception:
            continue

    # 2) Re-anchor translated row labels to the source right edge.
    source_right = {}
    for line in source_lines:
        y = round(line["rect"].y0 / 3.0)
        source_right[y] = line["rect"].x1 + offset_x
    redactions = []
    replacements = []
    for block in output_page.get_text("dict", clip=output_region).get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text or not _CJK_RE.search(text) or len(text) > 60:
                continue
            rect = pymupdf.Rect(line["bbox"])
            nearest = None
            best = 6.0
            for key, target in source_right.items():
                distance = abs(rect.y0 - key * 3.0)
                if distance <= best:
                    best = distance
                    nearest = target
            if nearest is None or abs(rect.x1 - nearest) <= 2.0:
                continue
            span = line.get("spans", [{}])[0]
            size = max(4.0, float(span.get("size") or 8.0))
            try:
                width = pymupdf.get_text_length(text, fontname="china-s", fontsize=size)
            except Exception:
                continue
            redactions.append(rect)
            replacements.append((pymupdf.Point(nearest - width, rect.y1 - 1.0), text, size))
    if replacements:
        for rect in redactions:
            output_page.add_redact_annot(rect, fill=False)
        try:
            output_page.apply_redactions()
        except Exception:
            return stats
        for point, text, size in replacements:
            try:
                output_page.insert_text(
                    point, text, fontname="china-s", fontsize=size, color=(0, 0, 0)
                )
                stats["aligned_labels"] += 1
            except Exception:
                continue
    return stats


def restore_figure_text(source_path: str, output_path: str) -> dict:
    """Rewrite the translated PDF, restoring figure labels in place."""
    source = pymupdf.open(source_path)
    output = pymupdf.open(output_path)
    try:
        dual = len(output) > 0 and len(source) > 0 and (
            output[0].rect.width > source[0].rect.width * 1.5
        )
        offset_x = source[0].rect.width if dual else 0.0
        stats = {"pages": 0, "regions": 0, "restored_labels": 0, "aligned_labels": 0}
        for index in range(min(len(source), len(output))):
            source_page = source[index]
            output_page = output[index]
            regions = _figure_regions(source_page)
            stats["regions"] += len(regions)
            page_stats = None
            for region in regions:
                current = _restore_region(source_page, output_page, region, offset_x)
                if page_stats is None:
                    page_stats = current
                else:
                    for key, value in current.items():
                        page_stats[key] += value
            if page_stats:
                for key, value in page_stats.items():
                    stats[key] += value
                stats["pages"] += 1
        temporary = output_path + ".figuretmp"
        output.save(temporary, garbage=3, deflate=True)
        output.close()
        os.replace(temporary, output_path)
        return stats
    finally:
        try:
            source.close()
        except Exception:
            pass
