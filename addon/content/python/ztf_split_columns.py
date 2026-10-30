"""#62：全文单/双栏判定与页面中线归属，纯函数，不设视觉容差。"""
from collections.abc import Mapping, Sequence
from math import isfinite

from ztf_split_lines import build_rows, _characters
from ztf_split_types import DocumentColumns, Line, MarginReason, MARGIN, PageColumns, SPANNING


def _validate(width, height, lines, figure_boxes):
    if not (isfinite(width) and isfinite(height) and width > 0 and height > 0):
        raise ValueError('invalid_page_size')
    for line in lines:
        if (not all(isfinite(v) for v in (line.x0, line.y0, line.x1, line.y1, line.baseline))
                or line.x1 < line.x0 or line.y1 < line.y0):
            raise ValueError('invalid_line_box')
    for b in figure_boxes:
        if len(b) != 4 or not all(isfinite(v) for v in b) or b[2] < b[0] or b[3] < b[1]:
            raise ValueError('invalid_figure_box')


def _inside(line, box):
    return box[0] <= line.x0 and box[1] <= line.y0 and line.x1 <= box[2] and line.y1 <= box[3]


def _side(line, mid):
    if line.x1 <= mid:
        return 0
    if line.x0 >= mid:
        return 1
    return SPANNING


def detect_document_columns(
    pages: Sequence[Sequence[Line]], widths: Sequence[float], heights: Sequence[float],
    margins: Mapping[int, Mapping[int, MarginReason]],
    figure_boxes: Mapping[int, Sequence[tuple[float, float, float, float]]],
    column_count_tag: int | None = None,
) -> DocumentColumns:
    """标签优先，否则从第一页寻找左右两组之间的首个回跳点。"""
    if len(pages) != len(widths) or len(pages) != len(heights):
        raise ValueError('invalid_page_sizes')
    for i, lines in enumerate(pages):
        _validate(widths[i], heights[i], lines, figure_boxes.get(i, ()))
    if column_count_tag in (1, 2):
        return DocumentColumns(column_count_tag, 'tag', None, ('column_count_tag',))
    for i, lines in enumerate(pages):
        mid = widths[i] / 2
        body = sorted((line for line in lines if line.index not in margins.get(i, {})
                       and not any(_inside(line, b) for b in figure_boxes.get(i, ()))),
                      key=lambda line: (line.order_start, line.order_end, line.index))
        previous = first = None
        for line in body:
            side = _side(line, mid)
            if previous is not None:
                upward = line.baseline < previous.y0
                if (upward and line.baseline <= first.baseline and line.x0 > previous.x1
                        and _side(previous, mid) == 0 and side == 1):
                    return DocumentColumns(2, 'jump', i, ('jump',))
                # 本组是中线同侧连续书写的一组；通栏内容不属于左右任一组。
                if side != _side(previous, mid):
                    first = line
            else:
                first = line
            previous = line
    return DocumentColumns(1, 'single', None, ('single_no_evidence',))


def detect_columns(
    page: int, width: float, height: float, lines: Sequence[Line],
    margins: Mapping[int, MarginReason], document: DocumentColumns,
    figure_boxes: Sequence[tuple[float, float, float, float]] = (),
) -> PageColumns:
    """视觉行有任一字符压中线即整行跨栏；段落以其各字符归属汇总。"""
    _validate(width, height, lines, figure_boxes)
    mid = width / 2
    figures = [b for b in figure_boxes if b[0] < mid < b[2]] if document.columns == 2 else []
    body_positions = [i for i, line in enumerate(lines) if line.index not in margins]
    body = [lines[i] for i in body_positions]
    spanning = set()
    if document.columns == 2:
        for row in build_rows(body):
            if any(char.x0 < mid < char.x1 for i in row for char in _characters(body[i])):
                spanning.update(body_positions[i] for i in row)
        spanning.update(i for i in body_positions if any(_inside(lines[i], b) for b in figures))
    assignments = []
    paragraphs = {}
    reasons = list(document.reasons)
    if spanning:
        reasons.append('spanning_line')
    for i, line in enumerate(lines):
        if line.index in margins:
            assignments.append(MARGIN)
            continue
        for char in _characters(line):
            group = (SPANNING if i in spanning else
                     0 if document.columns == 1 or char.x1 <= mid else 1)
            if char.paragraph >= 0:
                paragraphs.setdefault(char.paragraph, set()).add(group)
        # Line 的单值字段不能表示同一书写行的左右字符；用起始字符所属栏。
        # 字符及段落归属始终取逐字数据，不能从此摘要反推。
        assignments.append(SPANNING if i in spanning else
                           0 if document.columns == 1 or _characters(line)[0].x1 <= mid else 1)
    owners, split = [], []
    for paragraph, groups in sorted(paragraphs.items()):
        if SPANNING in groups:
            owners.append((paragraph, SPANNING))
        elif len(groups) > 1:
            split.append(paragraph)
        else:
            owners.append((paragraph, next(iter(groups))))
    if split:
        reasons.append('split_paragraph')
    if figures:
        reasons.append('spanning_figure')
    return PageColumns(page, document.columns, document.basis,
                       ((mid, mid),) if document.columns == 2 else (),
                       tuple(assignments), tuple(owners), tuple(split), tuple(reasons))
