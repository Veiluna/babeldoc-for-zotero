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


def _overlaps(first: pymupdf.Rect, second: pymupdf.Rect,
              tolerance: float = 0.5) -> bool:
    """True when two rectangles intersect, with a small optional tolerance."""
    return (
        first.x0 < second.x1 + tolerance
        and first.x1 > second.x0 - tolerance
        and first.y0 < second.y1 + tolerance
        and first.y1 > second.y0 - tolerance
    )


def _vertical_overlap_ratio(first: pymupdf.Rect,
                            second: pymupdf.Rect) -> float:
    """Fraction of *first*'s height that lies inside *second*."""
    height = first.y1 - first.y0
    if height <= 0:
        return 0.0
    overlap = min(first.y1, second.y1) - max(first.y0, second.y0)
    return max(0.0, overlap) / height


_ASCII_RUN_RE = re.compile(r"[ -~]+")


def _mixed_runs(text: str) -> list[tuple[str, str]]:
    """Split text into printable-ASCII and CJK runs with their fonts.

    The built-in china-s font renders ASCII glyphs at full CJK width, which
    turns mixed labels such as "WordPress扫描" into visibly spaced-out text.
    ASCII runs are therefore drawn with helv, CJK runs with china-s.
    """
    runs = []
    for match in re.finditer(r"[ -~]+|[^ -~]+", text):
        run = match.group(0)
        fontname = "helv" if _ASCII_RUN_RE.fullmatch(run) else "china-s"
        runs.append((run, fontname))
    return runs


def _mixed_width(text: str, size: float) -> float:
    return sum(
        pymupdf.get_text_length(run, fontname=fontname, fontsize=size)
        for run, fontname in _mixed_runs(text)
    )


def _insert_mixed_text(page: pymupdf.Page, point: pymupdf.Point,
                       text: str, size: float) -> None:
    x = float(point.x)
    for run, fontname in _mixed_runs(text):
        page.insert_text(
            (x, point.y), run, fontname=fontname, fontsize=size,
            color=(0, 0, 0),
        )
        x += pymupdf.get_text_length(run, fontname=fontname, fontsize=size)


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
    stats = {"restored_labels": 0, "aligned_labels": 0, "removed_fragments": 0}
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

    # 2) Re-anchor translated row labels to the source right edge.  While
    #    doing so, remove leftover English fragments that overlap a Chinese
    #    row label (BabelDOC keeps the untranslated row text, or this helper
    #    re-draws it as a short label, underneath the translation).
    source_right = {}
    for line in source_lines:
        y = round(line["rect"].y0 / 3.0)
        source_right[y] = line["rect"].x1 + offset_x
    chinese_rows = []
    for block in output_page.get_text("dict", clip=output_region).get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text or not _CJK_RE.search(text) or len(text) > 60:
                continue
            rect = pymupdf.Rect(line["bbox"])
            if not _overlaps(rect, output_region, 0.0):
                continue
            nearest = None
            best = 6.0
            for key, target in source_right.items():
                distance = abs(rect.y0 - key * 3.0)
                if distance <= best:
                    best = distance
                    nearest = target
            span = line.get("spans", [{}])[0]
            size = max(4.0, float(span.get("size") or 8.0))
            realign = nearest is not None and abs(rect.x1 - nearest) > 2.0
            right = nearest if realign else rect.x1
            try:
                width = _mixed_width(text, size)
            except Exception:
                if realign:
                    continue
                width = None
            if width is None:
                # The row cannot be redrawn, so never redact fragments that
                # overlap it: the row would disappear with them.
                target_rect = rect
                point = None
            else:
                target_rect = pymupdf.Rect(
                    right - width, rect.y0, right, rect.y1
                )
                point = pymupdf.Point(right - width, rect.y1 - 1.0)
            chinese_rows.append({
                "rect": rect,
                "target": target_rect,
                "point": point,
                "text": text,
                "size": size,
                "realign": realign,
                "force": False,
            })

    # Collect horizontal English fragments overlapping a Chinese row label.
    # get_text("dict", clip=...) may also return lines from blocks that start
    # on the source half, so require the line bbox itself to sit on the
    # translated side.  Rotated text (heat-map numbers, 45-degree axis labels,
    # "Attack phase"), pure numbers and unrelated English are left alone.
    fragments = []
    if chinese_rows:
        for block in output_page.get_text("dict", clip=output_region).get("blocks", []):
            for line in block.get("lines", []):
                text = "".join(
                    span.get("text", "") for span in line.get("spans", [])
                ).strip()
                if not text or _CJK_RE.search(text):
                    continue
                if len(text) < 2 or len(text) > 60:
                    continue
                if not re.search(r"[A-Za-z]", text):
                    continue
                direction = line.get("dir") or (1.0, 0.0)
                angle = math.degrees(
                    math.atan2(-float(direction[1]), float(direction[0]))
                )
                if abs(angle) > 5.0:
                    continue
                span = line.get("spans", [{}])[0]
                if float(span.get("size") or 0.0) < 2.0:
                    continue
                rect = pymupdf.Rect(line["bbox"])
                if not _overlaps(rect, output_region, 0.0):
                    continue
                # Require the fragment to sit mostly on the Chinese row.
                # Merely touching the next table line (a 0.5pt overlap) must
                # not count, otherwise ordinary table content is removed.
                hits = [
                    row for row in chinese_rows
                    if row["point"] is not None
                    and _overlaps(rect, row["target"])
                    and _vertical_overlap_ratio(rect, row["target"]) >= 0.5
                ]
                if not hits:
                    continue
                fragments.append(rect)
                for row in hits:
                    # A fragment is removed with a redaction; redraw the
                    # Chinese label even if it was already aligned, otherwise
                    # the redaction would delete it without a replacement.
                    row["force"] = True

    redactions = [
        row["rect"] for row in chinese_rows if row["realign"] or row["force"]
    ] + fragments
    stats["removed_fragments"] = len(fragments)
    if redactions:
        for rect in redactions:
            output_page.add_redact_annot(rect, fill=False)
        try:
            output_page.apply_redactions()
        except Exception:
            return stats
        for row in chinese_rows:
            if not (row["realign"] or row["force"]):
                continue
            try:
                _insert_mixed_text(
                    output_page, row["point"], row["text"], row["size"]
                )
                if row["realign"]:
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
        stats = {
            "pages": 0, "regions": 0, "restored_labels": 0,
            "aligned_labels": 0, "removed_fragments": 0,
        }
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
