"""0.8.0 分栏双语的共享数据结构（Lead 冻结，见 docs/specs/0.8.0-interfaces.md）。

只放数据结构，不放逻辑。坐标均为页面坐标：左上原点、单位 pt、y 向下增大。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Char:
    """一个源页字符。

    order / sub_order 是内容流书写顺序（BabelDOC IL 的 render_order /
    sub_render_order；PyMuPDF 夹具里用 rawdict 遍历序号，sub_order 为 0）。
    paragraph 是该字符所属段落在本页 paragraphs 列表中的下标，不属于任何段落为 -1。
    xobj 是所在 Form XObject 的编号，不在 XObject 中为 None。
    """

    id: int
    order: int
    sub_order: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    paragraph: int = -1
    xobj: int | None = None


@dataclass(frozen=True)
class Line:
    """按内容流顺序、同一基线连续写出的一组字符（ztf_split_lines.build_lines 产出）。"""

    page: int
    index: int
    char_ids: tuple[int, ...]
    order_start: int
    order_end: int
    x0: float
    x1: float
    y0: float
    y1: float
    baseline: float
    text: str
    paragraphs: tuple[int, ...]


MarginReason = Literal["tagged", "repeated"]
"""页眉页脚判定依据：PDF 标签、跨页重复。追加内容流只作辅证，不是独立依据。"""

ColumnBasis = Literal["jump", "tag", "single"]
"""全文认栏依据（2026-10-30 修订）：内容流回跳点、ColumnCount 结构标签、一栏。"""

SPANNING = -2
"""Line / 段落归属：跨栏。"""

MARGIN = -1
"""Line 归属：页眉、页脚、页码、水印。"""


@dataclass(frozen=True)
class PageColumns:
    """单页认栏结果（ztf_split_columns.detect_columns 产出）。

    columns 为 1 或 2，不存在"认不出"。双栏时 gutters == ((页面中线 x, 页面中线 x),)，
    单栏时为空（2026-10-30 修订：双栏一律以页面中线为分界）。
    line_column[i] 对应 lines[i]：0..columns-1、SPANNING 或 MARGIN。
    paragraph_column 为 (段落下标, 栏号或 SPANNING) 的元组，按段落下标升序。
    split_paragraphs 为字符同时落在中线左右两侧、且不含跨栏行的段落下标（BabelDOC
    跨栏断句未拆开时出现），由调用方按片段处理，不猜归属。
    reasons 为固定原因码，写入任务日志，不含正文。
    """

    page: int
    columns: int
    basis: ColumnBasis
    gutters: tuple[tuple[float, float], ...]
    line_column: tuple[int, ...]
    paragraph_column: tuple[tuple[int, int], ...]
    split_paragraphs: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentColumns:
    """全文认栏结果（ztf_split_columns.detect_document_columns 产出）。

    columns 为 1 或 2；basis 为 jump / tag / single；page 为判定所依据的源页号
    （0 起算，single 时为 None）。reasons 为固定原因码，不含正文。
    """

    columns: int
    basis: ColumnBasis
    page: int | None = None
    reasons: tuple[str, ...] = ()
