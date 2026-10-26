"""离线合成分栏双语 PDF，不发起网络请求。

输入源 PDF、同页单语 PDF 和冻结的 PagePlan；输出原子替换的 PDF 与计数 dict。
栏排版超出页面时，仅该源页回退整页并排并记录计数；其他合成或写入失败
抛出异常，由 runner 记录脱敏日志并降级交付单语译本。
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


class _ColumnLayoutOverflow(ValueError):
    """可局部恢复的栏高度溢出，与第三方嵌入或写入异常分开处理。"""


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
    """逐源页合成；局部回退同步更新内部计划，供元数据、导航与计数使用。"""
    placements = []
    for index, plan in enumerate(plans):
        bounds = source[plan.source_index].rect
        reason = "page_plan"
        if plan.columns:
            first_page, first_placement = len(output), len(placements)
            try:
                _compose_columns(source, mono, plan, output, placements)
                continue
            except _ColumnLayoutOverflow:
                # A、B 页中任何一页溢出，均撤销该源页的全部输出和区域变换。
                output.delete_pages(from_page=first_page, to_page=len(output) - 1)
                del placements[first_placement:]
                plan = type(plan)(plan.source_index, 0, (
                    type(plan.regions[0])("fallback", tuple(bounds)),))
                plans[index] = plan
                reason = "column_layout_overflow"
        if plan.columns == 0:
            page = output.new_page(width=2 * bounds.width, height=bounds.height)
            for document, kind, x in ((source, "en", 0), (mono, "zh", bounds.width)):
                input_page = document[plan.source_index]
                _place(page, document, plan, 0, input_page.rect * input_page.derotation_matrix,
                       fitz.Rect(x, 0, x + bounds.width, bounds.height), kind, placements)
            _log("split_compose_fallback", reason, source_index=plan.source_index)
    return placements


def _compose_columns(source, mono, plan, output, placements):
    """合成一个源页的各栏；高度溢出交由逐页调度恢复。"""
    bounds = source[plan.source_index].rect
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
                    raise _ColumnLayoutOverflow("column_layout_overflow")
                dest = fitz.Rect(x, y, x + box.width, previous_end)
                _place(page, document, plan, column, box, dest, kind, placements, selected)


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


def _navigation_values(value, size):
    """第三方坐标先检查有限性，再交给 Point / Rect 做运算。"""
    try:
        values = tuple(value)
        return len(values) == size and all(math.isfinite(v) for v in values)
    except (TypeError, ValueError, OverflowError):
        return False


def _navigation_index(placements):
    """按输入文档、源页建立放置索引；hf 只属于原文，span 只属于译文。"""
    index = {}
    for placement in placements:
        index.setdefault((placement.input, placement.src), []).append(placement)
    return index


def _navigation_target(index, source_index, point, side):
    """返回 (输出页、点、回退原因)；无对应页时返回 (None, None, 原因)。

    point 是相应输入文档的未旋转坐标；英文不借用中文跨栏映射。
    页眉页脚或空白落点以最近同侧区域选择 A/B，跳到该侧的页顶。
    """
    items = index.get(("source" if side == "en" else "mono", source_index), [])
    body = [p for p in items if p.k in {side, "span"}]
    if not body:
        return None, None, "target_page_missing"
    valid = _navigation_values(point, 2)
    header = valid and any(p.source_box[1] <= point[1] <= p.source_box[3]
                           for p in index.get(("source", source_index), []) if p.k == "hf")
    if valid:
        point = fitz.Point(point)
        if not header:
            # 扩框可能覆盖邻区空白；中文跨栏优先于普通栏。
            hit = next((p for p in sorted(body, key=lambda p: p.k != "span")
                        if fitz.Rect(p.source_box).contains(point)), None)
            if hit is not None:
                return hit.out, point * fitz.Matrix(hit.matrix), ""

        def distance(p):
            x0, y0, x1, y1 = p.source_box
            return max(x0 - point.x, 0, point.x - x1) ** 2 + max(y0 - point.y, 0, point.y - y1) ** 2

        # hf 在 A、B 重复，选择目标 x 所在的正文栏，不固定跳 A。
        candidates = [p for p in body if p.k == side] if header else body
        chosen = min(candidates or body, key=lambda p: (distance(p), p.out))
    else:
        chosen = min(body, key=lambda p: p.out)
    # span 仅参与选择目标页；它的左边缘不代表任何语言侧的页顶。
    same_side = [p for p in items if p.out == chosen.out and p.k == side]
    if not same_side:
        # 整页跨栏没有正文侧时，落到跨栏元素本身，而非伪造语言侧页顶。
        return chosen.out, fitz.Point(chosen.output_box[:2]), "target_unmapped"
    return chosen.out, fitz.Point(min(p.output_box[0] for p in same_side), 0), (
        "target_header_footer" if header else "target_unmapped")


def _navigation_zoom(zoom):
    """无效缩放回退为 0，原因固定且独立于目标位置回退。"""
    try:
        if isinstance(zoom, (int, float)) and math.isfinite(zoom):
            return zoom, ""
    except OverflowError:
        pass
    return 0, "invalid_zoom"


def _navigation_destination(document, link, index, side):
    """返回目标、位置回退原因、缩放回退原因；两种回退分别计数。"""
    kind = link.get("kind")
    if kind == fitz.LINK_URI:
        uri = link.get("uri")
        return ({"kind": kind, "uri": uri}, "", "") if uri else (None, "empty_uri", "")
    if kind not in {fitz.LINK_GOTO, fitz.LINK_NAMED}:
        return None, "unsupported_link_kind", ""
    page, point = link.get("page", -1), link.get("to")
    name = link.get("nameddest") or (point if isinstance(point, str) else None)
    if name:
        # resolve_link 与 get_links 一样返回可视坐标。
        page, x, y = document.resolve_link("#nameddest=" + name)
        point = (x, y)
    if type(page) is not int or not 0 <= page < len(document):
        return None, "target_page_missing", ""
    if _navigation_values(point, 2):
        point = fitz.Point(point) * document[page].derotation_matrix
    target_page, target_point, reason = _navigation_target(index, page, point, side)
    if target_page is None:
        return None, reason, ""
    zoom, zoom_reason = _navigation_zoom(link.get("zoom", 0))
    return {"kind": fitz.LINK_GOTO, "page": target_page, "to": target_point, "zoom": zoom}, reason, zoom_reason


def _insert_navigation_link(output, page_index, link):
    """用公开接口插入；URI 单独编码，避免 1.28.2 直接拼接括号造成改值。"""
    page = output[page_index]
    if link["kind"] != fitz.LINK_URI:
        page.insert_link(link)
        return
    before = {xref for xref, _, _ in page.annot_xrefs()}
    page.insert_link({**link, "uri": "https://example.invalid/"})
    added = [xref for xref, _, _ in page.annot_xrefs() if xref not in before]
    if len(added) != 1:
        raise ValueError("link_insertion_failed")
    output.xref_set_key(added[0], "A/URI", fitz.get_pdf_str(link["uri"]))


def _rebuild_split_links(source, mono, output, index):
    """返回输入链接数、写入注释数、完全未写入的输入链接数、页顶回退数。

    输入数包含 source、mono 两份；跨栏原文/mono 页眉没有可见区域，
    也计未写入并记录。单个链接可裁成多个注释，hf 可复制到 A、B。
    """
    stats = {"links_source": 0, "links_written": 0, "links_unresolved": 0,
             "links_fallback": 0, "links_zoom_fallback": 0}
    for document, side, input_name in ((source, "en", "source"), (mono, "zh", "mono")):
        for source_index in range(len(document)):
            page = document[source_index]
            for number, link in enumerate(page.get_links()):
                stats["links_source"] += 1
                fields = {"source_index": source_index, "side": side, "link_index": number}
                reason = "anchor_unmapped"
                pieces = []
                if _navigation_values(link.get("from"), 4):
                    box = fitz.Rect(link["from"]) * page.derotation_matrix
                    for placement in index.get((input_name, source_index), []):
                        clipped = box & fitz.Rect(placement.source_box)
                        if not clipped.is_empty:
                            pieces.append((placement, clipped))
                else:
                    reason = "invalid_link_box"
                if pieces:
                    destination, reason, zoom_reason = _navigation_destination(document, link, index, side)
                    if destination is not None:
                        if zoom_reason:
                            stats["links_zoom_fallback"] += 1
                            _log("split_navigation_fallback", zoom_reason, **fields)
                        if reason:
                            stats["links_fallback"] += 1
                            _log("split_navigation_fallback", reason, **fields)
                        for placement, clipped in pieces:
                            rectangle = clipped * fitz.Matrix(placement.matrix)
                            _insert_navigation_link(output, placement.out, {**destination, "from": rectangle})
                            stats["links_written"] += 1
                        continue
                stats["links_unresolved"] += 1
                _log("split_navigation_unresolved", reason, **fields)
    return stats


def _rebuild_split_toc(source, output, index):
    """保留源大纲层级；内部目标按英文侧映射，失败保留无动作条目。"""
    toc = []
    external = []
    stats = {"toc_entries": 0, "toc_unresolved": 0, "toc_zoom_fallback": 0}
    for number, (level, title, page, destination) in enumerate(source.get_toc(simple=False)):
        destination = dict(destination)
        destination.pop("xref", None)
        if destination.get("kind") in {fitz.LINK_GOTO, fitz.LINK_NAMED}:
            mapped, reason, zoom_reason = _navigation_destination(source, destination, index, "en")
            if zoom_reason:
                stats["toc_zoom_fallback"] += 1
                _log("split_navigation_fallback", zoom_reason, toc_index=number, side="en")
            if reason:
                _log("split_navigation_fallback" if mapped else "split_navigation_unresolved",
                     reason, toc_index=number, side="en")
            if mapped:
                destination = {**destination, **mapped}
                destination.pop("nameddest", None)
                page = mapped["page"] + 1
            else:
                stats["toc_unresolved"] += 1
                destination = {"kind": fitz.LINK_NONE}
                page = -1
        elif destination.get("kind") == fitz.LINK_NONE:
            page = -1
        else:
            # 1.28.2 set_toc 的 tuple 与 GOTOR 编码要求的 Point 不兼容；
            # get_toc 对远程命名目标和 LAUNCH 的 kind 也不能可靠往返。
            # 文件类及未知动作明确停用，保留标题/层级；URI 独立写入。
            if destination.get("kind") == fitz.LINK_URI:
                external.append((number, destination))
            else:
                stats["toc_unresolved"] += 1
                _log("split_navigation_unresolved", "unsupported_toc_action",
                     toc_index=number, side="en")
            destination = {"kind": fitz.LINK_NONE}
            page = -1
        toc.append([level, title, page, destination])
    if toc:
        output.set_toc(toc, collapse=0)
    for number, destination in external:
        try:
            output.set_toc_item(number, dest_dict=destination)
        except Exception as error:
            stats["toc_unresolved"] += 1
            _log("split_navigation_unresolved", "toc_action_failed", toc_index=number,
                 side="en", error_type=type(error).__name__)
    stats["toc_entries"] = len(toc)
    return stats


def _pdf_label_string(text):
    """ASCII 按 PDF 字面量转义；非 ASCII 一律使用带 BOM 的 UTF-16BE。"""
    if not text.isascii():
        return "<feff" + text.encode("utf-16-be").hex() + ">"
    return "(" + "".join(
        "\\" + char if char in "()\\" else
        f"\\{ord(char):03o}" if ord(char) < 32 or ord(char) == 127 else char
        for char in text) + ")"


def _source_label_rules(source):
    """读取完整数字树，保留规则对象；不经过丢失字符串转义的 get_label。"""
    pdf = fitz.mupdf
    document = pdf.PdfDocument(source.this)
    catalog = pdf.pdf_load_object(document, source.pdf_catalog())
    root = pdf.pdf_dict_gets(catalog, "PageLabels")
    if pdf.pdf_is_null(root):
        return []  # 缺省页序号是正常情况，不属于读取失败。
    pending, visited, rules = [root], set(), {}
    while pending:
        node = pending.pop()
        if pdf.pdf_is_indirect(node):
            xref = pdf.pdf_to_num(node)
            if xref in visited:
                raise ValueError("invalid_label_tree")
            visited.add(xref)
        if not pdf.pdf_is_dict(node):
            raise ValueError("invalid_label_tree")
        nums, kids = (pdf.pdf_dict_gets(node, key) for key in ("Nums", "Kids"))
        if not pdf.pdf_is_null(nums):
            if not pdf.pdf_is_null(kids) or not pdf.pdf_is_array(nums) or pdf.pdf_array_len(nums) % 2:
                raise ValueError("invalid_label_tree")
            for offset in range(0, pdf.pdf_array_len(nums), 2):
                key = pdf.pdf_array_get(nums, offset)
                if not pdf.pdf_is_int(key):
                    raise ValueError("invalid_label_tree")
                start = pdf.pdf_to_int(key)
                if start < 0 or start in rules:
                    raise ValueError("invalid_label_tree")
                rules[start] = pdf.pdf_array_get(nums, offset + 1)
        elif pdf.pdf_is_array(kids) and pdf.pdf_array_len(kids):
            pending.extend(pdf.pdf_array_get(kids, i) for i in range(pdf.pdf_array_len(kids)))
        else:
            raise ValueError("invalid_label_tree")
    if 0 not in rules:
        raise ValueError("invalid_label_tree")
    return sorted(rules.items())


def _source_page_label(source, rules, number):
    """按 PDF 标签规则展开 /S、/P、/St，字母编号为 A…Z、AA、BB…。"""
    if not rules:
        return str(number + 1)
    start, rule = next(item for item in reversed(rules) if item[0] <= number)
    pdf = fitz.mupdf
    if not pdf.pdf_is_dict(rule):
        raise ValueError("invalid_label_rule")
    prefix_obj, style_obj, first_obj = (pdf.pdf_dict_gets(rule, key) for key in ("P", "S", "St"))
    prefix, style, first = "", "", 1
    if not pdf.pdf_is_null(prefix_obj):
        if not pdf.pdf_is_string(prefix_obj):
            raise ValueError("invalid_label_prefix")
        # 与 1.28.2 xref_get_key 的 string 分支使用同一 PDF 字符串解码器。
        # 直接规则没有 xref；底层解码同时支持字面量、八进制、十六进制及 UTF-16BE。
        prefix = pdf.pdf_to_text_string(prefix_obj)
    if not pdf.pdf_is_null(style_obj):
        if not pdf.pdf_is_name(style_obj):
            raise ValueError("invalid_label_style")
        style = pdf.pdf_to_name(style_obj)
        if style not in {"D", "R", "r", "A", "a"}:
            raise ValueError("invalid_label_style")
    if not pdf.pdf_is_null(first_obj):
        if not pdf.pdf_is_int(first_obj) or pdf.pdf_to_int(first_obj) < 1:
            raise ValueError("invalid_label_start")
        first = pdf.pdf_to_int(first_obj)
    value = first + number - start
    suffix = ""
    if style == "D":
        suffix = str(value)
    elif style in {"A", "a"}:
        base = ord("A" if style == "A" else "a")
        suffix = chr(base + (value - 1) % 26) * ((value - 1) // 26 + 1)
    elif style in {"R", "r"}:
        for size, roman in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                            (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                            (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
            count, value = divmod(value, size)
            suffix += roman * count
        if style == "r":
            suffix = suffix.lower()
    return prefix + suffix


def _write_split_labels(source, output, plans, placements):
    """逐页写固定标签；单项失败写空规则，避免继承相邻页的标签。"""
    columns = {plan.source_index: plan.columns for plan in plans}
    pages = {p.out: p for p in placements}
    nums = []
    stats = {"labels": 0, "labels_unresolved": 0, "labels_read_fallback": 0}
    tree_error = None
    try:
        rules = _source_label_rules(source)
    except Exception as error:
        rules, tree_error = [], type(error).__name__
    for number in range(len(output)):
        placement = pages[number]
        read_error = tree_error
        label = str(placement.src + 1)
        if read_error is None:
            try:
                label = _source_page_label(source, rules, placement.src) or label
            except Exception as error:
                read_error = type(error).__name__
        if read_error:
            stats["labels_read_fallback"] += 1
            stats["labels_unresolved"] += 1
            _log("split_navigation_fallback", "label_read_failed",
                 source_index=placement.src, output_index=number, error_type=read_error)
        try:
            if columns[placement.src] == 2:
                label += placement.part
            # 1.28.2 set_page_labels 不转义 /P；直接写数字树的独立规则对象。
            # 标签已展开为完整文本，与此前一样不设 /S，/St 默认 1。
            encoded = _pdf_label_string(label)
            xref = output.get_new_xref()
            output.update_object(xref, "<<>>")
            output.xref_set_key(xref, "P", encoded)
            # 底层接口按 PDF 字符串规则解码；get_label 的读取缺陷不影响写入。
            if output.xref_get_key(xref, "P") != ("string", label):
                raise ValueError("page_label_decode_mismatch")
            nums.append(f"{number} {xref} 0 R")
            stats["labels"] += 1
        except Exception as error:
            # 不挂入可能已部分写入的对象；空规则也覆盖首项与末项失败。
            nums.append(f"{number} <<>>")
            if not read_error:
                stats["labels_unresolved"] += 1
            _log("split_navigation_unresolved", "label_write_failed",
                 source_index=placement.src, output_index=number,
                 error_type=type(error).__name__)
    output.xref_set_key(output.pdf_catalog(), "PageLabels", "<</Nums [" + " ".join(nums) + "]>>")
    return stats


def _rebuild_split_navigation(source, mono, output, plans, placements):
    """保存前独立重建导航，不改变合成区域；异常由公开入口统一脱敏。"""
    index = _navigation_index(placements)
    stats = _rebuild_split_links(source, mono, output, index)
    stats.update(_rebuild_split_toc(source, output, index))
    stats.update(_write_split_labels(source, output, plans, placements))
    return stats


def compose_split_document(source_pdf: str, mono_pdf: str, plans: "list[PagePlan]",
                           output_pdf: str) -> dict:
    """返回 input_pages、output_pages、regions、fallback_pages（均为整数）。

    导航计数 links_source 含两份输入的全部链接；links_written 是输出注释数，
    links_unresolved 是完全未写入的输入链接数（含有意省略的区域），
    links_fallback 是目标回退页顶的输入链接数；toc_entries、labels 是条数。
    links_zoom_fallback、toc_zoom_fallback 为无效缩放回退数；toc_unresolved
    为仅保留标题和层级、未能写入动作的目录条数。
    labels_unresolved 为源标签读取或输出写入失败的输出页数；labels_read_fallback
    为读取失败后使用源页序号的输出页数。写入失败页使用空标签，不影响其余页。

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
        plans = list(plans)  # 局部回退只修改本次合成计划，不改变调用方输入。
        placements = _compose_pages(source, mono, plans, output)
        _write_layout(output, plans, placements)
        navigation = _rebuild_split_navigation(source, mono, output, plans, placements)
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
                "fallback_pages": sum(plan.columns == 0 for plan in plans), **navigation}
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
