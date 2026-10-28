"""T2：按内容流顺序把字符切为连续行。

输入为页面坐标的 Char，输出不可变 Line 元组；不读 PDF、不执行 I/O。
非有限或倒置的字符框抛 ValueError，错误信息不含正文。
"""

from collections.abc import Sequence
from math import isfinite

from ztf_split_types import Char, Line


def build_lines(page: int, chars: Sequence[Char]) -> tuple[Line, ...]:
    """按 (order, sub_order) 切行；输入字符和顺序容器均不修改。

    同基线或整组字符框有共同纵向交集时，向右连续写入。
    向左重写另起一行。先确定整行基线，再将每个正宽空隙两侧拆为不同
    Line，所有段沿用整行基线，供认栏读取空隙；不以字符宽度作阈值。
    baseline 取整行最常见的 y1（并列时取最先出现者），不受上标改变。
    """
    groups: list[list[Char]] = []
    overlap_top = overlap_bottom = 0.0
    for char in sorted(chars, key=lambda char: (char.order, char.sub_order)):
        if (not all(isfinite(value) for value in (char.x0, char.y0, char.x1, char.y1))
                or char.x1 < char.x0 or char.y1 < char.y0):
            raise ValueError("invalid_char_box")
        previous = groups[-1][-1] if groups else None
        same_row = previous is not None and (
            previous.y1 == char.y1
            or max(overlap_top, char.y0) < min(overlap_bottom, char.y1))
        continuous = previous is not None and char.x0 >= previous.x0
        if not same_row or not continuous:
            groups.append([])
            overlap_top, overlap_bottom = char.y0, char.y1
        else:
            overlap_top = max(overlap_top, char.y0)
            overlap_bottom = min(overlap_bottom, char.y1)
        groups[-1].append(char)
    segments: list[list[Char]] = []
    baselines = []
    for group in groups:
        counts: dict[float, int] = {}
        for char in group:
            counts[char.y1] = counts.get(char.y1, 0) + 1
        baseline = max(counts, key=counts.get)
        right = None
        for char in group:
            if right is None or char.x0 > right:
                segments.append([])
                baselines.append(baseline)
            segments[-1].append(char)
            right = char.x1 if right is None else max(right, char.x1)
    return tuple(
        Line(page, index, tuple(char.id for char in group),
             group[0].order, group[-1].order,
             min(char.x0 for char in group), max(char.x1 for char in group),
             min(char.y0 for char in group), max(char.y1 for char in group),
             baselines[index], "".join(char.text for char in group),
             tuple(sorted({char.paragraph for char in group if char.paragraph > -1})))
        for index, group in enumerate(segments)
    )
