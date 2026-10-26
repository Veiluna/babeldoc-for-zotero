"""将 runner 导出的版面 JSON 规划为分栏区域，不读取 PDF、不访问网络。

输入为单页 dict：index、width、height、可选 rotation（默认 0）、
paragraphs[{box, output_box, label, translated, marginal, author}]、layouts[{box, class}]。
box 是原文框，左上原点、单位 pt；可选 output_box 是单语译文排版框，
本模块只按 box 规划，output_box 留给合成模块处理。
rotation 非零时整页回退；无效值（非 JSON 数值、布尔值或非有限值）按
invalid_geometry 整页回退。marginal=true 的段落不参与正文聚类；与其相交的
文本版面框一并候选，完整纵向带与其他段落及独立 layouts 框均无重叠时才归为 header_footer，
否则按横向位置随栏放置（含与栏内图表重叠的版权/许可块）。
独立小型 abandon 版面框仅在全部正文上下边缘之外且不在正文栏框内时补作页眉页脚；
宽不超过页宽 10%、高不超过页高 2%，且整带不能有非 marginal 段落。
普通 text/title/fallback_line 框不能仅凭尺寸或边缘位置推断 marginal 身份。
缺少 marginal 或 marginal=false 的保留段落均按普通内容处理。
两栏页的跨栏种子只有栏间隙水平重叠宽度严格超过 min(4pt, 间隙宽 25%)
的元素，以及 author=true 的段落；0.027pt 这类擦边不算跨栏。author 块不参与
识栏聚类；旧数据缺少 author 键时视为 false，不按标签或同行外接框推断作者种子。
同行扩展仅从种子出发，不含正文、图表容器或 marginal，显式 author 块除外。
所有非 marginal、非种子的窄元素均参与识栏，窄指正文区水平包络宽度的 60% 以下；
正文优先确定栏边界，另一侧只有图表或公式时也可形成第二栏。
中心落在容器类 layouts 框内的段落由该框代表，不独立参与识栏、间隙推断或
跨栏种子及同行扩展；文本框不替换段落标签或 author/marginal 语义，含段落的
文本框不重复提供识栏证据，空文本框仍按普通元素处理。未知类别不代表段落。
全部原始框仍参与完整覆盖与区域边界检查。
跨栏带与非种子部分重叠超过该元素高度一半时完整吸收，否则收缩到不重叠；
无法同时保留完整种子与完整元素时整页回退，绝不裁切输入框。
完全位于间隙左侧或右侧的非跨栏元素随对应栏，允许超出正文水平包络。
输出为冻结的 PagePlan，Region/PagePlan/plan_page 接口保持不变。
不支持的版式或无效坐标整页回退，以结构化日志说明原因；不部分丢弃元素。
"""

from dataclasses import dataclass
import json
import logging
import math
from typing import Literal


# BabelDOC 0.6.4 il_translator_llm_only.py:268–272；runner:92 同值。
BODY_TEXT_LAYOUT_LABELS = frozenset({"text", "plain text", "paragraph_hybrid"})
# 以下来源均相对本机 BabelDOC 0.6.4 的 babeldoc/ 目录：
# docvision/doclayout.py:46–49 从 ONNX metadata["names"] 读取默认模型的全部 10 类：
# title/plain text/abandon/figure/figure_caption/table/table_caption/table_footnote/
# isolate_formula/formula_caption。缓存模型为 doclayout_yolo_docstructbench_imgsz1024.onnx。
# format/pdf/document_il/utils/layout_helper.py:659–729 列出扩展标签；其中
# :700–701 为 formula/formula_hybrid，:726–728 为 table/figure/image。
# format/pdf/document_il/midend/paragraph_finder.py:72–87 会将 formula 改标 isolate_formula。
# 只允许已核实的非文本容器代表内部段落；其余类别不能凭不在文本表中就当作容器。
_CONTAINER_LAYOUT_CLASSES = frozenset({
    "figure", "table", "image", "formula", "isolate_formula", "formula_hybrid",
})
# 文本类来源：format/pdf/document_il/utils/layout_helper.py:801–849 is_text_layout；
# 同文件 :660–664、:705、:719–723 的编号/参考文献/算法/公式题注也是文本。
# docvision/rpc_doclayout6.py:411 另有 list；author 是冻结输入的兼容类名（第 6 轮反例），
# 实际作者模型类为上述源码 :845 的 author_info_hybrid。
_TEXT_LAYOUT_CLASSES = frozenset({
    "plain text", "tiny text", "title", "abandon", "figure_caption", "table_caption",
    "table_text", "table_footnote", "paragraph_title", "abstract", "content",
    "figure_title", "table_title", "doc_title", "footnote", "header", "footer", "seal",
    "text", "chart_title", "paragraph", "table_cell", "figure_text", "list_item", "caption",
    "page_header", "page_footer", "wired_table_cell", "wireless_table_cell",
    "paragraph_hybrid", "table_cell_hybrid", "caption_hybrid", "unknown_hybrid",
    "figure_text_hybrid", "list_item_hybrid", "title_hybrid", "fallback_line",
    "author_info_hybrid", "page_header_hybrid", "page_footer_hybrid", "footnote_hybrid",
    "number", "reference", "reference_content", "algorithm", "formula_caption",
    "reference_hybrid", "code_algo_hybrid", "line_number_hybrid", "page_number_hybrid",
    "list", "author",
})
# class_name 并非闭合枚举：format/pdf/document_il/midend/layout_parser.py:163 直接复制
# 模型 names；:206 另生成 fallback_line；docvision/rpc_doclayout8.py:332–352 透传 RPC
# label。整页混合标签（layout_helper.py:706–714）及未知类保持普通元素，不推断容器身份。
_GUTTER_OVERLAP_MAX_PT = 4.0
_GUTTER_OVERLAP_FRACTION = 0.25


@dataclass(frozen=True)
class Region:
    """源页上的区域；仅 spanning 使用 position，column 使用从 0 开始的栏号。"""

    kind: Literal["header_footer", "spanning", "column", "fallback"]
    box: tuple[float, float, float, float]
    position: Literal["top", "middle", "bottom", "none"] = "none"
    column: int = -1


@dataclass(frozen=True)
class PagePlan:
    """单个源页的规划；columns=0 表示整页回退。"""

    source_index: int
    columns: int
    regions: tuple[Region, ...]


def _fallback(page, reason):
    """仅记录固定原因与页号，不记录版面内容。"""
    logging.getLogger(__name__).warning(json.dumps({
        "event": "split_plan_fallback", "reason": reason, "count": 1,
        "source_index": page["index"],
    }))
    return PagePlan(page["index"], 0, (
        Region("fallback", (0.0, 0.0, page["width"], page["height"])),))


def _number(value):
    """仅接受 JSON 数值；先核对有限性再做坐标运算。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("nonfinite_number")
    return value


def _safe_extent(value):
    """尺寸损坏时没有可推断的整页框，该轴用 0 表示未知。"""
    try:
        return max(0.0, _number(value))
    except (ValueError, OverflowError):
        return 0.0


def _validate_geometry(page):
    """检查全部元素（包括未知标签），不通过时不得只丢弃坏框。"""
    if _number(page["width"]) <= 0 or _number(page["height"]) <= 0:
        raise ValueError("invalid_size")
    _number(page.get("rotation", 0))
    for key in ("paragraphs", "layouts"):
        if not isinstance(page[key], list):
            raise ValueError("invalid_elements")
        for item in page[key]:
            box = item["box"]
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                raise ValueError("invalid_box")
            x0, y0, x1, y1 = map(_number, box)
            if x0 >= x1 or y0 >= y1:
                raise ValueError("empty_or_inverted_box")


def _bounds(boxes):
    """取非空矩形集合的外接框。"""
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _contains_center(outer, inner):
    """版面框按段落中心确定归属，不依赖段落标签或完整包含关系。"""
    x, y = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


def _cluster_body(body, width):
    """按左边缘聚类，容忍不超过页宽 4%（至少 12pt）的段首缩进。

    与每组最左锚点比较，避免多次小缩进串联吞掉真正的下一栏。
    """
    tolerance = max(12.0, width * 0.04)
    groups = []
    for box in sorted(body):
        if not groups or box[0] - groups[-1][0][0] > tolerance:
            groups.append([box])
        else:
            groups[-1].append(box)
    return groups


def _merge_x_connected(groups):
    """按水平范围把左缘分组合并为连通分量，用于识别同栏内的缩进段。

    分组已按左缘升序，扫描时只需与当前分量的最右边缘比较；仅相接
    而不重叠的分组仍视为不同栏，合并后组内框保持排序以便取左缘。
    """
    merged = []
    for group in groups:
        right = max(box[2] for box in group)
        if merged and group[0][0] < merged[-1][1]:
            merged[-1] = (sorted(merged[-1][0] + group), max(merged[-1][1], right))
        else:
            merged.append((list(group), right))
    return [boxes for boxes, _ in merged]


def _column_candidates(body):
    """先排除通栏正文，再统计栏数，通栏段落仍保留供跨栏带识别。

    两个水平分离且纵向共存的正文框互为栏同伴，并提供栏间隙证据。
    有同伴的正文框与另一栏正文并排，是栏正文，始终保留为栏锚点，
    即便它横跨同栏内窄块之间的伪间隙。无同伴的正文框横跨任一栏间隙
    即视为通栏正文，不充当栏锚点；不设外包络上限，超出栏边距的整页
    宽通栏框同样被排除。纵向共存约束避免把同栏不同行的短段当成两栏。
    """
    def beside(left, right):
        """left 位于 right 左侧且二者纵向共存。"""
        return (left[2] < right[0]
                and max(left[1], right[1]) < min(left[3], right[3]))

    gutters = {(left[2], right[0]) for left in body for right in body
               if beside(left, right)}
    return [box for box in body
            if any(beside(box, peer) or beside(peer, box) for peer in body)
            or not any(box[0] < start and end < box[2] for start, end in gutters)]


def _merge_bands(bands, gap=2.0):
    """合并重叠或相邻的纵向区间；跨栏默认间隔至多 2pt，页眉页脚用 0。"""
    merged = []
    for start, end in sorted(bands):
        if merged and start <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _row_member(label):
    """正文标签与图表容器以外的类别可补齐同行，例如作者段落或作者版面框。"""
    return (label not in BODY_TEXT_LAYOUT_LABELS
            and label not in _CONTAINER_LAYOUT_CLASSES)


def _hits_gutter(box, gutter):
    """超过绝对/相对容差的水平交集才是跨栏证据。"""
    tolerance = min(_GUTTER_OVERLAP_MAX_PT,
                    (gutter[1] - gutter[0]) * _GUTTER_OVERLAP_FRACTION)
    return min(box[2], gutter[1]) - max(box[0], gutter[0]) > tolerance


def _supplement_columns(groups, items, width):
    """正文优先作锚点，所有标签的窄框补足缺少正文的一栏。"""
    eligible = [tuple(item["box"]) for item in items
                if item.get("marginal") is not True and item.get("author") is not True]
    bounds = _bounds(eligible)
    narrow = [box for box in eligible if box[2] - box[0] < (bounds[2] - bounds[0]) * 0.6]
    if len(groups) == 2:
        gutter = (max(box[2] for box in groups[0]), min(box[0] for box in groups[1]))
        # 已有两侧正文时按其锚点聚类；页边小框不能凭离正文较远另造第三栏。
        evidence = [[], []]
        for box in narrow:
            if not _hits_gutter(box, gutter):
                side = 0 if (box[0] + box[2]) / 2 <= sum(gutter) / 2 else 1
                evidence[side].append(box)
        return [group + extra for group, extra in zip(groups, evidence)]
    candidates = _cluster_body(_column_candidates(narrow), width)
    if len(candidates) > 2:
        candidates = _merge_x_connected(candidates)
    if len(candidates) == 2:
        right_start = candidates[1][0][0]
        left_core = [box for box in candidates[0] if box[2] <= right_start]
        if left_core:
            return [left_core, candidates[1]]
        return groups
    return candidates if len(candidates) > 2 else groups


def _fit_spanning_bands(bands, seeds, elements):
    """迭代扩展/收缩带，直到每个框完整归属；不能保留种子时返回 None。"""
    seen = set()
    while tuple(bands) not in seen:
        seen.add(tuple(bands))
        changed = False
        for index, (start, end) in enumerate(bands):
            for box in sorted(elements):
                overlap = min(end, box[3]) - max(start, box[1])
                if box in seeds or overlap <= 0 or (start <= box[1] and box[3] <= end):
                    continue
                if overlap > (box[3] - box[1]) / 2:
                    replacement = [(min(start, box[1]), max(end, box[3]))]
                else:
                    # 元素可能盖住整个带；此时收缩后没有可保留的带。
                    replacement = [(a, b) for a, b in ((start, min(end, box[1])),
                                                       (max(start, box[3]), end)) if a < b]
                bands = _merge_bands(bands[:index] + replacement + bands[index + 1:], gap=0)
                if not all(any(a <= seed[1] and seed[3] <= b for a, b in bands)
                           for seed in seeds):
                    return None
                changed = True
                break
            if changed:
                break
        if not changed:
            return bands
    return None


def _spanning_bands(items, gutter, elements):
    """仅从真实间隙重叠或显式 author 种子扩展同行，绝不以行外接框生成种子。

    水平重叠严格大于 min(4pt, 间隙宽 25%) 才是几何种子。
    同行纵向交集须达到较矮块高度的一半，以容纳作者上标或小字号块。
    栏内正文、figure/table 容器和 marginal 不参与同行扩展，且其纵向范围
    阻止扩展吞入独立栏内容；种子自身的跨栏证据始终保留。
    """
    def same_row(left, right):
        overlap = min(left[3], right[3]) - max(left[1], right[1])
        return overlap >= 0.5 * min(left[3] - left[1], right[3] - right[1])

    def overlaps_any(box, others):
        return any(box[1] < other[3] and other[1] < box[3] for other in others)

    seeds, row_members, column_content = [], [], []
    for item in items:
        box = tuple(item["box"])
        author = item.get("author", False) is True
        marginal = item.get("marginal") is True
        label = item.get("label", item.get("class"))
        crossing = _hits_gutter(box, gutter)
        if crossing or author:
            seeds.append(box)
        if author or (not marginal and _row_member(label)):
            row_members.append(box)
        elif not crossing:
            column_content.append(box)

    # 先连接可扩展的同行块；只有种子能启动这些行的跨栏扩展。
    rows = []
    for box in row_members:
        matching = [row for row in rows if any(same_row(box, peer) for peer in row)]
        rows = [row for row in rows if row not in matching]
        rows.append([box, *(peer for row in matching for peer in row)])
    bands = []
    for seed in seeds:
        row = [seed, *(box for members in rows if any(same_row(seed, b) for b in members)
                       for box in members)]
        bounds = _bounds(row)
        # 单块跨栏证据始终保留；若扩展会吞掉独立栏内容，仅保留种子自身的带。
        if overlaps_any(bounds, column_content):
            bounds = seed
        bands.append((bounds[1], bounds[3]))
    return _fit_spanning_bands(_merge_bands(bands), seeds, elements)


def _column_regions(boxes, bands, column):
    """从栏内全部内容框扣除纵向带，逐段取外接框；同栏分段保持同一个栏号。"""
    bounds = _bounds(boxes)
    cursor = bounds[1]
    regions = []
    for start, end in [*bands, (bounds[3], bounds[3])]:
        if start > cursor:
            pieces = [(box[0], max(cursor, box[1]), box[2], min(start, box[3]))
                      for box in boxes if box[1] < start and box[3] > cursor]
            if pieces:
                regions.append(Region("column", _bounds(pieces), column=column))
        cursor = max(cursor, end)
    return regions


def _header_footer_bands(page, layouts, body, groups):
    """吸收 marginal 的文本框或无正文的边缘 abandon 小框；整带不得切开其他元素。"""
    marginal_boxes = [tuple(item["box"]) for item in page["paragraphs"]
                      if item.get("marginal") is True]
    related = [layout for layout in page["layouts"]
               if layout.get("class") in _TEXT_LAYOUT_CLASSES
               and any(max(layout["box"][0], box[0]) < min(layout["box"][2], box[2])
                       and max(layout["box"][1], box[1]) < min(layout["box"][3], box[3])
                       for box in marginal_boxes)]
    body_bounds = _bounds(body)
    frames = [_bounds(group) for group in groups if group]
    for layout in page["layouts"]:
        box = layout["box"]
        # 无段落页码只接受模型明确标为 abandon 的小框；普通标题/文本不是边缘证据。
        # 下方 blockers 同时排除任何与该带相交的非 marginal 段落和独立版面框。
        if (layout not in related and layout.get("class") == "abandon"
                and box[2] - box[0] <= page["width"] * 0.1
                and box[3] - box[1] <= page["height"] * 0.02
                and (box[3] <= body_bounds[1] or box[1] >= body_bounds[3])
                and not any(frame[0] <= box[0] and frame[1] <= box[1]
                            and box[2] <= frame[2] and box[3] <= frame[3] for frame in frames)):
            related.append(layout)
    candidates = marginal_boxes + [tuple(layout["box"]) for layout in related]
    blockers = [tuple(item["box"]) for item in page["paragraphs"]
                if item.get("marginal") is not True]
    blockers += [tuple(layout["box"]) for layout in layouts if layout not in related]
    # 扩框若碰到正文，连同与其相交的页眉候选一起留在栏内，不能裁切任一框。
    while True:
        blocked = [box for box in candidates
                   if any(box[1] < other[3] and other[1] < box[3] for other in blockers)]
        if not blocked:
            break
        candidates = [box for box in candidates if box not in blocked]
        blockers.extend(blocked)
    return _merge_bands([(box[1], box[3]) for box in candidates], gap=0)


def plan_page(page: dict) -> PagePlan:
    """将规格 §1 的单页 dict 转为规格 §2 的分栏计划。

    任一段落/版面框非有限、空框或反向框，均整页回退并记录原因。
    尺寸无效时回退框的对应轴为 0，调用方须从源页重新取得真实尺寸。
    """
    safe_page = {
        "index": page["index"],
        "width": _safe_extent(page.get("width")),
        "height": _safe_extent(page.get("height")),
    }
    try:
        _validate_geometry(page)
    except (KeyError, TypeError, ValueError, OverflowError):
        return _fallback(safe_page, "invalid_geometry")
    if page.get("rotation", 0) != 0:
        return _fallback(page, "rotation")
    # 只有非文本容器能代表内部段落；文本框必须保留段落自身标签与显式语义。
    containers = [layout for layout in page["layouts"]
                  if layout.get("class") in _CONTAINER_LAYOUT_CLASSES]
    paragraphs = [item for item in page["paragraphs"]
                  if not any(_contains_center(layout["box"], item["box"])
                             for layout in containers)]
    # 含任一原始段落的文本框不重复提供布局证据（即便该段落另归属于容器）。
    # 空文本框仍保留，避免遗漏只有版面框、没有段落的一栏或跨栏标题。
    layouts = [layout for layout in page["layouts"]
               if layout.get("class") not in _TEXT_LAYOUT_CLASSES
               or not any(_contains_center(layout["box"], item["box"])
                          for item in page["paragraphs"])]
    original_body = [tuple(item["box"]) for item in page["paragraphs"]
                     if item["label"] in BODY_TEXT_LAYOUT_LABELS
                     and item.get("marginal") is not True and item.get("author") is not True]
    if not original_body:
        return _fallback(page, "no_body")
    body = [tuple(item["box"]) for item in paragraphs
            if item["label"] in BODY_TEXT_LAYOUT_LABELS
            and item.get("marginal") is not True and item.get("author") is not True]
    # 无框外正文且框与其内容互相越界时，不能用框内段落重建两栏并裁框。
    # 完整嵌套的单栏内容仍可安全归入同一区域。
    if not body and any(_contains_center(layout["box"], box)
                        and _bounds([box, layout["box"]]) not in (box, tuple(layout["box"]))
                        for box in original_body for layout in containers):
        return _fallback(page, "region_boundary_conflict")
    groups = _cluster_body(_column_candidates(body), page["width"])
    if len(groups) > 2:
        # 多于两组时先按水平重叠合并同栏缩进段；两组仍走下方通栏摘要处理
        groups = _merge_x_connected(groups)
    if len(groups) > 2:
        return _fallback(page, "too_many_columns")
    if len(groups) == 2:
        # 通栏摘要仍可标为正文，不能让其右边缘吞掉栏间隙。
        right_start = groups[1][0][0]
        left_core = [box for box in groups[0] if box[2] <= right_start]
        if left_core:
            groups = [left_core, groups[1]]
        else:
            # 只有水平相叠的缩进段落，没有可分开的两栏。
            groups = [body]
    if len(groups) < 2:
        # 落在某对窄正文外包络内的正文仍会退出锚点预筛，单栏须覆盖全部正文。
        groups = [body]
    body_groups = groups
    body_box = _bounds([box for group in body_groups for box in group] or original_body)
    # 不跨过空隙合并页眉页脚，空隙中可能存在非 marginal 元素。
    header_bands = _header_footer_bands(page, layouts, original_body, body_groups)
    headers = [Region("header_footer", (0, start, page["width"], end))
               for start, end in header_bands]
    spanning = []
    bands = []
    items = [item for item in page["paragraphs"] + page["layouts"]
             if not any(start <= item["box"][1] and item["box"][3] <= end
                        for start, end in header_bands)]
    elements = [tuple(item["box"]) for item in items]
    evidence = [item for item in paragraphs + layouts
                if not any(start <= item["box"][1] and item["box"][3] <= end
                           for start, end in header_bands)]
    groups = _supplement_columns(groups, evidence, page["width"])
    if len(groups) > 2:
        return _fallback(page, "too_many_columns")
    if len(groups) == 2:
        # 正文优先确定边界；没有正文的一侧使用该侧全部窄元素。
        boundaries = (body_groups if len(body_groups) == 2 else
                      [[box for box in group if box in body] or group for group in groups])
        gutter = (max(box[2] for box in boundaries[0]), min(box[0] for box in boundaries[1]))
        bands = _spanning_bands(evidence, gutter, elements)
        if bands is None or any(start < b and a < end for start, end in bands
                                for a, b in header_bands):
            return _fallback(page, "region_boundary_conflict")
        for start, end in bands:
            position = "top" if end <= body_box[1] else (
                "bottom" if start >= body_box[3] else "middle")
            spanning.append(Region("spanning", (0, start, page["width"], end), position))
    # 正文只负责识别栏数/间隙，栏框必须覆盖标题、保留内容及图表等所有元素。
    # 直接命中间隙的框已归为跨栏带；其余按水平位置归入相应栏。
    content_groups = [[] for _ in groups]
    for box in elements:
        if len(groups) == 1 or box[2] <= gutter[0]:
            index = 0
        elif box[0] >= gutter[1]:
            index = 1
        else:
            # 容差内擦边或落在间隙内的窄块按中心点归栏。
            index = 0 if (box[0] + box[2]) / 2 <= sum(gutter) / 2 else 1
        content_groups[index].append(box)
    columns = [region for index, boxes in enumerate(content_groups) if boxes
               for region in _column_regions(boxes, _merge_bands(bands + header_bands, gap=0), index)]
    return PagePlan(page["index"], len(groups), tuple(headers + spanning + columns))
