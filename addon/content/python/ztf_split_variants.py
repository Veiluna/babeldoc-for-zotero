"""T4 中文排版变体：过滤排版后的 IL 副本，交 BabelDOC 写出，供 T5 整页嵌入。

原 IL、原 PDF 和单语输出均不修改。纯 IL 入口 create_variants 不执行 I/O；
install 在翻译前采集字符，在 PDFCreater.write 前生成变体。依赖 BabelDOC 0.6.4。
"""

import copy
import functools
import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

# runner 也会通过 spec_from_file_location 加载 helper（此时不保证脚本目录在 sys.path）。
_DIRECTORY = str(Path(__file__).resolve().parent)
if _DIRECTORY not in sys.path:
    sys.path.insert(0, _DIRECTORY)

from ztf_split_types import MARGIN, SPANNING, Char, PageColumns


@dataclass(frozen=True)
class Variant:
    column: int
    page: Any
    paragraphs: tuple[int, ...]


def _paragraph_chars(paragraph):
    for composition in paragraph.pdf_paragraph_composition:
        char = getattr(composition, "pdf_character", None)
        if char is not None:
            yield char
        for name in ("pdf_line", "pdf_same_style_characters", "pdf_formula"):
            owner = getattr(composition, name, None)
            if owner is not None:
                yield from owner.pdf_character


def _page_box(page, box):
    """未恢复 MediaBox/CropBox 的 IL 用户空间 → 左上原点；不舍入认栏数据。"""
    crop, top = page.cropbox.box, page.mediabox.box.y2
    return (box.x - crop.x, top - box.y2 - crop.y,
            box.x2 - crop.x, top - box.y - crop.y)


def collect_source_chars(page: Any, convert_box: Callable | None = None) -> tuple[Char, ...]:
    """翻译前采集，保留真实 render/sub_render_order；同一字符只计一次。"""
    convert = convert_box or (lambda box: _page_box(page, box))
    glyphs = {id(char): (char, -1) for char in page.pdf_character}
    for index, paragraph in enumerate(page.pdf_paragraph):
        for char in _paragraph_chars(paragraph):
            glyphs[id(char)] = (char, index)
    chars = []
    for char, paragraph in glyphs.values():
        if char.render_order is None or char.sub_render_order is None:
            # layout_helper._add_space_dummy_chars_to_list / add_space_dummy_chars
            # 补的空格没有内容流顺序，不能拿它的宽框填满真实栏缝。
            if (char.char_unicode or "").isspace():
                continue
            raise ValueError("source_char_order_missing")
        bounds = convert(char.box)
        chars.append(Char(len(chars), char.render_order, char.sub_render_order,
                          *bounds, char.char_unicode or "", paragraph, char.xobj_id))
    return tuple(chars)


def _region(box, gutters):
    x0, _, x1, _ = box
    if any(x0 < right and x1 > left for left, right in gutters):
        return SPANNING
    return sum(x0 >= right for _, right in gutters)


def _inside(box, owner):
    return (owner[0] <= box[0] and owner[1] <= box[1]
            and box[2] <= owner[2] and box[3] <= owner[3])


_FIGURE_LABELS = frozenset(("figure", "table", "isolate_formula", "formula"))
_ERROR_REASONS = frozenset(("source_char_order_missing", "source_snapshot_missing",
                            "paragraph_identity_changed", "split_paragraph_unresolved",
                            "form_carrier_order_unresolved"))


def _error_reason(error):
    # 第三方异常消息可能包含正文/凭据；只保留本模块固定原因码。
    return (str(error) if type(error) is ValueError and str(error) in _ERROR_REASONS
            else type(error).__name__)


def _formula_forms(paragraphs):
    for paragraph in paragraphs:
        for composition in paragraph.pdf_paragraph_composition:
            if composition.pdf_formula is not None:
                yield from composition.pdf_formula.pdf_form


def _form_carriers(page, forms=None):
    # xref 标识资源；同一资源的调用按源内容流顺序配对。
    # 解析器的 xobj_id 与 Form.render_order 均随源调用递增；后者在公式搬移时保留，
    # 而 form.box 会被排版改变、Form 还可能在页级/公式列表间移动，不能用于身份匹配。
    xobjects = {}
    for item in page.pdf_xobject:
        xobjects.setdefault(item.xref_id, []).append(item.xobj_id)
    calls = {}
    for index, form in enumerate(page.pdf_form if forms is None else forms):
        ref = getattr(getattr(form, "pdf_form_subtype", None), "pdf_xobj_form", None)
        if ref is not None and ref.xref_id in xobjects:
            calls.setdefault(ref.xref_id, []).append((index, form))
    carriers = {}
    for xref, entries in calls.items():
        orders = [form.render_order for _, form in entries]
        if (len(entries) != len(xobjects[xref]) or None in orders
                or len(set(orders)) != len(orders)):
            raise ValueError("form_carrier_order_unresolved")
        for (index, _), child in zip(sorted(entries, key=lambda entry: entry[1].render_order),
                                     sorted(xobjects[xref])):
            carriers[index] = child
    return carriers


def _copy_il(value):
    """复制边界将 0.6.4 lazy 图形历史替换为有效指令，不改原对象及缓存。"""
    from babeldoc.format.pdf.document_il.frontend.il_creater_active_support import (
        LazyPassthroughInstruction,
    )

    memo, seen, pending = {}, set(), [value]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, LazyPassthroughInstruction):
            detached = copy.copy(item)
            detached.snapshot = copy.copy(item.snapshot)
            detached.snapshot._render_cache = dict(item.snapshot._render_cache)
            memo[id(item)] = detached.materialize()
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
        elif hasattr(item, "__dict__") and not isinstance(item, type):
            pending.extend(vars(item).values())
    return copy.deepcopy(value, memo)


def _column_count_tag(pdf):
    """读取结构树内的 ColumnCount；不从正文或页面字典猜测标签。"""
    import pymupdf

    native = pymupdf.mupdf
    document = native.pdf_document_from_fz_document(pdf.this)
    tree = native.pdf_dict_getp(native.pdf_trailer(document), "Root/StructTreeRoot")
    pending, seen = [tree], set()
    while pending:
        item = pending.pop()
        xref = native.pdf_to_num(item)
        if xref:
            if xref in seen:
                continue
            seen.add(xref)
        if native.pdf_is_array(item):
            pending.extend(native.pdf_array_get(item, i)
                           for i in reversed(range(native.pdf_array_len(item))))
        elif native.pdf_is_dict(item):
            count = native.pdf_dict_gets(item, "ColumnCount")
            if native.pdf_is_int(count) and native.pdf_to_int(count) in (1, 2):
                return native.pdf_to_int(count)
            # /P、/Pg 是父级和源页反向引用，不属于子结构或布局属性。
            for i in reversed(range(native.pdf_dict_len(item))):
                key = native.pdf_to_name(native.pdf_dict_get_key(item, i))
                if key not in ("P", "Pg"):
                    pending.append(native.pdf_dict_get_val(item, i))
    return None


def create_variants(
    page: Any, columns: PageColumns,
    char_columns: Mapping[tuple[int, int, int | None], int] | None = None,
    *, convert_box: Callable | None = None,
) -> tuple[Variant, ...]:
    """每栏及 SPANNING 各一份独立副本；一栏页直接使用单语页。"""
    if columns.columns == 1:
        return ()
    if columns.split_paragraphs:
        # T2 明确未给混组段落归属；译后不能按坐标猜中文的源字符归属。
        raise ValueError("split_paragraph_unresolved")
    assignments = dict(columns.paragraph_column)
    char_columns = char_columns or {}
    convert = convert_box or (lambda box: _page_box(page, box))
    figure_boxes = [convert(item.box) for item in page.page_layout
                    if item.class_name in _FIGURE_LABELS]

    def own_region(item):
        bounds = convert(item.box)
        containing = [box for box in figure_boxes if _inside(bounds, box)]
        if containing:
            bounds = min(containing, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
        return _region(bounds, columns.gutters)

    all_forms = [*page.pdf_form, *_formula_forms(page.pdf_paragraph)]
    carriers = _form_carriers(page, all_forms)

    # 普通 Form/图片/曲线的 box 已是页面用户空间，直接按自身框归属。
    # 本机 0.6.4 的 project_native_shading_paint 却为透传 shading 写入零占位框；
    # Chen 的 6 个绿色组件由这些 shading 写出，不能把零框当成左栏坐标。
    # 仅这类无空间范围的绘制随其载体归属；跨栏载体及无载体的未知范围保留 SPANNING。
    carrier_by_child = {child: all_forms[index] for index, child in carriers.items()}
    child_by_form = {id(all_forms[index]): child for index, child in carriers.items()}

    def owner(item):
        bounds = item.box
        if (getattr(item, "passthrough_paint", False)
                and bounds.x == bounds.y == bounds.x2 == bounds.y2 == 0):
            form = carrier_by_child.get(getattr(item, "xobj_id", None))
            return own_region(form) if form is not None else SPANNING
        return own_region(item)
    result = []
    for column in (*range(columns.columns), SPANNING):
        indices = tuple(index for index in range(len(page.pdf_paragraph))
                        if assignments.get(index) == column)
        variant = _copy_il(page)
        variant.pdf_paragraph = [variant.pdf_paragraph[index] for index in indices]
        variant.pdf_character = [char for char in variant.pdf_character
                                 if char_columns.get((char.render_order, char.sub_render_order,
                                                      char.xobj_id), owner(char)) == column]
        for name in ("pdf_curve", "pdf_rectangle", "pdf_figure", "page_layout"):
            setattr(variant, name, [item for item in getattr(variant, name)
                                    if owner(item) == column])
        forms = [(index, form) for index, form in enumerate(variant.pdf_form)
                 if index not in carriers and owner(form) == column]
        active = {getattr(item, "xobj_id", None) for item in
                  [*variant.pdf_character, *variant.pdf_curve, *variant.pdf_rectangle,
                   *(form for _, form in forms)]}
        for paragraph in variant.pdf_paragraph:
            active.update(char.xobj_id for char in _paragraph_chars(paragraph))
            for composition in paragraph.pdf_paragraph_composition:
                formula = composition.pdf_formula
                if formula is not None:
                    active.update(item.xobj_id for item in [*formula.pdf_form, *formula.pdf_curve])
        selected_formula_forms = {id(form) for form in _formula_forms(
            page.pdf_paragraph[index] for index in indices)}
        active.update(child_by_form[form] for form in selected_formula_forms if form in child_by_form)
        pending = dict(carriers)
        while pending:
            selected = [index for index, child in pending.items() if child in active]
            if not selected:
                break
            for index in selected:
                form = (variant.pdf_form[index] if index < len(page.pdf_form)
                        else _copy_il(all_forms[index]))
                if id(all_forms[index]) not in selected_formula_forms:
                    forms.append((index, form))
                active.add(form.xobj_id)
                del pending[index]
        variant.pdf_form = [form for _, form in sorted(forms)]
        # 写出器逐项重写 XObject 流；未激活的同 xref 实例会以空流覆盖已选实例。
        variant.pdf_xobject = [item for item in variant.pdf_xobject if item.xobj_id in active]
        result.append(Variant(column, variant, indices))
    return tuple(result)


def _isolate_form_streams(page, pdf):
    """只修改渲染副本：每次 Form 调用使用独立流和资源名，保持原页面位置。"""
    from babeldoc.format.pdf.document_il.utils.zstd_helper import zstd_compress
    from babeldoc.format.pdf.new_parser.base_operations import compute_xobject_end_operation

    forms = [*page.pdf_form, *_formula_forms(page.pdf_paragraph)]
    carriers = _form_carriers(page, forms)
    xobjects = {item.xobj_id: item for item in page.pdf_xobject}
    for index, child in carriers.items():
        form, xobject = forms[index], xobjects[child]
        source = xobject.xref_id
        kind, value = pdf.xref_get_key(source, "Matrix")
        if kind == "xref":
            value = pdf.xref_object(int(value.split()[0]))
        matrix = ((1, 0, 0, 1, 0, 0) if kind == "null"
                  else tuple(float(n) for n in value.strip("[]").split()))
        # 本机解析器用 XObject 名称路径保存逆 CTM，重复同名调用会覆盖前次。
        # 按本次调用的 Matrix × CTM 重算，避免筛选后沿用最后一次调用的逆矩阵。
        xobject.base_operations.value = zstd_compress(
            compute_xobject_end_operation(matrix, form.ctm))
        target = pdf.get_new_xref()
        # 整份复制 PDF 字典，保留字符串字段的 PDF 转义（如 PTEX.FileName）。
        pdf.update_object(target, pdf.xref_object(source))
        pdf.update_stream(target, pdf.xref_stream(source))
        xobject.xref_id = target
        form.pdf_form_subtype.pdf_xobj_form.xref_id = target

    for index, child in carriers.items():
        form = forms[index]
        target = xobjects[child].xref_id
        parent = xobjects.get(form.xobj_id)
        owner = parent.xref_id if parent else pdf[page.page_number].xref
        # Resources / XObject 都可能是间接字典；只添加唯一新名字，保留共享字典旧键。
        prefix = ""
        for part in ("Resources", "XObject"):
            prefix += part + "/"
            kind, value = pdf.xref_get_key(owner, prefix[:-1])
            if kind == "xref":
                owner, prefix = int(value.split()[0]), ""
        name = f"ZTFVariant{target}"
        while pdf.xref_get_key(owner, prefix + name)[0] != "null":
            name += "_"
        pdf.xref_set_key(owner, prefix + name, f"{target} 0 R")
        form.pdf_form_subtype.pdf_xobj_form.do_args = name


def render_variants(creator: Any, variants: tuple[Variant, ...], output_pdf: str) -> None:
    """只复用 0.6.4 字体/内容流写出器，不调用 write，不重排、不改原文件。

    每份变体重新打开解析用 PDF，避免共享 xref/Form 流被上一份变体改写；
    保持原 page_number，隔离每次 Form 调用的流，恢复原页几何后整页复制。
    """
    import pymupdf
    from babeldoc.format.pdf.document_il import il_version_1 as il
    from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater

    config = creator.translation_config
    with pymupdf.open() as output:
        for variant in variants:
            config.raise_if_cancelled()
            # FontMapper.add_font 会往 IL 中追加字体，副本不得污染待交付的 IL。
            document = il.Document(page=[_copy_il(variant.page)], total_pages=1)
            with pymupdf.open(creator.original_pdf_path) as pdf:
                _isolate_form_streams(document.page[0], pdf)
                writer = PDFCreater(creator.original_pdf_path, document, config,
                                    creator.mediabox_data)
                writer.font_mapper.add_font(pdf, document)
                writer.update_page_content_stream(False, document.page[0], pdf, config)
                writer.restore_media_box(pdf, creator.mediabox_data)
                number = document.page[0].page_number
                output.insert_pdf(pdf, from_page=number, to_page=number,
                                  links=False, annots=False, widgets=False)
        output.save(output_pdf, garbage=4, deflate=True)


def install(log: Callable[[dict], None]) -> None:
    """由 runner 的单个挂接点安装；调用方只在 ZTF_SPLIT_DUAL=1 时调用。

    sidecar: <mono.pdf>.variants.json，pdf_path + 0 起源页/变体页/栏号映射，
    SPANNING=-2；含 T2 依据与段落下标，不含正文。异常仅记录固定原因码/类型与页号，
    未决混组段落报 page.error，留给 T5 的单页安全网，不伪造 SPANNING。
    """
    import pymupdf
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
        ILTranslatorLLMOnly,
    )
    from ztf_split_columns import detect_columns, detect_document_columns
    from ztf_split_lines import build_lines
    from ztf_split_margins import classify_margins, read_pdf_evidence

    snapshots = {}
    source_errors = {}
    original_write = None

    def capture(document, source_pdf):
        tagged, appended = read_pdf_evidence(str(source_pdf))
        entries = []
        with pymupdf.open(source_pdf) as source:
            column_count_tag = _column_count_tag(source)
            for page in document.page:
                number = page.page_number
                original = source[number]
                matrix = original.transformation_matrix

                def convert(box, _matrix=matrix):
                    return tuple(pymupdf.Rect(box.x, box.y, box.x2, box.y2) * _matrix)

                try:
                    chars = collect_source_chars(page, convert)
                except Exception as error:  # noqa: BLE001 - 保留源页诊断，不中断其他页采集
                    reason = _error_reason(error)
                    source_errors[id(page)] = reason
                    log({"event": "split_variants_page_error", "stage": "source",
                         "page": number, "reason": reason, "count": 1})
                    continue
                lines = build_lines(number, chars)
                figures = tuple(convert(item.box) for item in page.page_layout
                                if item.class_name in _FIGURE_LABELS)
                entries.append((page, chars, lines, convert, figures,
                                original.cropbox.width, original.cropbox.height))
        # classify_margins 按传入序列下标寻址，证据按真实源页号重映射。
        margins = classify_margins(
            [entry[2] for entry in entries], [entry[6] for entry in entries],
            {i: tagged.get(entry[0].page_number, ()) for i, entry in enumerate(entries)},
            {i: appended.get(entry[0].page_number, ()) for i, entry in enumerate(entries)},
        )
        document_columns = detect_document_columns(
            [e[2] for e in entries], [e[5] for e in entries], [e[6] for e in entries],
            margins, {i: e[4] for i, e in enumerate(entries)},
            column_count_tag,
        )
        for index, (page, chars, lines, convert, figures, width, height) in enumerate(entries):
            columns = detect_columns(page.page_number, width, height, lines,
                                     margins[index], document_columns, figures)
            mid = width / 2
            groups = {char_id: group for line, group in zip(lines, columns.line_column)
                      if group in (MARGIN, SPANNING) for char_id in line.char_ids}
            groups.update({char.id: (0 if columns.columns == 1 or char.x1 <= mid else 1)
                           for char in chars if char.id not in groups})
            owners = {(char.order, char.sub_order, char.xobj): groups[char.id] for char in chars}
            snapshots[id(page)] = (columns, owners, convert,
                                   tuple(id(p) for p in page.pdf_paragraph))

    for translator in (ILTranslator, ILTranslatorLLMOnly):
        original_translate = getattr(translator, "translate", None)
        if original_translate is None:
            continue

        @functools.wraps(original_translate)
        def translate(instance, document, *args, _original=original_translate, **kwargs):
            nonlocal original_write
            # 只有真正开始翻译时才接入后端，安装本身不加载 PDF 写出器。
            if original_write is None:
                from babeldoc.format.pdf.document_il.backend.pdf_creater import (
                    PDFCreater,
                )

                original_write = PDFCreater.write
                PDFCreater.write = functools.wraps(original_write)(write)
            try:
                capture(document, instance.translation_config.input_file)
            except Exception as error:  # noqa: BLE001 - 变体诊断不能改变单语翻译路径
                log({"event": "split_variants_error", "stage": "source",
                     "error": _error_reason(error), "count": 1})
            return _original(instance, document, *args, **kwargs)

        translator.translate = translate

    def write(creator, translation_config, check_font_exists=False):
        started = time.perf_counter()
        variants = []
        pages = []
        for page in creator.docs.page:
            entry = {"source_page": page.page_number, "variants": []}
            try:
                if id(page) in source_errors:
                    entry["error"] = source_errors.pop(id(page))
                    pages.append(entry)
                    continue
                saved = snapshots.pop(id(page), None)
                if saved is None:
                    raise ValueError("source_snapshot_missing")
                columns, owners, convert, paragraph_ids = saved
                if tuple(id(p) for p in page.pdf_paragraph) != paragraph_ids:
                    raise ValueError("paragraph_identity_changed")
                entry.update({"columns": columns.columns, "basis": columns.basis,
                              "gutters": columns.gutters, "reasons": columns.reasons,
                              "paragraph_column": columns.paragraph_column,
                              "split_paragraphs": columns.split_paragraphs})
                for variant in create_variants(page, columns, owners, convert_box=convert):
                    entry["variants"].append({"column": variant.column,
                                              "pdf_page": len(variants),
                                              "paragraphs": variant.paragraphs})
                    variants.append(variant)
            except Exception as error:  # noqa: BLE001 - 显式记录单页安全网所需原因
                # 只发固定原因码，第三方异常文本可能包含文档数据。
                entry["error"] = _error_reason(error)
                log({"event": "split_variants_page_error", "page": page.page_number,
                     "reason": entry["error"], "count": 1})
            pages.append(entry)
        temporary = Path(translation_config.get_working_file_path("split-variants.pdf"))
        rendered = False
        try:
            if variants:
                try:
                    render_variants(creator, tuple(variants), str(temporary))
                    rendered = True
                except Exception as error:  # noqa: BLE001 - 变体写出失败保留单语输出
                    log({"event": "split_variants_error", "stage": "render",
                         "error": type(error).__name__, "count": 1})
            elapsed = time.perf_counter() - started
            # 原写出器只接触原 document，变体从未被写回 creator.docs。
            result = original_write(creator, translation_config, check_font_exists)
            if result.mono_pdf_path:
                try:
                    pdf_path = str(result.mono_pdf_path) + ".variants.pdf"
                    if rendered:
                        temporary.replace(pdf_path)
                    metadata = {"version": 1, "pdf_path": pdf_path if rendered else None,
                                "render_seconds": round(elapsed, 4), "pages": pages}
                    mapping = str(result.mono_pdf_path) + ".variants.json"
                    Path(mapping).write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False),
                                             encoding="utf-8")
                    log({"event": "split_variants", "pages": len(pages),
                         "count": len(variants) if rendered else 0,
                         "render_seconds": metadata["render_seconds"], "mapping_path": mapping})
                except Exception as error:  # noqa: BLE001 - sidecar 失败不覆盖单语结果
                    log({"event": "split_variants_error", "stage": "sidecar",
                         "error": type(error).__name__, "count": 1})
            return result
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                log({"event": "split_variants_error", "stage": "cleanup",
                     "error": type(error).__name__, "count": 1})
