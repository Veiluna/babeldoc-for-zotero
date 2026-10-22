"""封存（0.7.0 起不启用；见 docs/archive/table-translation.md）。
表格单元格字号下限 0.9（严格原边框、允许溢出）。

职责（0.7.0 T-B3 / issue #38，用户定案简化方案）：
- runner 在 BabelDOC typesetting 阶段调用本模块，替换
  Typesetting._find_optimal_scale_and_layout 的测量/应用逻辑；
- 表格段落按 1.0 → 0.95 → 0.9 阶梯尝试，优先保持源字号且不要求整表统一；
- 排版严格使用 paragraph.box（原单元格边框），不做增高/扩展/跨行重排；
- 0.9 在原边框内放不下（all_units_fit=False）时仍应用该次布局产生的
  typeset_units（允许文字溢出单元格），记录 overflow warning；
- 仅当 0.9 布局没有产生任何 unit 时才回退原实现并记录 patch_error；
- 非表格段落完全走原实现（正文 0.95 策略不变）。

输入输出：
- find_optimal_scale_and_layout(...) 与 BabelDOC 内部函数同形，返回
  (scale, typeset_units|None)；
- supports() 只检查 Typesetting API（BabelDOC 版本由 runner 严格校验并阻断，
  本模块不维护第二事实源）；
- summary_message() 把 warning 列表转成 ZTF_WARNING 文案。

失败模式：
- helper 未部署 / typesetting API 缺失：runner 分别记录
  unavailable(reason=helper_missing / typesetting_api_missing)；
- 单次排版异常或 0.9 无任何 unit：回退原实现并记录 patch_error。
"""

from __future__ import annotations

from typing import Any, Callable

MIN_TABLE_SCALE = 0.9
TABLE_LAYOUT_LABELS = frozenset({
    "table_cell",
    "table_cell_hybrid",
    "table_text",
    "table_title",
    "wired_table_cell",
    "wireless_table_cell",
})
_SCALE_CANDIDATES = (1.0, 0.95, MIN_TABLE_SCALE)
_REQUIRED_TYPESETTING_METHODS = (
    "_find_optimal_scale_and_layout",
    "_layout_typesetting_units",
)


def supports(typesetting_cls: Any) -> bool:
    """Typesetting 是否具备 patch 所需的稳定接口（版本由 runner 统一校验）。"""
    return all(
        callable(getattr(typesetting_cls, name, None))
        for name in _REQUIRED_TYPESETTING_METHODS
    )


def is_table_layout_label(layout_label: Any) -> bool:
    """layout_label 是否属于 BabelDOC 的表格类段落。"""
    normalized = str(layout_label or "").strip().lower().replace(" ", "_").replace("-", "_")
    return normalized in TABLE_LAYOUT_LABELS


def summary_message(warnings: list[dict[str, Any]]) -> str:
    """把 table_layout_warnings 转成用户可读的 ZTF_WARNING 文案（按类型分离）。"""
    if not warnings:
        return ""

    def kind_of(item: dict[str, Any]) -> str:
        return str(item.get("kind") or item.get("event") or "")

    unavailable = [
        item for item in warnings if kind_of(item) == "table_layout_patch_unavailable"
    ]
    errors = [
        item for item in warnings if kind_of(item) == "table_layout_patch_error"
    ]
    overflows = [
        item for item in warnings if kind_of(item) == "table_layout_overflow"
    ]
    parts: list[str] = []
    helper_missing = any(item.get("reason") == "helper_missing" for item in unavailable)
    api_missing = any(
        item.get("reason") == "typesetting_api_missing" for item in unavailable
    )
    if helper_missing:
        parts.append(
            "表格单元格字号 patch 未启用（辅助模块未部署），"
            "已按原始排版继续（patch 未生效）"
        )
    if api_missing:
        parts.append(
            "表格单元格字号 patch 未启用（BabelDOC typesetting API 缺失），"
            "已按原始排版继续（patch 未生效）"
        )
    if unavailable and not (helper_missing or api_missing):
        parts.append(
            "表格单元格字号 patch 未启用（原因未知），"
            "已按原始排版继续（patch 未生效）"
        )
    if errors:
        parts.append(
            "表格单元格字号 patch 执行异常，已回退 BabelDOC 原始排版"
            f"（{len(errors)} 处，字号可能小于 0.9）"
        )
    if overflows:
        labels = []
        for item in overflows[:3]:
            label = " ".join(
                part for part in (
                    str(item.get("table") or ""),
                    f"第 {item['page']} 页" if item.get("page") else "",
                    str(item.get("preview") or ""),
                ) if part
            )
            if label:
                labels.append(label)
        parts.append(
            "部分表格单元格在最小缩放 0.9 下仍放不下，"
            "已保持 0.9 并允许文字溢出单元格"
            f"（{len(overflows)} 处"
            + (f"：{'、'.join(labels)}" if labels else "")
            + "）"
        )
    return "；".join(parts)


def _finite_scale(value: Any, fallback: float = 1.0) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return fallback
    if scale != scale or scale in (float("inf"), float("-inf")):
        return fallback
    return max(0.0, min(1.0, scale))


def _result_scale(result: Any) -> float:
    if isinstance(result, (tuple, list)) and result:
        return _finite_scale(result[0], 0.0)
    return 0.0


def _layout_units(
    instance: Any,
    typesetting_units: Any,
    box: Any,
    scale: float,
    line_skip: float,
    paragraph: Any,
    use_english_line_break: bool,
) -> tuple[Any, bool]:
    try:
        typeset_units, all_units_fit = instance._layout_typesetting_units(
            typesetting_units,
            box,
            scale,
            line_skip,
            paragraph,
            use_english_line_break,
        )
    except Exception:
        return None, False
    return typeset_units, bool(all_units_fit)


def _apply_layout_units(
    original: Callable[..., Any],
    instance: Any,
    paragraph: Any,
    page: Any,
    typesetting_units: Any,
    typeset_units: Any,
    scale: float,
    use_english_line_break: bool,
) -> Any:
    """让 BabelDOC 原生 apply 路径应用指定 scale 的 typeset_units。

    不手工 relocate：只把“该 scale 已算好的 units”注入布局调用，由 BabelDOC
    自己的 render/apply 逻辑写回 composition。
    """
    original_layout = getattr(instance, "_layout_typesetting_units", None)
    if original_layout is None:
        return None

    def applied_layout(
        current_units: Any,
        box: Any,
        requested_scale: float,
        line_skip: float,
        target_paragraph: Any,
        use_english_break: bool = True,
    ) -> Any:
        if abs(_finite_scale(requested_scale, 0.0) - scale) < 1e-6:
            return typeset_units, True
        return original_layout(
            current_units,
            box,
            requested_scale,
            line_skip,
            target_paragraph,
            use_english_break,
        )

    instance._layout_typesetting_units = applied_layout
    try:
        return original(
            instance,
            paragraph,
            page,
            typesetting_units,
            scale,
            use_english_line_break,
            True,
        )
    finally:
        instance._layout_typesetting_units = original_layout


def find_optimal_scale_and_layout(
    original: Callable[..., Any],
    instance: Any,
    paragraph: Any,
    page: Any,
    typesetting_units: Any,
    initial_scale: float = 1.0,
    use_english_line_break: bool = True,
    apply_layout: bool = False,
    *,
    is_table_paragraph: Callable[[Any, Any], bool],
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    """带表格单元格 0.9 字号下限的 _find_optimal_scale_and_layout 替代实现。"""
    try:
        table_paragraph = bool(is_table_paragraph(paragraph, page))
    except Exception:
        table_paragraph = False
    if not table_paragraph:
        return original(
            instance, paragraph, page, typesetting_units,
            initial_scale, use_english_line_break, apply_layout,
        )

    def emit(kind: str, fields: dict[str, Any]) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, fields)
        except Exception:
            # 回调失败不得中断排版；调用方负责把事件写进日志。
            pass

    try:
        measured_scale, _ = original(
            instance, paragraph, page, typesetting_units,
            initial_scale, use_english_line_break, False,
        )
        measured = _finite_scale(measured_scale)
        if not apply_layout:
            # 测量阶段把表格段落 floor 到 0.9；实际渲染时再按阶梯应用。
            return max(MIN_TABLE_SCALE, min(1.0, measured)), None
        line_skip = 1.5 if getattr(instance, "is_cjk", False) else 1.3
        box = getattr(paragraph, "box", None)
        min_scale_units = None
        for target in _SCALE_CANDIDATES:
            typeset_units, fits = _layout_units(
                instance, typesetting_units, box, target, line_skip,
                paragraph, use_english_line_break,
            )
            if target == MIN_TABLE_SCALE:
                min_scale_units = typeset_units
            if fits:
                result = original(
                    instance, paragraph, page, typesetting_units, target,
                    use_english_line_break, True,
                )
                if _result_scale(result) >= target - 0.001:
                    return result
        if min_scale_units:
            # 0.9 在原边框内放不下，但仍产生 units：应用它们（允许溢出），
            # 不丢字、不改 paragraph.box。
            result = _apply_layout_units(
                original, instance, paragraph, page, typesetting_units,
                min_scale_units, MIN_TABLE_SCALE, use_english_line_break,
            )
            if _result_scale(result) >= MIN_TABLE_SCALE - 0.001:
                emit("table_layout_overflow", {
                    "measured_scale": round(measured, 4),
                    "applied_scale": MIN_TABLE_SCALE,
                })
                return result
        # 0.9 布局没有任何 unit：回退原实现（全有或全无），记录 patch_error。
        emit("table_layout_patch_error", {
            "reason": "no_typeset_units_at_min_scale",
            "measured_scale": round(measured, 4),
        })
        return original(
            instance, paragraph, page, typesetting_units,
            initial_scale, use_english_line_break, True,
        )
    except Exception as error:
        emit("table_layout_patch_error", {"error": str(error)})
        try:
            return original(
                instance, paragraph, page, typesetting_units,
                initial_scale, use_english_line_break, apply_layout,
            )
        except Exception:
            return _finite_scale(initial_scale), None
