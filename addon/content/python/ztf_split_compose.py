"""离线合成分栏双语 PDF，不发起网络请求。

输入源 PDF、同页单语 PDF 和冻结的 PagePlan；输出原子替换的 PDF 与计数 dict。
任何合成或写入失败均抛出异常，由 runner 记录脱敏日志并降级交付单语译本。
"""

from dataclasses import dataclass
from collections import Counter
import json
import logging
import math
import os
import tempfile
from typing import TYPE_CHECKING

import pymupdf as fitz

if TYPE_CHECKING:
    from ztf_split_plan import PagePlan


MARGIN = 12.0


def _log(event, reason, **fields):
    """只接受代码内固定原因；不记录第三方异常消息、路径或正文。"""
    logging.getLogger(__name__).warning(json.dumps({
        "event": event, "reason": reason, "count": 1, **fields}))


def _close(document):
    """清理失败独立记录，不覆盖合成失败，也不阻断其余资源清理。"""
    try:
        document.close()
    except Exception as error:
        _log("split_compose_cleanup_error", "close_failed", error_type=type(error).__name__)


@dataclass(frozen=True)
class RegionPlacement:
    """S4 扩展点：区域在 PyMuPDF 左上原点坐标中的实际嵌入变换。

    src/out 为从 0 开始的源页/输出页号；input 为 source 或 mono；k 为
    en/zh/span/hf；source_box 为扩展后的实际裁框，output_box 为落点；
    matrix 为源页坐标到输出页坐标的六元仿射变换，可直接交给 fitz.Matrix。
    """

    src: int
    out: int
    part: str
    input: str
    k: str
    source_box: tuple[float, float, float, float]
    output_box: tuple[float, float, float, float]
    matrix: tuple[float, float, float, float, float, float]


def _text_lines(page):
    """稳定的字符序号用于精确分配；不把 MuPDF 的整块当作不可分割文字。"""
    lines = []
    offset = 0
    for block_index, block in enumerate(page.get_text(
            "rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]):
        for line in block.get("lines", []):
            chars = []
            for span in line["spans"]:
                for char in span["chars"]:
                    chars.append((offset, char["c"], fitz.Rect(char["bbox"])))
                    offset += 1
            if chars:
                lines.append((block_index, fitz.Rect(line["bbox"]), chars))
    return lines


def _subtract_rect(box, keep):
    """从涂黑框扣除保留区，返回互不重叠的至多四块。"""
    hit = box & keep
    if hit.is_empty:
        return [box]
    return [r for r in (fitz.Rect(box.x0, box.y0, box.x1, hit.y0),
                        fitz.Rect(box.x0, hit.y1, box.x1, box.y1),
                        fitz.Rect(box.x0, hit.y0, hit.x0, hit.y1),
                        fitz.Rect(hit.x1, hit.y0, box.x1, hit.y1)) if not r.is_empty]


def _embed(output_page, document, index, box, destination, selected=None):
    """复制单页并删除框外文字，再按原字号嵌入；临时文档总是关闭。"""
    temporary = fitz.open()
    try:
        temporary.insert_pdf(document, from_page=index, to_page=index,
                             links=False, annots=False, widgets=False)
        page = temporary[0]
        rotation = page.rotation
        page.set_rotation(0)
        bounds = page.rect
        outside_boxes = (
            (bounds.x0, bounds.y0, bounds.x1, box.y0),
            (bounds.x0, box.y1, bounds.x1, bounds.y1),
            (bounds.x0, box.y0, box.x0, box.y1),
            (box.x1, box.y0, bounds.x1, box.y1),
        )
        if selected is not None:
            # 按连续字符段涂黑，先扣除保留字符框；支持混合字号与倾斜图中文字。
            # 后面的守恒检查明确拒绝无法隔离的真实重叠。
            lines = _text_lines(page)
            chars = [c for _, _, group in lines for c in group]
            expected = Counter(c for i, c, _ in chars if i in selected and not c.isspace())
            outside_boxes = [fitz.Rect(r) for r in outside_boxes if not fitz.Rect(r).is_empty]
            protected = []
            for _, _, group in lines:
                run = fitz.Rect()
                kept = fitz.Rect()
                for i, _, rect in [*group, (None, "", fitz.Rect())]:
                    remove = (i is not None and i not in selected and not rect.is_empty
                              and rect.intersects(box))
                    if not run.is_empty and not remove:
                        outside_boxes.append(run + (0.001, 0.001, -0.001, -0.001))
                        run = fitz.Rect()
                    if remove:
                        run |= rect
                    elif i in selected:
                        kept |= rect
                if not kept.is_empty:
                    protected.append(kept + (-0.001, -0.001, 0.001, 0.001))
            safe_boxes = []
            for rect in outside_boxes:
                pieces = [rect]
                for keep in protected:
                    pieces = [part for piece in pieces for part in _subtract_rect(piece, keep)]
                safe_boxes.extend(pieces)
            outside_boxes = safe_boxes
        for outside in outside_boxes:
            rect = fitz.Rect(outside)
            if not rect.is_empty:
                page.add_redact_annot(rect, fill=False, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        if selected is not None:
            actual = Counter(c for _, _, group in _text_lines(page)
                             for _, c, _ in group if not c.isspace())
            if actual != expected:
                _log("split_compose_text_error", "redaction_text_mismatch", source_index=index)
                raise ValueError("redaction_text_mismatch")
            if any(not box.contains(rect) for _, _, group in _text_lines(page)
                   for _, c, rect in group if not c.isspace()):
                _log("split_compose_text_error", "text_outside_clip", source_index=index)
                raise ValueError("text_outside_clip")
        output_page.show_pdf_page(destination, temporary, 0, clip=box,
                                  keep_proportion=True, overlay=True, rotate=-rotation)
    finally:
        _close(temporary)


def _compose_pages(source, mono, plans, output):
    """填充输出文档并返回区域变换；S4 在此后、保存前重建导航。"""
    placements = []
    for plan in plans:
        bounds = source[plan.source_index].rect
        if plan.columns == 0:
            page = output.new_page(width=2 * bounds.width, height=bounds.height)
            for document, kind, x in ((source, "en", 0), (mono, "zh", bounds.width)):
                input_page = document[plan.source_index]
                _place(page, document, plan, 0, input_page.rect * input_page.derotation_matrix,
                       fitz.Rect(x, 0, x + bounds.width, bounds.height), kind, placements)
            logging.getLogger(__name__).warning(json.dumps({
                "event": "split_compose_fallback", "reason": "page_plan", "count": 1,
                "source_index": plan.source_index}))
            continue
        source_page, mono_page = source[plan.source_index], mono[plan.source_index]
        headers, _, source_columns = _partition_content(source_page, plan, False)
        _, spans, mono_columns = _partition_content(mono_page, plan, True)
        # 两套裁框独立：原文只扣源计划中的跨栏带，译文扣扩展、合并后的跨栏带。
        # 不以译文的高度涂黑原文；输出发生碰撞时下移栏内容，保持原字号。
        columns = {"en": source_columns, "zh": mono_columns}
        column_bounds = {}
        for regions in columns.values():
            for region, box, _ in regions:
                column_bounds[region.column] = column_bounds.get(region.column, fitz.Rect(box)) | box
        width = max(bounds.width, 2 * max(b.width for b in column_bounds.values()) + 3 * MARGIN)
        for column in range(plan.columns):
            page = output.new_page(width=width, height=bounds.height)
            page_spans = [(r, b, ids) for r, b, ids in spans if (
                (r.position != "bottom" and column == 0)
                or (r.position == "bottom" and column == plan.columns - 1))]
            for region, box, selected in headers + page_spans:
                document = source if region.kind == "header_footer" else mono
                dest = fitz.Rect((width - box.width) / 2, box.y0,
                                 (width + box.width) / 2, box.y1)
                _place(page, document, plan, column, box, dest,
                       "hf" if region.kind == "header_footer" else "span", placements, selected)
            obstacles = _merge_intervals([(b.y0, b.y1) for _, b, _ in headers + page_spans])
            for document, kind in ((source, "en"), (mono, "zh")):
                middle = _merge_intervals(
                    [(r.box[1], r.box[3]) for r in plan.regions
                     if r.kind == "spanning" and r.position == "middle"] if kind == "en" else
                    [(b.y0, b.y1) for r, b, _ in spans if r.position == "middle"])
                regions = sorted([(r, b, ids) for r, b, ids in columns[kind] if r.column == column],
                                 key=lambda item: item[1].y0)
                down = 0
                previous_end = 0
                for region, box, selected in regions:
                    frame = column_bounds[column]
                    x = (width / 2 - MARGIN / 2 - frame.width if kind == "en"
                         else width / 2 + MARGIN / 2) + box.x0 - frame.x0
                    shift = sum(end - start for start, end in middle if end <= box.y0) if column == 1 else 0
                    y = max(box.y0 - shift + down, previous_end)
                    for start, end in obstacles:
                        if y < end and start < y + box.height:
                            y = end
                    down = y - (box.y0 - shift)
                    previous_end = y + box.height
                    if previous_end > bounds.height + 0.01:
                        _log("split_compose_collision", "column_layout_overflow", source_index=plan.source_index)
                        raise ValueError("column_layout_overflow")
                    dest = fitz.Rect(x, y, x + box.width, previous_end)
                    _place(page, document, plan, column, box, dest, kind, placements, selected)
    return placements


def _partition_content(page, plan, translated):
    """按行认栏、按字符唯一分配；只有本行带的邻栏内容才构成边界。

    跨栏块的延续行可扩框，但页眉页脚是屏障，已有栏正文不被跨栏吸入。
    归属与裁框分开：裁框覆盖邻区空白时，也不能把邻区文字嵌入第二遍。
    """
    lines = _text_lines(page)
    headers = [r for r in plan.regions if r.kind == "header_footer"]
    spans = [r for r in plan.regions if r.kind == "spanning"]
    columns = [r for r in plan.regions if r.kind == "column"]
    frames = [fitz.Rect() for _ in range(plan.columns)]
    for r in columns:
        frames[r.column] |= fitz.Rect(r.box)
    assigned = {id(r): [] for r in headers + spans + columns}

    def vertical(box, r):
        return max(0, min(box.y1, r.box[3]) - max(box.y0, r.box[1]))

    def header_at(box):
        # 使用字符中心，不因同一文字块中相邻行的 bbox 相交而吸入页眉。
        y = (box.y0 + box.y1) / 2
        return next((r for r in headers if r.box[1] <= y <= r.box[3]), None)

    def column_at(box):
        overlaps = [max(0, min(box.x1, f.x1) - max(box.x0, f.x0)) for f in frames]
        return max(range(plan.columns), key=lambda c: overlaps[c])

    seeds = {}
    column_seeds = {}
    for block, box, chars in lines:
        if header_at(box):
            continue
        hits = [r for r in spans if vertical(box, r) > 0]
        if hits:
            seeds.setdefault(block, []).extend(hits)
        else:
            column = column_at(box)
            column_seeds.setdefault((block, column), []).extend(
                r for r in columns if r.column == column and vertical(box, r) > 0)

    column_lines = []
    for block, box, chars in lines:
        column = column_at(box)
        candidates = [r for r in columns if r.column == column and vertical(box, r) > 0]
        direct = [r for r in spans if vertical(box, r) > 0]
        if not candidates and not direct:
            # 同块译文比源栏框更高时，保留其延续行；不能跨越已规划的独立带。
            candidates = [r for r in column_seeds.get((block, column), [])
                          if not any(min(box.y0, r.box[1]) < b.box[3]
                                     and b.box[1] < max(box.y1, r.box[3])
                                     for b in headers + spans)]
        # 跨栏延续不能越过页眉；靠栏起点的独立正文保持栏归属。
        continuation = []
        for r in seeds.get(block, []):
            between = (min(box.y0, r.box[1]), max(box.y1, r.box[3]))
            if any(between[0] < h.box[3] and h.box[1] < between[1] for h in headers):
                continue
            if candidates and abs(box.x0 - frames[column].x0) <= MARGIN:
                continue
            continuation.append(r)
        span = min(direct or continuation,
                   key=lambda r: abs((box.y0 + box.y1) - (r.box[1] + r.box[3])), default=None)
        body_chars = []
        for char in chars:
            header = header_at(char[2])
            if header:
                assigned[id(header)].append(char)
            elif span:
                # source 的跨栏原文只排除；mono 中只嵌入一次。
                if translated:
                    assigned[id(span)].append(char)
            elif candidates:
                owner = max(candidates, key=lambda r: (vertical(box, r),
                            -abs((box.y0 + box.y1) - (r.box[1] + r.box[3]))))
                assigned[id(owner)].append(char)
                body_chars.append(char)
        if body_chars:
            column_lines.append((column, box))

    # 横向限制仅来自本行带的另一栏；真实碰撞必须明确失败，不能截字。
    for i, (column, box) in enumerate(column_lines):
        for other, peer in column_lines[i + 1:]:
            if column != other and box.intersects(peer):
                _log("split_compose_collision", "column_text_overlap", source_index=plan.source_index)
                raise ValueError("column_text_overlap")

    def content(r):
        selected = assigned[id(r)]
        result = fitz.Rect(r.box)
        for _, _, bbox in selected:
            result |= bbox
        if not page.rect.contains(result):
            _log("split_compose_text_error", "region_outside_page", source_index=plan.source_index)
            raise ValueError("region_outside_page")
        return r, result, {i for i, _, _ in selected}

    header_content = [content(r) for r in headers]
    span_content = []
    for item in sorted([content(r) for r in spans] if translated else [],
                       key=lambda item: item[1].y0):
        r = item[0]
        if span_content and item[1].y0 <= span_content[-1][1].y1:
            previous, previous_box, previous_ids = span_content.pop()
            position = ("top" if "top" in {previous.position, r.position} else
                        "bottom" if previous.position == r.position == "bottom" else "middle")
            union = previous_box | item[1]
            item = (type(r)("spanning", tuple(union), position), union, previous_ids | item[2])
        span_content.append(item)
    for _, box, _ in span_content:
        if any(box.intersects(hbox) for _, hbox, _ in header_content):
            _log("split_compose_collision", "span_header_overlap", source_index=plan.source_index)
            raise ValueError("span_header_overlap")

    bands = _merge_intervals([(b.y0, b.y1) for _, b, _ in header_content + span_content]
                             if translated else [(r.box[1], r.box[3]) for r in headers + spans])
    column_content = []
    for r in columns:
        for piece in _subtract_bands(fitz.Rect(r.box), bands):
            # 按字符中心唯一归属；行高超出计划时扩展片段，不能在带边缘丢字。
            selected = [c for c in assigned[id(r)]
                        if piece.y0 <= (c[2].y0 + c[2].y1) / 2 <= piece.y1]
            ids = {c[0] for c in selected}
            for _, _, bbox in selected:
                piece |= bbox
            column_content.append((r, piece, ids))
        placed = set().union(*(ids for owner, _, ids in column_content if owner is r))
        missing = [c for c in assigned[id(r)] if c[0] not in placed]
        if missing:
            # 溢入跨栏带的译文也保留，放置阶段再局部下移避让。
            box = fitz.Rect(missing[0][2])
            for _, _, bbox in missing:
                box |= bbox
            column_content.append((r, box, {c[0] for c in missing}))
    return header_content, span_content, column_content


def _merge_intervals(intervals):
    """收拢时按跨栏带的并集扣空白，重叠部分只计一次。"""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _subtract_bands(box, bands):
    """仅切分纵向范围，保持栏内原坐标和字号。"""
    cursor = box.y0
    pieces = []
    for start, end in bands:
        if start > cursor:
            piece = fitz.Rect(box.x0, cursor, box.x1, min(start, box.y1))
            if not piece.is_empty:
                pieces.append(piece)
        cursor = max(cursor, end)
    if cursor < box.y1:
        pieces.append(fitz.Rect(box.x0, cursor, box.x1, box.y1))
    return pieces


def _place(page, document, plan, column, box, destination, kind, placements, selected=None):
    """每次真实嵌入记录变换，S4 可据此重建链接与跳转。"""
    _embed(page, document, plan.source_index, box, destination, selected)
    matrix = document[plan.source_index].rotation_matrix
    rotated_box = box * matrix
    matrix *= fitz.Matrix(1, 0, 0, 1, destination.x0 - rotated_box.x0,
                          destination.y0 - rotated_box.y0)
    placements.append(RegionPlacement(
        plan.source_index, page.number, "ab"[column],
        "source" if kind in {"en", "hf"} else "mono", kind,
        tuple(box), tuple(destination),
        tuple(matrix)))


def _write_layout(output, plans, placements):
    """冻结的 v1 信息字典，r 使用 PDF 用户空间的左下原点坐标。"""
    pages = []
    for page in output:
        items = [p for p in placements if p.out == page.number]
        for i, item in enumerate(items):
            for peer in items[i + 1:]:
                if item.k != peer.k and fitz.Rect(item.output_box).intersects(fitz.Rect(peer.output_box)):
                    _log("split_compose_collision", "metadata_region_overlap", source_index=item.src)
                    raise ValueError("metadata_region_overlap")
        pages.append({"src": items[0].src, "part": items[0].part, "regions": [
            {"k": p.k, "r": list(fitz.Rect(p.output_box) * ~page.transformation_matrix)}
            for p in items]})
        if any(plan.source_index == items[0].src and plan.columns == 0 for plan in plans):
            pages[-1]["fallback"] = True
    output.set_metadata({"producer": "BabelDOC for Zotero"})
    _, reference = output.xref_get_key(-1, "Info")
    output.xref_set_key(int(reference.split()[0]), "ZTFSplitLayout",
                        fitz.get_pdf_str(json.dumps({"v": 1, "pages": pages}, separators=(",", ":"))))


def _validate_inputs(source, mono, plans):
    """冻结接口应完整描述所选页；拒绝无效几何，避免悄悄生成部分文档。"""
    if not source.is_pdf or not mono.is_pdf or len(source) != len(mono) or not plans:
        raise ValueError("invalid_documents")
    seen = set()
    for plan in plans:
        if (type(plan.source_index) is not int or plan.source_index in seen
                or not 0 <= plan.source_index < len(source) or plan.columns not in {0, 1, 2}):
            raise ValueError("invalid_page_plan")
        seen.add(plan.source_index)
        # 在构造任何计划 Rect 或使用其坐标前检查所有框，包括回退计划。
        for region in plan.regions:
            box = region.box
            try:
                valid = (isinstance(box, (tuple, list)) and len(box) == 4
                         and all(type(v) in {int, float} and math.isfinite(v) for v in box))
            except OverflowError:
                valid = False
            # S2 尺寸未知的回退哨兵不参与任何坐标运算，继续从真实页取尺寸。
            sentinel = (plan.columns == 0 and region.kind == "fallback"
                        and valid and tuple(box) == (0, 0, 0, 0))
            if not valid or (not sentinel and (box[0] >= box[2] or box[1] >= box[3])):
                _log("split_compose_invalid_plan", "invalid_region_box", source_index=plan.source_index)
                raise ValueError("invalid_region_box")
    # 先完成全部计划的原始框检查，后续才读取页面几何或构造 Rect。
    for plan in plans:
        a, b = source[plan.source_index], mono[plan.source_index]
        if (not all(math.isfinite(v) for v in (*a.rect, *b.rect))
                or a.rect.is_empty or b.rect.is_empty
                or any(abs(x - y) > 0.01 for x, y in zip(a.rect, b.rect))):
            raise ValueError("page_geometry_mismatch")
        for region in plan.regions:
            box = region.box
            if (box[2] > box[0] and box[3] > box[1]
                    and not fitz.Rect(box).intersects(a.rect)):
                _log("split_compose_invalid_plan", "invalid_region_box", source_index=plan.source_index)
                raise ValueError("region_outside_page")
        if plan.columns:
            if a.rotation or b.rotation:
                raise ValueError("rotation_requires_fallback")
            if {r.column for r in plan.regions if r.kind == "column"} != set(range(plan.columns)):
                raise ValueError("missing_column")
            for region in plan.regions:
                if region.kind not in {"header_footer", "spanning", "column"}:
                    raise ValueError("invalid_region_kind")
                if region.kind == "spanning" and region.position not in {"top", "middle", "bottom"}:
                    raise ValueError("invalid_span_position")


def compose_split_document(source_pdf: str, mono_pdf: str, plans: "list[PagePlan]",
                           output_pdf: str) -> dict:
    """返回 input_pages、output_pages、regions、fallback_pages（均为整数）。

    失败抛 RuntimeError("split_compose_failed")，日志只含固定原因与异常类型。
    原输出在替换成功之前保持完整；所有输入及临时 Document 在 finally 关闭。
    """
    source = mono = output = None
    temporary_path = None
    try:
        source = fitz.open(source_pdf)
        mono = fitz.open(mono_pdf)
        output = fitz.open()
        _validate_inputs(source, mono, plans)
        placements = _compose_pages(source, mono, plans, output)
        _write_layout(output, plans, placements)
        handle = tempfile.NamedTemporaryFile(prefix=".ztf-split-", suffix=".pdf",
                                             dir=os.path.dirname(os.path.abspath(output_pdf)),
                                             delete=False)
        temporary_path = handle.name
        handle.close()
        output.save(temporary_path, garbage=4, deflate=True)
        output.close()
        output = None
        os.replace(temporary_path, output_pdf)
        temporary_path = None
        return {"input_pages": len(plans), "output_pages": len({p.out for p in placements}),
                "regions": len(placements),
                "fallback_pages": sum(plan.columns == 0 for plan in plans)}
    except Exception as error:
        # 不把第三方异常中的路径、正文或凭据带进 runner 日志。
        logging.getLogger(__name__).warning(json.dumps({
            "event": "split_compose_error", "reason": "composition_failed", "count": 1,
            "error_type": type(error).__name__}))
        raise RuntimeError("split_compose_failed") from None
    finally:
        for document in (output, mono, source):
            if document is not None:
                _close(document)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except Exception as error:
                _log("split_compose_cleanup_error", "unlink_failed", error_type=type(error).__name__)
