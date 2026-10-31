"""T2：按内容流顺序把字符切为连续行。

输入为页面坐标的 Char，输出不可变 Line 元组；不读 PDF、不执行 I/O。
非有限或倒置的字符框抛 ValueError，错误信息不含正文。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import isfinite

from ztf_split_types import Char, Line


@dataclass(frozen=True)
class _WritingLine(Line):
    # 冻结 Line 接口仍可直接使用；保留字符框以区分中线上的字和空白。
    chars: tuple[Char, ...] = field(repr=False)


def _characters(line: Line) -> tuple[Char, ...]:
    """build_lines 保留逐字数据；手工构造的 Line 视为单个连续字符框。"""
    if isinstance(line, _WritingLine):
        return tuple(c for c in line.chars if not _text_only(c))
    return tuple(Char(line.char_ids[0] if line.char_ids else -1,
                      line.order_start, 0, line.x0, line.y0, line.x1, line.y1,
                      line.text, paragraph)
                 for paragraph in (line.paragraphs or (-1,)))


def _text_only(char):
    # 分段时被删除的真实空格只还原行文字，不贡献几何、压中线或回跳证据。
    return char.paragraph == -1 and char.text.isspace()


def build_lines(page: int, chars: Sequence[Char]) -> tuple[Line, ...]:
    """按 (order, sub_order) 切行；输入字符和顺序容器均不修改。

    同主基线或与主基线字符框纵向重叠时，向右连续写入。
    向左重写另起一行；任何正宽空隙都不切行。
    分组锚框固定，不被上下标缩窄或随上下标字数漂移。
    baseline 摘要沿用整行最常见的 y1（并列时取最先出现者）。
    """
    groups: list[list[Char]] = []
    anchor_top = anchor_baseline = None
    ordered = sorted(chars, key=lambda char: (char.order, char.sub_order))
    for char in ordered:
        if _text_only(char):
            continue
        if (not all(isfinite(value) for value in (char.x0, char.y0, char.x1, char.y1))
                or char.x1 < char.x0 or char.y1 < char.y0):
            raise ValueError("invalid_char_box")
        previous = groups[-1][-1] if groups else None
        same_row = previous is not None and (
            anchor_baseline == char.y1
            or max(anchor_top, char.y0) < min(anchor_baseline, char.y1))
        continuous = previous is not None and char.x0 >= previous.x0
        if not same_row or not continuous:
            groups.append([])
            anchor_top, anchor_baseline = char.y0, char.y1
        groups[-1].append(char)
    baselines = []
    for group in groups:
        counts: dict[float, int] = {}
        for char in group:
            counts[char.y1] = counts.get(char.y1, 0) + 1
        baseline = max(counts, key=counts.get)
        baselines.append(baseline)
    # 真实空格按内容流顺序附回相邻书写行；框和基线仍只由几何字符决定。
    owners = {c.id: i for i, group in enumerate(groups) for c in group}
    text_groups = [[] for _ in groups]
    current = 0
    for char in ordered:
        current = owners.get(char.id, current)
        if text_groups:
            text_groups[current].append(char)
    return tuple(
        _WritingLine(page, index, tuple(char.id for char in text_groups[index]),
             group[0].order, group[-1].order,
             min(char.x0 for char in group), max(char.x1 for char in group),
             min(char.y0 for char in group), max(char.y1 for char in group),
             baselines[index], "".join(char.text for char in text_groups[index]),
             tuple(sorted({char.paragraph for char in group if char.paragraph > -1})),
             tuple(group))
        for index, group in enumerate(groups)
    )


def build_rows(lines: Sequence[Line]) -> tuple[tuple[int, ...], ...]:
    """同基线或字符框纵向重叠的书写行合并为视觉行，返回输入位置。

    按纵向区间的连通分量合并；不要求内容流相邻，不设坐标容差。
    """
    parents = list(range(len(lines)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    baselines = {}
    intervals = []
    for index, line in enumerate(lines):
        if line.baseline in baselines:
            parents[root(index)] = root(baselines[line.baseline])
        baselines[line.baseline] = index
        intervals.extend((char.y0, char.y1, index) for char in _characters(line)
                         if char.y0 < char.y1)
    end = None
    anchor = None
    for top, bottom, index in sorted(intervals):
        if end is not None and top < end:
            parents[root(index)] = root(anchor)
            end = max(end, bottom)
        else:
            anchor, end = index, bottom
    rows = {}
    for index in range(len(lines)):
        rows.setdefault(root(index), []).append(index)
    return tuple(tuple(row) for row in sorted(rows.values(), key=lambda row: row[0]))
