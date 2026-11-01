"""Build and render vector backgrounds for figure and table regions.

The module has two deliberately separate parts.  The first part only inspects
BabelDOC IL objects and removes objects that are copied from the source PDF.
The second part is an optional PyMuPDF post-processing step: it makes a
filtered source page, imports it as a Form XObject, and prepends that Form to
the BabelDOC page stream.  Keeping the selection logic independent makes the
ownership and duplicate-rendering rules testable without importing BabelDOC.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import functools
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ztf_split_types import SPANNING


FIGURE_CLASSES = frozenset(("figure", "table"))


def _bounds(value: Any) -> tuple[float, float, float, float] | None:
    """Return a finite ``(x0, y0, x1, y1)`` tuple from an IL box-like value."""
    box = getattr(value, "box", value)
    if box is None:
        return None
    try:
        result = tuple(float(getattr(box, name)) for name in ("x", "y", "x2", "y2"))
    except (AttributeError, TypeError, ValueError):
        try:
            result = tuple(float(item) for item in box)
        except (TypeError, ValueError):
            return None
    if len(result) != 4 or not all(math.isfinite(item) for item in result):
        return None
    x0, y0, x1, y1 = result
    if x1 < x0 or y1 < y0:
        return None
    return result


def _contains(container: tuple[float, float, float, float],
              item: tuple[float, float, float, float]) -> bool:
    return (container[0] <= item[0] and container[1] <= item[1]
            and item[2] <= container[2] and item[3] <= container[3])


def _overlaps(first: tuple[float, float, float, float],
              second: tuple[float, float, float, float]) -> bool:
    """Use closed rectangles so a curve touching a figure edge is selected."""
    return not (first[2] < second[0] or second[2] < first[0]
                or first[3] < second[1] or second[3] < first[1])


def _figure_boxes(page: Any, convert_box: Callable[[Any], Any] | None = None
                  ) -> tuple[tuple[float, float, float, float], ...]:
    convert = convert_box or (lambda value: value)
    result = []
    for layout in getattr(page, "page_layout", ()) or ():
        if getattr(layout, "class_name", None) not in FIGURE_CLASSES:
            continue
        bounds = _bounds(convert(getattr(layout, "box", layout)))
        if bounds is not None:
            result.append(bounds)
    return tuple(result)


def figure_boxes(page: Any, convert_box: Callable[[Any], Any] | None = None
                 ) -> tuple[tuple[float, float, float, float], ...]:
    """Return BabelDOC ``figure``/``table`` layout boxes in page coordinates."""
    return _figure_boxes(page, convert_box)


def _inside_figure(value: Any, figures: tuple[tuple[float, float, float, float], ...],
                   convert_box: Callable[[Any], Any] | None = None) -> bool:
    bounds = _bounds((convert_box or (lambda item: item))(getattr(value, "box", value)))
    return bounds is not None and any(_contains(figure, bounds) for figure in figures)


def _chart_paint(bounds: tuple[float, float, float, float] | None,
                 figures: tuple[tuple[float, float, float, float], ...],
                 canvas: tuple[float, float, float, float] | None) -> bool:
    """局部图形允许 BBox 留白跨边；覆盖整页的载体保留在 IL。"""
    if bounds is None or canvas is not None and _contains(bounds, canvas):
        return False
    return any(_overlaps(bounds, figure) for figure in figures)


def _iter_char_containers(paragraph: Any) -> Iterable[Any]:
    for composition in getattr(paragraph, "pdf_paragraph_composition", ()) or ():
        direct = getattr(composition, "pdf_character", None)
        if direct is not None:
            yield composition
        for name in ("pdf_line", "pdf_same_style_characters", "pdf_formula"):
            child = getattr(composition, name, None)
            if child is not None and hasattr(child, "pdf_character"):
                yield child


def _iter_paragraph_chars(paragraph: Any) -> Iterable[Any]:
    seen = set()
    for container in _iter_char_containers(paragraph):
        value = getattr(container, "pdf_character", ())
        chars = (value,) if value is not None and not isinstance(value, (list, tuple)) else (value or ())
        for char in chars:
            if id(char) not in seen:
                seen.add(id(char))
                yield char


def _all_page_chars(page: Any) -> tuple[Any, ...]:
    result = []
    seen = set()
    for char in getattr(page, "pdf_character", ()) or ():
        if id(char) not in seen:
            seen.add(id(char))
            result.append(char)
    for paragraph in getattr(page, "pdf_paragraph", ()) or ():
        for char in _iter_paragraph_chars(paragraph):
            if id(char) not in seen:
                seen.add(id(char))
                result.append(char)
    return tuple(result)


def _owner(bounds: tuple[float, float, float, float],
           figures: tuple[tuple[float, float, float, float], ...],
           gutters: tuple[tuple[float, float], ...]) -> int:
    containing = [figure for figure in figures if _contains(figure, bounds)]
    if containing:
        bounds = min(containing, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    if any(bounds[0] < right and bounds[2] > left for left, right in gutters):
        return SPANNING
    return sum(bounds[0] >= right for _, right in gutters)


@dataclass(frozen=True)
class BackgroundPlan:
    """Selected IL objects and the exact rectangular union used for clipping."""

    figure_boxes: tuple[tuple[float, float, float, float], ...]
    selected_curves: tuple[Any, ...] = ()
    selected_characters: tuple[Any, ...] = ()
    selected_figures: tuple[Any, ...] = ()
    selected_forms: tuple[Any, ...] = ()
    selected_xobjects: tuple[Any, ...] = ()
    clip_regions: tuple[tuple[float, float, float, float], ...] = ()
    gutters: tuple[tuple[float, float], ...] = ()
    _owner_cache: dict[int, int] = field(default_factory=dict, compare=False, repr=False)
    _converted_bounds: dict[int, tuple[float, float, float, float]] = field(default_factory=dict, compare=False, repr=False)
    excluded_figure_boxes: tuple[tuple[float, float, float, float], ...] = ()

    def owner(self, item: Any) -> int:
        identity = id(item)
        cached = self._owner_cache.get(identity)
        if cached is not None:
            return cached
        bounds = self._converted_bounds.get(identity, _bounds(item))
        if bounds is None:
            result = SPANNING
        else:
            overlapping = tuple(figure for figure in self.figure_boxes if _overlaps(bounds, figure))
            if any(bounds[0] < right and bounds[2] > left for left, right in self.gutters):
                result = SPANNING
            elif overlapping:
                # 跨出图框的边框/填充也继承整块图的 owner；不能因自身
                # 外接框位于左半页，就把跨栏图的一半另放进左栏变体。
                figure = min(overlapping, key=lambda r: (r[2]-r[0])*(r[3]-r[1]))
                result = _owner(figure, (), self.gutters)
            else:
                result = _owner(bounds, self.clip_regions, self.gutters)
        self._owner_cache[identity] = result
        return result

    def for_owner(self, owner: int) -> "BackgroundPlan":
        """Return one column's objects and its matching clip rectangles."""
        def keep(item: Any) -> bool:
            return self.owner(item) == owner

        selected = tuple(item for item in self.selected_curves if keep(item))
        selected_chars = tuple(item for item in self.selected_characters if keep(item))
        selected_figures = tuple(item for item in self.selected_figures if keep(item))
        selected_forms = tuple(item for item in self.selected_forms if keep(item))
        selected_xobjects = tuple(item for item in self.selected_xobjects if keep(item))
        figures = tuple(region for region in self.figure_boxes
                        if _owner(region, (), self.gutters) == owner)
        regions = list(figures)
        for item in selected:
            bounds = self._converted_bounds.get(id(item), _bounds(item))
            if bounds in self.clip_regions and bounds not in regions:
                regions.append(bounds)
        return BackgroundPlan(
            figures, selected, selected_chars, selected_figures,
            selected_forms, selected_xobjects, tuple(regions), tuple(self.gutters),
            _converted_bounds=self._converted_bounds,
            excluded_figure_boxes=tuple(region for region in self.figure_boxes if region not in figures),
        )

    def with_gutters(self, gutters: tuple[tuple[float, float], ...]) -> "BackgroundPlan":
        """Return the same selection with the final split-column gutters."""
        return replace(self, gutters=tuple(gutters), _owner_cache={})


def build_plan(page: Any, gutters: tuple[tuple[float, float], ...] = (),
               convert_box: Callable[[Any], Any] | None = None) -> BackgroundPlan:
    """Select chart curves, text, images and Forms without touching formula curves."""
    figures = _figure_boxes(page, convert_box)
    if not figures:
        return BackgroundPlan((), gutters=tuple(gutters))
    convert = convert_box or (lambda value: value)
    curves = tuple(curve for curve in getattr(page, "pdf_curve", ()) or ()
                   if (bounds := _bounds(convert(getattr(curve, "box", curve))))
                   is not None and any(_overlaps(bounds, figure) for figure in figures))
    characters = tuple(char for char in _all_page_chars(page)
                       if _inside_figure(char, figures, convert_box))
    canvas = getattr(page, "cropbox", None) or getattr(page, "mediabox", None)
    canvas = _bounds(convert(getattr(canvas, "box", canvas))) if canvas is not None else None
    figures_in = tuple(item for item in getattr(page, "pdf_figure", ()) or ()
                       if _chart_paint(_bounds(convert(getattr(item, "box", item))), figures, canvas))
    forms = tuple(item for item in getattr(page, "pdf_form", ()) or ()
                  if _chart_paint(_bounds(convert(getattr(item, "box", item))), figures, canvas))
    removed_xobj_ids = {getattr(item, "xobj_id", None) for item in (*figures_in, *forms)}
    # ``pdf_xobject`` entries are resource carriers, not independent paint
    # operations.  Removing a carrier merely because its reported box is in a
    # chart can invalidate a Form invocation whose visible child extends
    # outside that box.  Remove carriers only when the corresponding Form or
    # figure object was selected above.
    xobjects = tuple(item for item in getattr(page, "pdf_xobject", ()) or ()
                     if getattr(item, "xobj_id", None) in removed_xobj_ids)
    regions = list(figures)
    for curve in curves:
        bounds = _bounds(convert(getattr(curve, "box", curve)))
        if bounds is not None and not any(_contains(figure, bounds) for figure in figures):
            if bounds not in regions:
                regions.append(bounds)
    # 跨框线的额外区域内已有的其他曲线也由底图绘制，避免在 IL 再画一次。
    curves = tuple(curve for curve in getattr(page, "pdf_curve", ()) or ()
                   if (bounds := _bounds(convert(getattr(curve, "box", curve)))) is not None
                   and (any(_overlaps(bounds, figure) for figure in figures)
                        or any(_contains(region, bounds) for region in regions)))
    converted_bounds = {
        id(item): _bounds(convert(getattr(item, 'box', item)))
        for item in (*curves, *characters, *figures_in, *forms, *xobjects)
    }
    return BackgroundPlan(
        figures, curves, characters, figures_in, forms, xobjects,
        tuple(regions), tuple(gutters),
        _converted_bounds=converted_bounds,
    )


def _filter_container(container: Any, selected: set[int]) -> None:
    chars = getattr(container, "pdf_character", None)
    if chars is not None:
        if isinstance(chars, (list, tuple)):
            container.pdf_character = [char for char in chars if id(char) not in selected]
        elif id(chars) in selected:
            container.pdf_character = None


def remove_selected_objects(page: Any, plan: BackgroundPlan) -> None:
    """Remove exactly the selected IL objects; leave formulas and other objects intact."""
    if not plan.figure_boxes:
        return
    selected_chars = {id(item) for item in plan.selected_characters}
    selected_curves = {id(item) for item in plan.selected_curves}
    selected_figures = {id(item) for item in plan.selected_figures}
    selected_forms = {id(item) for item in plan.selected_forms}
    selected_xobjects = {id(item) for item in plan.selected_xobjects}
    if hasattr(page, "pdf_curve"):
        page.pdf_curve = [item for item in page.pdf_curve if id(item) not in selected_curves]
    if hasattr(page, "pdf_character"):
        page.pdf_character = [item for item in page.pdf_character if id(item) not in selected_chars]
    if hasattr(page, "pdf_figure"):
        page.pdf_figure = [item for item in page.pdf_figure if id(item) not in selected_figures]
    if hasattr(page, "pdf_form"):
        page.pdf_form = [item for item in page.pdf_form if id(item) not in selected_forms]
    if hasattr(page, "pdf_xobject"):
        page.pdf_xobject = [item for item in page.pdf_xobject if id(item) not in selected_xobjects]
    affected_paragraphs = set()
    for paragraph in getattr(page, "pdf_paragraph", ()) or ():
        if any(id(char) in selected_chars for char in _iter_paragraph_chars(paragraph)):
            affected_paragraphs.add(id(paragraph))
        for container in _iter_char_containers(paragraph):
            _filter_container(container, selected_chars)
    # 栏归属按源段落下标保存。保留空壳及对象身份，避免正文下标前移。
    for paragraph in getattr(page, 'pdf_paragraph', ()) or ():
        if id(paragraph) in affected_paragraphs and not tuple(_iter_paragraph_chars(paragraph)):
            has_formula = any(getattr(comp, 'pdf_formula', None) is not None
                              for comp in paragraph.pdf_paragraph_composition)
            if not has_formula and hasattr(paragraph, 'unicode'):
                paragraph.unicode = ''


def clone_for_owner(plan: BackgroundPlan, owner: int) -> BackgroundPlan:
    """Alias used by the split-variant writer."""
    return plan.for_owner(owner)


# A small registry avoids adding fields to BabelDOC's generated slot dataclasses.
_PLANS: dict[int, BackgroundPlan] = {}


def remember(page: Any, plan: BackgroundPlan) -> None:
    _PLANS[id(page)] = plan


def remembered(page: Any) -> BackgroundPlan | None:
    return _PLANS.get(id(page))


def forget(page: Any) -> None:
    _PLANS.pop(id(page), None)


def _difference_rects(page_rect: tuple[float, float, float, float],
                     keep: Iterable[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """Partition a page into rectangles outside a union of keep rectangles."""
    pieces = [page_rect]
    for raw in keep:
        x0, y0, x1, y1 = raw
        next_pieces = []
        for px0, py0, px1, py1 in pieces:
            ix0, iy0, ix1, iy1 = max(px0, x0), max(py0, y0), min(px1, x1), min(py1, y1)
            if ix1 < ix0 or iy1 < iy0:
                next_pieces.append((px0, py0, px1, py1))
                continue
            for candidate in ((px0, py0, px1, iy0), (px0, iy1, px1, py1),
                              (px0, iy0, ix0, iy1), (ix1, iy0, px1, iy1)):
                if candidate[2] > candidate[0] and candidate[3] > candidate[1]:
                    next_pieces.append(candidate)
        pieces = next_pieces
    return pieces


def _redact_outside(source_page: Any, plan: BackgroundPlan) -> None:
    """Keep only the vector/text union in a copied source page."""
    # Remove source text outside figure boxes first.  This is separate from
    # graphics redaction so text in a crossing curve's extra rectangle does not
    # leak into the background Form.
    for block in source_page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            rect = tuple(float(v) for v in line.get("bbox", ()))
            if len(rect) == 4 and not any(_contains(figure, rect) for figure in plan.figure_boxes):
                source_page.add_redact_annot(rect, fill=False)
    source_page.apply_redactions(images=0, graphics=0, text=0)
    # 只物理删除框外文字；图形/图片由最后的精确并集剪裁排除，
    # 保持嵌套 Form、渐变和透明度指令原样，不再改写图形对象。


def clip_source_layer_outside_figures(page: Any,
                                     figures: Iterable[tuple[float, float, float, float]]) -> None:
    """让源英文/页边层避开跨栏底图，防止源 Form 的白色填充盖图。"""
    figures = tuple(figures)
    if not figures:
        return
    rectangles = _difference_rects(tuple(page.rect), figures)
    height = float(page.rect.height)
    prefix = b"q\n" + b" ".join(
        f"{x0:g} {height-y1:g} {x1-x0:g} {y1-y0:g} re".encode()
        for x0, y0, x1, y1 in rectangles
    ) + b" W n\n"
    document = page.parent
    refs = []
    for data in (prefix, b"\nQ\n"):
        xref = document.get_new_xref()
        document.update_object(xref, "<<>>")
        document.update_stream(xref, data)
        refs.append(xref)
    contents = [refs[0], *page.get_contents(), refs[1]]
    document.xref_set_key(page.xref, "Contents",
                          "[" + " ".join(f"{xref} 0 R" for xref in contents) + "]")


def install_source_layer_guard(compose_helper: Any) -> None:
    """由 runner 在加载 split-compose 后安装；只排除已有 SPANNING 图框。

    读取现有 layout 与 variants sidecar。单栏/无图页不改，局部栏图仍由
    现有英文 owner 规则绘制。临时包装源层创建函数，合成结束必恢复。
    """
    if getattr(compose_helper, "_ztf_figure_background_guard", False):
        return
    original = compose_helper.compose_split_document

    @functools.wraps(original)
    def compose(source_pdf, mono_pdf, layout, output_pdf, **kwargs):
        sidecar = Path(str(mono_pdf) + ".variants.json")
        entries = (json.loads(sidecar.read_text(encoding="utf-8")).get("pages", ())
                   if sidecar.is_file() else ())
        entries = {entry["source_page"]: entry for entry in entries}
        regions = {}
        for page in layout.get("pages", ()):
            entry = entries.get(page["index"], {})
            if entry.get("columns", 1) <= 1:
                continue
            gutters = tuple(tuple(pair) for pair in entry.get("gutters", ()))
            regions[page["index"]] = tuple(
                tuple(item["box"]) for item in page.get("layouts", ())
                if item.get("class") in FIGURE_CLASSES
                and _owner(tuple(item["box"]), (), gutters) == SPANNING)
        original_layer = compose_helper._layer

        def layer(source, number, *args, **layer_kwargs):
            result = original_layer(source, number, *args, **layer_kwargs)
            try:
                clip_source_layer_outside_figures(result[0], regions.get(number, ()))
                return result
            except Exception:
                result.close()
                raise

        compose_helper._layer = layer
        try:
            return original(source_pdf, mono_pdf, layout, output_pdf, **kwargs)
        finally:
            compose_helper._layer = original_layer

    compose_helper.compose_split_document = compose
    compose_helper._ztf_figure_background_guard = True


def _prepend_filtered_form(target_page: Any, filtered: Any,
                           plan: BackgroundPlan) -> bool:
    import pymupdf

    before = list(target_page.get_contents() or ())
    target_page.show_pdf_page(target_page.rect, filtered, 0, clip=target_page.rect)
    after = list(target_page.get_contents() or ())
    added = [xref for xref in after if xref not in before]
    if not added:
        return False
    xref = added[-1]
    stream = target_page.parent.xref_stream(xref) or b""
    height = float(target_page.rect.height)
    # IL/source boxes and PyMuPDF rectangles use a top-left origin.  Raw PDF
    # content streams use a bottom-left origin, so flip y when writing the
    # explicit clipping path around the imported Form.
    path = b"q\n" + b" ".join(
        f"{x0:g} {height - y1:g} {x1 - x0:g} {y1 - y0:g} re".encode()
        for x0, y0, x1, y1 in plan.clip_regions
    ) + b" W n\n" + stream + b"\nQ\n"
    target_page.parent.update_stream(xref, path)
    refs = "[" + " ".join(f"{item} 0 R" for item in [*added, *before]) + "]"
    target_page.parent.xref_set_key(target_page.xref, "Contents", refs)
    return True


def prepend_background_form(target_page: Any, source_document: Any,
                            source_page_number: int, plan: BackgroundPlan) -> bool:
    """Import one filtered source page as a vector Form below target contents."""
    if not plan.clip_regions:
        return False
    import pymupdf

    filtered = pymupdf.open()
    try:
        filtered.insert_pdf(source_document, from_page=source_page_number, to_page=source_page_number)
        source_page = filtered[0]
        _redact_outside(source_page, plan)
        return _prepend_filtered_form(target_page, filtered, plan)
    finally:
        filtered.close()


def inject_backgrounds(output_path: str, source_path: str,
                       entries: Mapping[int, BackgroundPlan] | Iterable[tuple[int, BackgroundPlan]],
                       *, page_numbers: Iterable[int] | None = None) -> int:
    """Inject backgrounds into an output PDF and return the number of pages changed."""
    import pymupdf

    mapping = dict(entries)
    changed = 0
    temporary = output_path + ".figure-background.tmp"
    output = pymupdf.open(output_path)
    source = pymupdf.open(source_path)
    try:
        numbers = list(page_numbers) if page_numbers is not None else list(range(len(output)))
        for output_index, source_number in enumerate(numbers):
            if output_index >= len(output):
                break
            plan = mapping.get(source_number)
            if plan is None:
                continue
            if prepend_background_form(output[output_index], source, source_number, plan):
                changed += 1
        if not changed:
            return 0
        output.save(temporary, garbage=3, deflate=True)
        output.close()
        Path(temporary).replace(output_path)
        return changed
    finally:
        source.close()
        try:
            output.close()
        except ValueError:
            pass
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def inject_background_sequence(output_path: str, source_path: str,
                               plans: Iterable[BackgroundPlan | None],
                               source_pages: Iterable[int]) -> int:
    """Inject one owner-specific plan for each output page in a variant PDF."""
    import pymupdf

    temporary = output_path + ".figure-background.tmp"
    output = pymupdf.open(output_path)
    source = pymupdf.open(source_path)
    changed = 0
    filtered_cache = {}
    try:
        for output_index, (plan, source_number) in enumerate(zip(plans, source_pages)):
            if output_index >= len(output) or plan is None:
                continue
            page_changed = bool(plan.excluded_figure_boxes)
            if page_changed:
                # 包围载体可能仍含其他 owner 的白色填充或图形；先让当前
                # 变体避开这些图框，平移嵌入 dual 时也不会盖住 SPANNING。
                clip_source_layer_outside_figures(output[output_index], plan.excluded_figure_boxes)
            if not plan.clip_regions:
                changed += int(page_changed)
                continue
            key = (source_number, plan.figure_boxes, plan.clip_regions)
            filtered = filtered_cache.get(key)
            if filtered is None:
                filtered = pymupdf.open()
                filtered.insert_pdf(source, from_page=source_number, to_page=source_number)
                _redact_outside(filtered[0], plan)
                filtered_cache[key] = filtered
            page_changed = _prepend_filtered_form(output[output_index], filtered, plan) or page_changed
            changed += int(page_changed)
        if not changed:
            return 0
        output.save(temporary, garbage=3, deflate=True)
        output.close()
        Path(temporary).replace(output_path)
        return changed
    finally:
        source.close()
        for filtered in filtered_cache.values():
            filtered.close()
        try:
            output.close()
        except ValueError:
            pass
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "BackgroundPlan", "FIGURE_CLASSES", "build_plan", "clone_for_owner",
    "figure_boxes", "forget", "inject_backgrounds", "inject_background_sequence",
    "prepend_background_form",
    "clip_source_layer_outside_figures", "install_source_layer_guard",
    "remember", "remembered", "remove_selected_objects",
]
