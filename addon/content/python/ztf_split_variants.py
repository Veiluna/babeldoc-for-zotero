"""T4 中文排版变体：过滤排版后的 IL 副本，交 BabelDOC 写出，供 T5 整页嵌入。

纯 IL 入口 create_variants 不执行 I/O，也不改传入的排版 IL。
install 在翻译前抽出图表对象，写出 mono/variants 时以原页矢量 Form 提供底图；
原 PDF 不修改。依赖 BabelDOC 0.6.4。
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
import ztf_timing as timing
from ztf_figure_background import (
    build_plan,
    inject_background_sequence,
    inject_backgrounds,
    remember,
    remove_selected_objects,
    remembered,
    plan_objects_removed,
)


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


def _install_paragraph_capture(finder, captured):
    """在 0.6.4 分段删空格前持有真实对象；分段后只按对象身份恢复归段。"""
    original = finder.process_page

    @functools.wraps(original)
    def process_page(instance, page, *args, **kwargs):
        try:
            glyphs = {id(c): c for c in page.pdf_character}
            for paragraph in page.pdf_paragraph:
                glyphs.update((id(c), c) for c in _paragraph_chars(paragraph))
        except Exception:
            glyphs = None
        result = original(instance, page, *args, **kwargs)
        try:
            owners = {id(c): i for i, p in enumerate(page.pdf_paragraph)
                      for c in _paragraph_chars(p)}
            if glyphs is not None:
                captured[id(page)] = (page, tuple((c, owners.get(identity, -1))
                                                for identity, c in glyphs.items()))
        except Exception:
            pass  # 翻译入口按页回退原取字路径，并记录 source_capture_missing。
        return result

    finder.process_page = process_page


def collect_source_chars(page: Any, convert_box: Callable | None = None,
                         captured=None) -> tuple[Char, ...]:
    """翻译前采集，保留真实 render/sub_render_order；同一字符只计一次。"""
    convert = convert_box or (lambda box: _page_box(page, box))
    glyphs = {id(char): (char, paragraph) for char, paragraph in (captured or ())}
    glyphs.update((id(char), (char, -1)) for char in page.pdf_character)
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


def object_region(bounds, figure_boxes, gutters):
    """图形随最小完整包含它的图表公式归属，否则按自身框；不扩框。"""
    containing = [box for box in figure_boxes if _inside(bounds, box)]
    if containing:
        bounds = min(containing, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    return _region(bounds, gutters)


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
        elif type(item).__name__ == "PdfCurve":
            # P05 图表页可含 267k 条曲线。曲线及路径是写出器只读的，
            # 只隔离其容器和 graphic state；Lazy 指令仍按原语义 materialize。
            detached = copy.copy(item)
            for name in ("pdf_path", "pdf_original_path", "ctm", "relocation_transform"):
                field_value = getattr(item, name, None)
                if isinstance(field_value, list):
                    setattr(detached, name, list(field_value))
            graphic_state = getattr(item, "graphic_state", None)
            if graphic_state is not None:
                graphic_state_copy = copy.copy(graphic_state)
                instruction = getattr(graphic_state, "passthrough_per_char_instruction", None)
                if isinstance(instruction, LazyPassthroughInstruction):
                    instruction_copy = copy.copy(instruction)
                    instruction_copy.snapshot = copy.copy(instruction.snapshot)
                    instruction_copy.snapshot._render_cache = dict(instruction.snapshot._render_cache)
                    graphic_state_copy.passthrough_per_char_instruction = instruction_copy.materialize()
                detached.graphic_state = graphic_state_copy
            memo[id(item)] = detached
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
    """为每个检测到的栏和 SPANNING 生成一份筛选后的 IL 页面。"""
    if columns.columns == 1:
        return ()
    if columns.split_paragraphs:
        raise ValueError("split_paragraph_unresolved")
    assignments = dict(columns.paragraph_column)
    char_columns = char_columns or {}
    convert = convert_box or (lambda box: _page_box(page, box))
    figure_boxes = [convert(item.box) for item in page.page_layout
                    if item.class_name in _FIGURE_LABELS]

    def own_region(item):
        return object_region(convert(item.box), figure_boxes, columns.gutters)

    all_forms = [*page.pdf_form, *_formula_forms(page.pdf_paragraph)]
    carriers = _form_carriers(page, all_forms)
    carrier_by_child = {child: all_forms[index] for index, child in carriers.items()}
    child_by_form = {id(all_forms[index]): child for index, child in carriers.items()}

    def owner(item):
        bounds = item.box
        if (getattr(item, "passthrough_paint", False)
                and bounds.x == bounds.y == bounds.x2 == bounds.y2 == 0):
            form = carrier_by_child.get(getattr(item, "xobj_id", None))
            return own_region(form) if form is not None else SPANNING
        return own_region(item)

    # 归属在页面级只计算一次；变体循环只读取缓存，避免同一批对象重复 owner。
    owner_cache = {}

    def cached_owner(item):
        identity = id(item)
        if identity not in owner_cache:
            owner_cache[identity] = owner(item)
        return owner_cache[identity]

    char_owner = {}
    for char in page.pdf_character:
        key = (char.render_order, char.sub_render_order, char.xobj_id)
        char_owner[id(char)] = (char_columns[key] if key in char_columns
                                else cached_owner(char))
    object_collections = ("pdf_xobject", "page_layout", "pdf_rectangle",
                          "pdf_paragraph", "pdf_figure", "pdf_character", "pdf_curve",
                          "pdf_form")

    def copy_page_shell():
        shell = copy.copy(page)
        for name in object_collections:
            if hasattr(shell, name):
                setattr(shell, name, [])
        return _copy_il(shell)

    result = []
    source_background = remembered(page)
    for column in (*range(columns.columns), SPANNING):
        indices = tuple(index for index in range(len(page.pdf_paragraph))
                        if assignments.get(index) == column)
        paragraphs = [page.pdf_paragraph[index] for index in indices]

        def is_margin(char):
            return char_columns.get((getattr(char, "render_order", None),
                                     getattr(char, "sub_render_order", None),
                                     getattr(char, "xobj_id", None))) == MARGIN

        selected_characters = [char for char in page.pdf_character
                               if char_owner[id(char)] == column]
        selected_curves = [item for item in page.pdf_curve if cached_owner(item) == column]
        selected_rectangles = [item for item in page.pdf_rectangle
                               if cached_owner(item) == column]
        selected_figures = [item for item in page.pdf_figure if cached_owner(item) == column]
        selected_layout = [item for item in page.page_layout if cached_owner(item) == column]
        forms = [(index, form) for index, form in enumerate(page.pdf_form)
                 if index not in carriers and cached_owner(form) == column]
        active = {getattr(item, "xobj_id", None) for item in
                  [*selected_characters, *selected_curves, *selected_rectangles,
                   *(form for _, form in forms)]}
        for paragraph in paragraphs:
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
                form = all_forms[index]
                if id(form) not in selected_formula_forms:
                    forms.append((index, form))
                active.add(form.xobj_id)
                del pending[index]
        selected_forms = [form for _, form in sorted(forms)]
        selected_xobjects = [item for item in page.pdf_xobject if item.xobj_id in active]

        # 只把本变体会用到的对象作为 deepcopy 根；页面壳的 mediabox/cropbox、字体等
        # 仍然独立复制，但不会把其他栏的图形对象带入复制图。
        roots = (paragraphs, selected_characters, selected_curves, selected_rectangles,
                 selected_figures, selected_layout, selected_forms, selected_xobjects)
        copied = _copy_il(roots)
        (copied_paragraphs, copied_characters, copied_curves, copied_rectangles,
         copied_figures, copied_layout, copied_forms, copied_xobjects) = copied
        variant = copy_page_shell()
        variant.pdf_paragraph = list(copied_paragraphs)
        variant.pdf_character = list(copied_characters)
        variant.pdf_curve = list(copied_curves)
        variant.pdf_rectangle = list(copied_rectangles)
        variant.pdf_figure = list(copied_figures)
        variant.page_layout = list(copied_layout)
        variant.pdf_form = list(copied_forms)
        variant.pdf_xobject = list(copied_xobjects)
        if source_background is not None:
            remember(variant, source_background.for_owner(column))

        # Margin 字符只从当前变体的副本中移除，避免修改 creator.docs IL。
        for paragraph in variant.pdf_paragraph:
            compositions = []
            for composition in paragraph.pdf_paragraph_composition:
                char = getattr(composition, "pdf_character", None)
                if char is not None and is_margin(char):
                    continue
                for name in ("pdf_line", "pdf_same_style_characters", "pdf_formula"):
                    container = getattr(composition, name, None)
                    if container is not None:
                        container.pdf_character = [c for c in container.pdf_character if not is_margin(c)]
                compositions.append(composition)
            paragraph.pdf_paragraph_composition = compositions
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


def _copy_render_page(page):
    """复制已筛选变体的 IL 字段，供写出器在隔离副本上修改。

    输入是一个已由 ``create_variants`` 筛选的 IL 页面；输出是保留页面
    元数据但深复制可变字段的独立页面。复制或 IL 对象结构不符合
    BabelDOC 0.6.4 约定时会直接抛出原始异常，不返回部分页面。
    """
    field_names = ("mediabox", "cropbox", "pdf_xobject", "page_layout",
                   "pdf_rectangle", "pdf_font", "pdf_paragraph", "pdf_figure",
                   "pdf_character", "pdf_curve", "pdf_form", "base_operations")
    roots = tuple(getattr(page, name, None) for name in field_names)
    copied = _copy_il(roots)
    isolated = copy.copy(page)
    for name, value in zip(field_names, copied):
        setattr(isolated, name, value)
    return isolated


def render_variants(creator: Any, variants: tuple[Variant, ...], output_pdf: str) -> None:
    """只复用 0.6.4 字体/内容流写出器，不调用 write，不重排、不改原文件。

    每份变体重新打开解析用 PDF，避免共享 xref/Form 流被上一份变体改写；
    保持原 page_number，隔离每次 Form 调用的流，恢复原页几何后整页复制。
    浅复制 writer 复用已加载的 FontMapper；只替换副本的 docs，不影响随后 mono。
    """
    import pymupdf
    from babeldoc.format.pdf.document_il import il_version_1 as il

    config = creator.translation_config
    with pymupdf.open() as output:
        for variant in variants:
            config.raise_if_cancelled()
            # FontMapper.add_font 会往 IL 中追加字体，副本不得污染待交付的 IL。
            document = il.Document(page=[_copy_render_page(variant.page)], total_pages=1)
            with timing.call('ztf_split_variants.open[render_variants]',
                             pymupdf.open, creator.original_pdf_path) as pdf:
                _isolate_form_streams(document.page[0], pdf)
                # 0.6.4 add_font 的 xref/PdfFont 均按 doc/IL 局部创建；mapper 只读。
                # 每份 writer 保留原配置/字体，docs 仍指向本次独立的 IL 副本。
                writer = copy.copy(creator)
                writer.docs = document
                timing.call('ztf_split_variants.add_font[render_variants]',
                            writer.font_mapper.add_font, pdf, document)
                timing.call('ztf_split_variants.update_page_content_stream[render_variants]',
                            writer.update_page_content_stream, False, document.page[0], pdf, config)
                timing.call('ztf_split_variants.restore_media_box[render_variants]',
                            writer.restore_media_box, pdf, creator.mediabox_data)
                number = document.page[0].page_number
                timing.call('ztf_split_variants.insert_pdf[render_variants]',
                            output.insert_pdf, pdf, from_page=number, to_page=number,
                            links=False, annots=False, widgets=False)
        timing.call('ztf_split_variants.save[render_variants]',
                    output.save, output_pdf, garbage=4, deflate=True)


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
    mono_plans = {}
    source_errors = {}
    captured_pages = {}
    capture_hook = False
    try:
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
        _install_paragraph_capture(ParagraphFinder, captured_pages)
        capture_hook = True
    except Exception:
        # 各页在 capture 中记录固定原因码并继续使用原取字路径。
        pass
    original_write = None

    def capture(document, source_pdf):
        tagged, appended = read_pdf_evidence(
            str(source_pdf), include_appended=timing.enabled()
        )
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
                    captured = captured_pages.pop(id(page), None)
                    if captured is None:
                        log({"event": "split_source_capture_fallback", "page": number,
                             "reason": "source_capture_missing" if capture_hook
                             else "source_capture_unavailable"})
                    chars = collect_source_chars(page, convert, captured[1] if captured else None)
                except Exception as error:  # noqa: BLE001 - 保留源页诊断，不中断其他页采集
                    reason = _error_reason(error)
                    source_errors[id(page)] = reason
                    log({"event": "split_variants_page_error", "stage": "source",
                         "page": number, "reason": reason, "count": 1})
                    continue
                lines = build_lines(number, chars)
                figures = tuple(convert(item.box) for item in page.page_layout
                                if item.class_name in _FIGURE_LABELS)
                plan = remembered(page)
                if plan is not None and not plan_objects_removed(page):
                    selected_ids = {id(item) for item in plan.selected_curves}
                    current_ids = {id(item) for item in (getattr(page, "pdf_curve", ()) or ())}
                    if current_ids and not selected_ids.intersection(current_ids):
                        plan = None
                entries.append((page, chars, lines, convert, figures,
                                original.cropbox.width, original.cropbox.height,
                                plan or build_plan(page, convert_box=convert)))
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
        for index, (page, chars, lines, convert, figures, width, height, plan) in enumerate(entries):
            columns = detect_columns(page.page_number, width, height, lines,
                                     margins[index], document_columns, figures)
            mid = width / 2
            groups = {char_id: group for line, group in zip(lines, columns.line_column)
                      if group in (MARGIN, SPANNING) for char_id in line.char_ids}
            groups.update({char.id: (0 if columns.columns == 1 or char.x1 <= mid else 1)
                           for char in chars if char.id not in groups})
            owners = {(char.order, char.sub_order, char.xobj): groups[char.id] for char in chars}
            plan = plan.with_gutters(columns.gutters)
            # 复用 runner 已采集的计划；按对象身份移除是幂等的。
            canonical_removed = plan_objects_removed(page)
            remove_selected_objects(page, plan)
            mono_plans[id(page)] = plan
            remember(page, plan, removed=canonical_removed)
            snapshots[id(page)] = (columns, owners, convert,
                                   tuple(id(p) for p in page.pdf_paragraph), plan)

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
                timing.patch(PDFCreater, 'write', 'ztf_split_variants')
            try:
                capture(document, instance.translation_config.input_file)
            except Exception as error:  # noqa: BLE001 - 变体诊断不能改变单语翻译路径
                log({"event": "split_variants_error", "stage": "source",
                     "error": _error_reason(error), "count": 1})
            finally:
                captured_pages.clear()
            return _original(instance, document, *args, **kwargs)

        translator.translate = translate

    def write(creator, translation_config, check_font_exists=False):
        started = time.perf_counter()
        variants = []
        pages = []
        variant_backgrounds = []
        variant_source_pages = []
        mono_backgrounds = {}
        for page in creator.docs.page:
            # 图表已移出 IL；单语底图不能依赖后续分栏检查或变体生成成功。
            plan = mono_plans.pop(id(page), None)
            if plan is not None:
                mono_backgrounds[page.page_number] = plan
            entry = {"source_page": page.page_number, "variants": []}
            try:
                if id(page) in source_errors:
                    entry["error"] = source_errors.pop(id(page))
                    pages.append(entry)
                    continue
                saved = snapshots.pop(id(page), None)
                if saved is None:
                    raise ValueError("source_snapshot_missing")
                if len(saved) == 5:
                    columns, owners, convert, paragraph_ids, plan = saved
                else:
                    columns, owners, convert, paragraph_ids = saved
                    plan = None
                if tuple(id(p) for p in page.pdf_paragraph) != paragraph_ids:
                    raise ValueError("paragraph_identity_changed")
                entry.update({"columns": columns.columns, "basis": columns.basis,
                              "gutters": columns.gutters,
                              "paragraph_column": columns.paragraph_column,
                              "split_paragraphs": columns.split_paragraphs})
                if timing.enabled():
                    entry["reasons"] = columns.reasons
                for variant in create_variants(page, columns, owners, convert_box=convert):
                    item = {"column": variant.column, "pdf_page": len(variants)}
                    if timing.enabled():
                        item["paragraphs"] = variant.paragraphs
                    entry["variants"].append(item)
                    variants.append(variant)
                    variant_backgrounds.append(remembered(variant.page))
                    variant_source_pages.append(page.page_number)
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
                    if variant_backgrounds:
                        inject_background_sequence(
                            str(temporary), str(creator.original_pdf_path),
                            variant_backgrounds, variant_source_pages,
                        )
                    rendered = True
                except Exception as error:  # noqa: BLE001 - 变体写出失败保留单语输出
                    log({"event": "split_variants_error", "stage": "render",
                         "error": type(error).__name__, "count": 1})
            elapsed = time.perf_counter() - started
            # 原写出器只接触原 document，变体从未被写回 creator.docs。
            result = timing.call('ztf_split_variants.original_write[write]', original_write,
                                 creator, translation_config, check_font_exists)
            if result.mono_pdf_path and mono_backgrounds:
                try:
                    inject_backgrounds(
                        str(result.mono_pdf_path), str(creator.original_pdf_path),
                        mono_backgrounds,
                        page_numbers=(tuple(sorted(
                            page.page_number for page in creator.docs.page
                            if translation_config.should_translate_page(page.page_number + 1)
                        )) if getattr(translation_config, "only_include_translated_page", False)
                            else None),
                    )
                except Exception as error:  # noqa: BLE001
                    log({"event": "figure_background_error", "stage": "mono",
                         "error": type(error).__name__, "count": 1})
            if result.mono_pdf_path:
                try:
                    pdf_path = str(result.mono_pdf_path) + ".variants.pdf"
                    if rendered:
                        temporary.replace(pdf_path)
                    metadata = {"version": 1, "pdf_path": pdf_path if rendered else None,
                                "pages": pages}
                    if timing.enabled():
                        metadata["render_seconds"] = round(elapsed, 4)
                    mapping = str(result.mono_pdf_path) + ".variants.json"
                    with timing.measure('ztf_split_variants.mapping_json[write]'):
                        Path(mapping).write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False),
                                                 encoding="utf-8")
                    event = {"event": "split_variants", "pages": len(pages),
                             "count": len(variants) if rendered else 0,
                             "mapping_path": mapping}
                    if "render_seconds" in metadata:
                        event["render_seconds"] = metadata["render_seconds"]
                    log(event)
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
