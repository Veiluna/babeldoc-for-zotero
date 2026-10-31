"""T3 页眉、页脚与水印证据及判定（冻结接口见 0.8.0-interfaces）。

classify_margins 输入各页 Line 和证据框，输出 {0 起页号: {Line.index: 原因}}。
read_pdf_evidence 只读源 PDF，返回 tagged / appended 的页面坐标文字框。
参数不合法或 PDF 无法解析时抛出异常，交由调用方处理；
本模块不调用网络、不写源 PDF，日志只含原因码与计数，不含原文。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from bisect import bisect_right
import json
import logging
import math
import re

import pymupdf

from ztf_split_types import Line, MarginReason

_LOGGER = logging.getLogger(__name__)


def _log(event: str, **fields) -> None:
    """仅向调用方配置的 logger 发结构化事件，不配置全局 handler。"""
    _LOGGER.info(json.dumps({"event": event, **fields}, separators=(",", ":")))


def _valid_box(box: Sequence[float]) -> bool:
    return (len(box) == 4 and all(math.isfinite(v) for v in box)
            and box[0] <= box[2] and box[1] <= box[3])


def _inside(item: Line, boxes: Sequence[tuple[float, float, float, float]]) -> bool:
    return any(x0 <= item.x0 and y0 <= item.y0 and
               item.x1 <= x1 and item.y1 <= y1 for x0, y0, x1, y1 in boxes)


def _key(item: Line) -> tuple[str, float]:
    return " ".join(re.sub(r"\d+", "#", item.text).split()), item.baseline


def classify_margins(
    pages: Sequence[Sequence[Line]],
    heights: Sequence[float],
    tagged: Mapping[int, Sequence[tuple[float, float, float, float]]] | None = None,
    appended: Mapping[int, Sequence[tuple[float, float, float, float]]] | None = None,
) -> dict[int, dict[int, MarginReason]]:
    """先标签后跨页重复；appended 不改变结果，页键包含无命中的页。

    数字串统一为 #（页码 9 / 10 也相同），空白折叠。baseline 直接比较
    输入浮点数，不舍入、不放宽。重复须跨至少两页，且覆盖全篇至少 1/3。
    heights 为冻结接口的页面尺寸输入，不用于设置页边区域或视觉容差。
    """
    if len(pages) != len(heights) or any(not math.isfinite(h) or h <= 0 for h in heights):
        raise ValueError("invalid_page_heights")
    for evidence in (tagged, appended):
        for boxes in (evidence or {}).values():
            if any(not _valid_box(box) for box in boxes):
                raise ValueError("invalid_evidence_box")
    occurrences = defaultdict(set)
    for page, lines in enumerate(pages):
        for item in lines:
            if (not _valid_box((item.x0, item.y0, item.x1, item.y1))
                    or not math.isfinite(item.baseline)):
                raise ValueError("invalid_line_coordinates")
            key = _key(item)
            if key[0]:
                occurrences[key].add(page)
    result = {}
    for page, lines in enumerate(pages):
        found = {}
        for item in lines:
            key = _key(item)
            count = len(occurrences[key])
            corroborated = _inside(item, (appended or {}).get(page, ()))
            if _inside(item, (tagged or {}).get(page, ())):
                found[item.index] = "tagged"
            elif key[0] and count >= 2 and count * 3 >= len(pages):
                found[item.index] = "repeated"
            if item.index in found or corroborated:
                _log("split_margin", page=page, line=item.index,
                     reason=found.get(item.index, "appended_only"),
                     count=count, appended=corroborated)
        result[page] = found
    return result


def _tokens(data: bytes):
    """用本机 MuPDF 的 PDF lexer，避免把字符串/注释里的 BDC 当操作符。

    依据：PyMuPDF 1.28.2 mupdf.py 的 pdf_lex、PdfLexbuf 与 fz_tell。
    scratch 对 NAME 已解码 #xx；STRING 由原生 lexer 完整跳过（含转义）。
    """
    native = pymupdf.mupdf
    buffer = native.fz_new_buffer_from_copied_data(data)
    stream = native.fz_open_buffer(buffer)
    lexbuf = native.PdfLexbuf(native.PDF_LEXBUF_SMALL)  # MuPDF 缓冲区规格，不是文字阈值。
    while True:
        start = native.fz_tell(stream)
        kind = native.pdf_lex(stream, lexbuf)
        end = native.fz_tell(stream)
        if kind == native.PDF_TOK_EOF:
            return
        yield start, end, kind, lexbuf.m_internal.scratch


def _operations(data: bytes):
    """保留原字节区间；inline image 的二进制不交给文字 lexer 猜测。"""
    native = pymupdf.mupdf
    operands = []
    start = 0
    for _, end, kind, value in _tokens(data):
        if kind == native.PDF_TOK_KEYWORD:
            if value == "BI":
                raise ValueError("inline_image_evidence_unsupported")
            yield start, end, value, operands
            operands = []
            start = end
        else:
            operands.append((kind, value))


def _pagination(operands, document, page) -> bool:
    """仅认 Artifact + 根属性字典的 Type/Subtype；支持资源命名属性。"""
    native = pymupdf.mupdf
    name = native.PDF_TOK_NAME
    if not operands or operands[0] != (name, "Artifact"):
        return False
    properties = operands[1:]
    if len(properties) == 1 and properties[0][0] == name:
        kind, value = document.xref_get_key(
            page.xref, "Resources/Properties/" + properties[0][1])
        if kind == "xref":
            value = document.xref_object(int(value.split()[0]), compressed=True)
        properties = [(k, v) for _, _, k, v in _tokens(value.encode("utf-8"))]
    depth = 0
    previous = None
    for kind, value in properties:
        if kind in (native.PDF_TOK_OPEN_DICT, native.PDF_TOK_OPEN_ARRAY):
            depth += 1
        elif kind in (native.PDF_TOK_CLOSE_DICT, native.PDF_TOK_CLOSE_ARRAY):
            depth -= 1
        elif depth == 1 and kind == name:
            if ((previous == "Type" and value == "Pagination") or
                    (previous == "Subtype" and value in ("Header", "Footer", "Watermark"))):
                return True
            previous = value if previous is None else None
            continue
        previous = None
    return False


def _masked(data, operations, selected):
    """用空 ActualText 遮住非目标文字，保持所有定位/推进/变换操作。

    不能截断或孤立解析流：FoBa 尾流开头的 Q 对应前面的 q。
    空 ActualText 只影响文本提取，不移除字形绘制或改变文字推进。
    """
    parts = []
    cursor = 0
    for index, (start, end, operator, _) in enumerate(operations):
        if operator in ("Tj", "TJ", "'", '"', "Do") and index not in selected:
            parts.extend((data[cursor:start], b"\n/Span <</ActualText ()>> BDC\n",
                          data[start:end], b"\nEMC\n"))
            cursor = end
    parts.append(data[cursor:])
    return b"".join(parts)


def _extract_boxes(document, page, scratch, data):
    document.update_stream(scratch, data)
    page.set_contents(scratch)
    boxes = []
    count = 0
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    for block in page.get_text("dict", flags=flags, sort=False)["blocks"]:
        for item in block.get("lines", ()):
            text = "".join(span["text"] for span in item["spans"])
            if text.strip():
                box = tuple(item["bbox"])
                if not _valid_box(box):
                    raise ValueError("invalid_pdf_text_box")
                boxes.append(box)
                count += len(text.strip())
    return boxes, count


def read_pdf_evidence(
    pdf_path: str,
) -> tuple[dict[int, list[tuple[float, float, float, float]]],
           dict[int, list[tuple[float, float, float, float]]]]:
    """返回 (tagged, appended)，键为 0 起页号，值为页面文字行框。

    依据：Page.get_contents 按 /Contents 数组顺序返回 xref；xref_stream
    解压内容，xref_get_key 解析属性。所有遮罩只改打开文档的内存对象，
    用 get_text('dict', sort=False) 取原坐标，finally 恢复 /Contents 并关闭。
    appended 的“小量”按数据比较：连续独立尾流的总文字量小于
    前面某个正文流；没有固定字数/行数/字体/坐标阈值。尾流须自行 BT、Tf、
    定位后写字，不能是上个流中未完成文本对象的续写。

    inline image 页的词法提取暂不支持，记录明确原因后不给该页证据；
    Form 内部标签暂不递归，外层 Artifact 包含的 Form 可正常提取。
    PDF 打开/解析的其他失败向调用方抛出，不静默伪装为无证据。
    """
    document = pymupdf.open(pdf_path)
    tagged = {}
    appended = {}
    try:
        scratch = document.get_new_xref()
        document.update_object(scratch, "<<>>")
        document.update_stream(scratch, b"")
        for page in document:
            forms = page.get_xobjects()
            if forms:
                _log("split_margin_evidence", page=page.number,
                     reason="form_internal_tags_not_scanned", count=len(forms))
            refs = page.get_contents()
            original = document.xref_get_key(page.xref, "Contents")[1]
            chunks = [document.xref_stream(ref) for ref in refs]
            starts = []
            size = 0
            for chunk in chunks:
                starts.append(size)
                size += len(chunk) + 1
            data = b"\n".join(chunks)
            try:
                operations = list(_operations(data))
            except ValueError as error:
                if str(error) != "inline_image_evidence_unsupported":
                    raise
                _log("split_margin_evidence", page=page.number,
                     reason="inline_image_evidence_unsupported", count=1)
                continue
            selected = set()
            stack = []
            groups = [[] for _ in refs]
            for index, (_, end, operator, operands) in enumerate(operations):
                group = bisect_right(starts, end - 1) - 1
                groups[group].append(index)
                if operator in ("BDC", "BMC"):
                    stack.append(operator == "BDC" and _pagination(operands, document, page))
                elif operator == "EMC":
                    if stack:
                        stack.pop()
                elif any(stack):
                    selected.add(index)
            try:
                if selected:
                    boxes, _ = _extract_boxes(document, page, scratch,
                                              _masked(data, operations, selected))
                    if boxes:
                        tagged[page.number] = boxes
                if len(refs) < 2:
                    continue
                measurements = []
                for group in groups:
                    text_ops = [operations[i][2] for i in group]
                    if not any(op in ("Tj", "TJ", "'", '"', "Do") for op in text_ops):
                        measurements.append(([], 0, False))
                        continue
                    boxes, count = _extract_boxes(document, page, scratch,
                                                  _masked(data, operations, set(group)))
                    before_text = []
                    for op in text_ops:
                        if op in ("Tj", "TJ", "'", '"'):
                            break
                        before_text.append(op)
                    independent = ("BT" in before_text and "Tf" in before_text and
                                   any(op in before_text for op in ("Tm", "Td", "TD")) and
                                   "Do" not in text_ops)
                    measurements.append((boxes, count, independent))
                tail_boxes = []
                tail_count = 0
                for index in range(len(refs) - 1, 0, -1):
                    boxes, count, independent = measurements[index]
                    if not count:
                        continue
                    if not independent:
                        break
                    candidate_boxes = boxes + tail_boxes
                    candidate_count = count + tail_count
                    if not any(body_count > candidate_count
                               for _, body_count, _ in measurements[:index]):
                        break
                    tail_boxes = candidate_boxes
                    tail_count = candidate_count
                if tail_boxes:
                    appended[page.number] = tail_boxes
                _log("split_margin_evidence", page=page.number,
                     reason="extracted", count=len(tagged.get(page.number, ())),
                     appended=len(tail_boxes))
            finally:
                document.xref_set_key(page.xref, "Contents", original)
        return tagged, appended
    finally:
        document.close()
