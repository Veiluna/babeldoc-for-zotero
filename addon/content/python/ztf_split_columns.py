"""T2：从内容流行坐标认栏，返回 PageColumns，固定原因码不含正文。

纯函数，无网络或 PDF I/O；margins 中的行不参与认栏。
非有限或倒置的坐标、非正页面尺寸抛 ValueError。
"""

from collections.abc import Mapping, Sequence
from math import isfinite

from ztf_split_types import Line, MarginReason, MARGIN, PageColumns, SPANNING


def _jump_gutters(lines: Sequence[Line], height: float) -> tuple[tuple[float, float], ...]:
    """按回跳点分内容流组，再用各组极值计算栏缝；非正栏缝交给备用依据。"""
    groups: list[list[Line]] = [[]]
    seeds = []
    previous = None
    for line in sorted(lines, key=lambda line: (line.order_start, line.order_end, line.index)):
        # 2 来自规格的半页高定义，不是视觉容差。
        if (previous is not None and previous.baseline - line.baseline > height / 2
                and line.x0 > previous.x1):
            seeds.append((previous.x1, line.x0))
            groups.append([])
        groups[-1].append(line)
        previous = line
    if not seeds:
        return ()
    members_by_group: list[list[Line]] = [[] for _ in groups]
    for group_index, group in enumerate(groups):
        rows: list[list[Line]] = []
        for line in group:
            if (not rows or line.baseline != rows[-1][-1].baseline
                    or line.x0 < rows[-1][-1].x0):
                rows.append([])
            rows[-1].append(line)
        for row in rows:
            crossed = [(left, right) for left, right in seeds
                       if row[0].x0 < left and max(line.x1 for line in row) > right]
            # 字符覆盖栏缝的连续标题/页脚不属于任何单栏组；
            # 同基线两段之间保留着完整栏缝时，两侧段分别计入对应组。
            if any(line.x0 < right and line.x1 > left
                   for left, right in crossed for line in row):
                continue
            for line in row:
                target = (sum(line.x0 >= right for _, right in seeds)
                          if crossed else group_index)
                members_by_group[target].append(line)
    extents = []
    for members in members_by_group:
        if not members:
            return ()
        extents.append((min(line.x0 for line in members), max(line.x1 for line in members)))
    gutters = tuple((left[1], right[0]) for left, right in zip(extents, extents[1:]))
    if any(not right > left for left, right in gutters):
        return ()
    return gutters


def _gap_gutters(lines: Sequence[Line]) -> tuple[tuple[float, float], ...]:
    """同基线分段空隙在严格多数行中的交集；每个基线只计一票。

    左右栏行距可能不同，因此每个基线独立读取纵向重叠的文字段，
    不做会丢失匹配的逐行一对一配对。相邻行框略有重叠也无妨：
    同侧区间合并，只有共同空白能投票。2 只表示多数定义的一半。
    """
    baselines: dict[float, list[Line]] = {}
    for line in lines:
        baselines.setdefault(line.baseline, []).append(line)
    rows = [[line for line in lines if any(
        line.baseline == anchor.baseline or max(line.y0, anchor.y0) < min(line.y1, anchor.y1)
        for anchor in anchors)] for anchors in baselines.values()]
    gaps = []
    for row_index, row in enumerate(rows):
        right = None
        for line in sorted(row, key=lambda line: line.x0):
            if right is not None and line.x0 > right:
                gaps.append((right, line.x0, row_index))
            right = line.x1 if right is None else max(right, line.x1)
    edges = sorted({edge for left, right, _ in gaps for edge in (left, right)})
    candidates = {}
    for left, right in zip(edges, edges[1:]):
        middle = (left + right) / 2
        supporting = [(start, end, row) for start, end, row in gaps if start < middle < end]
        support = len({row for _, _, row in supporting})
        if support > len(rows) / 2:
            intersection = (max(start for start, _, _ in supporting),
                            min(end for _, end, _ in supporting))
            candidates[intersection] = support
    selected = []
    for gap in sorted(candidates, key=lambda gap: (-candidates[gap], -(gap[1] - gap[0]), gap)):
        if any(gap[0] < other[1] and other[0] < gap[1] for other in selected):
            continue
        proposed = selected + [gap]
        support = 0
        for row_index in range(len(rows)):
            evidence = [next(((left, right) for left, right, row in gaps
                              if row == row_index and left < (start + end) / 2 < right), None)
                        for start, end in proposed]
            # N 栏要有 N-1 个不同空隙；同一个宽空白不能重复算作多条栏缝，
            # 否则参考文献悬挂缩进会被当成额外一栏。
            if None not in evidence and len(set(evidence)) == len(proposed):
                support += 1
        if support > len(rows) / 2:
            selected.append(gap)
    return tuple(sorted(selected))


def detect_columns(
    page: int,
    width: float,
    height: float,
    lines: Sequence[Line],
    margins: Mapping[int, MarginReason],
    figure_boxes: Sequence[tuple[float, float, float, float]] = (),
) -> PageColumns:
    """优先回跳点，随后同基线空隙交集，均无证据时返回一栏。

    line_column 按输入位置返回；margins 则按 Line.index 匹配。
    build_lines 已将正宽空隙拆开，每个 Line 内字符的横向覆盖连续；
    因此只有段框与栏缝有交集才是跨栏，栏缝两侧的独立段分别归栏。
    跨栏图/表/公式框中完整包含的行随元素归 SPANNING；无文字的元素
    仅以 spanning_figure 原因码报告（冻结结构没有元素归属字段）。
    仅跨组、不含跨栏行的段落只列入 split_paragraphs，不伪造单栏归属。
    """
    if not (isfinite(width) and isfinite(height) and width > 0 and height > 0):
        raise ValueError("invalid_page_size")
    for line in lines:
        if (not all(isfinite(value) for value in (line.x0, line.y0, line.x1, line.y1, line.baseline))
                or line.x1 < line.x0 or line.y1 < line.y0):
            raise ValueError("invalid_line_box")
    for box in figure_boxes:
        if (not all(isfinite(value) for value in box)
                or box[2] < box[0] or box[3] < box[1]):
            raise ValueError("invalid_figure_box")
    body = [line for line in lines if line.index not in margins]
    gutters = _jump_gutters(body, height)
    basis = "jump" if gutters else "single"
    if not gutters:
        gutters = _gap_gutters(body)
        if gutters:
            basis = "gap"
    spanning_figures = [box for box in figure_boxes
                        if any(box[0] < left and box[2] > right for left, right in gutters)]
    assignments = []
    paragraph_groups: dict[int, set[int]] = {}
    reasons = [{"jump": "jump", "gap": "gap_intersection", "single": "single_no_evidence"}[basis]]
    for line in lines:
        if line.index in margins:
            column = MARGIN
        elif any(line.x0 < right and line.x1 > left for left, right in gutters):
            column = SPANNING
            if "spanning_line" not in reasons:
                reasons.append("spanning_line")
        elif any(not (line.x0 < box[0] or line.y0 < box[1]
                      or line.x1 > box[2] or line.y1 > box[3]) for box in spanning_figures):
            column = SPANNING
        else:
            column = sum(not line.x0 < right for left, right in gutters)
        assignments.append(column)
        if column != MARGIN:
            for paragraph in line.paragraphs:
                if paragraph > -1:
                    paragraph_groups.setdefault(paragraph, set()).add(column)
    paragraphs = []
    split_paragraphs = []
    for paragraph, groups in sorted(paragraph_groups.items()):
        if SPANNING in groups:
            paragraphs.append((paragraph, SPANNING))
        elif len(groups) > 1:
            split_paragraphs.append(paragraph)
        else:
            paragraphs.append((paragraph, next(iter(groups))))
    if split_paragraphs:
        reasons.append("split_paragraph")
    if spanning_figures:
        reasons.append("spanning_figure")
    return PageColumns(page, len(gutters) + 1, basis, gutters,
                       tuple(assignments), tuple(paragraphs), tuple(split_paragraphs), tuple(reasons))
