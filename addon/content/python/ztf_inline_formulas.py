"""在 BabelDOC 0.6.4 分类原始字形时补全行内数学原子。"""

from __future__ import annotations

import functools
import re


_LETTER = re.compile(r"[A-Za-z\u0370-\u03ff]")
_SCRIPT = frozenset("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_OPERATORS = frozenset("+-−±∓=≠≈≃≡<>≤≥×÷·⋅*/∈∉⊂⊆∪∩⊗⊕→")
_BRACKETS = {"(": ")", "[": "]", "{": "}"}


def _script_flags(chars):
    flags = [False] * len(chars)
    anchor = None
    for index, char in enumerate(chars):
        value = char.char_unicode or ""
        if value.isspace():
            continue
        size = char.pdf_style.font_size
        if value in _SCRIPT:
            flags[index] = True
        elif anchor is not None and (value.isalnum() or _LETTER.fullmatch(value)):
            base = chars[anchor]
            base_size = base.pdf_style.font_size
            # 使用原始字符框的基线关系，不能用有斜体下伸部的视觉框判断上下标。
            flags[index] = (
                size <= base_size * .85
                and abs(char.box.y - base.box.y) >= base_size * .08
                and -.5 * base_size <= char.box.x - chars[index - 1].box.x2 <= .6 * base_size
            )
        if not flags[index]:
            anchor = index
    return flags


def _tokens(chars, scripts):
    tokens = []
    index = 0
    while index < len(chars):
        start = index
        value = chars[index].char_unicode or ""
        if value.isspace():
            index += 1
            continue
        if scripts[index]:
            kind = "script"
            index += 1
            while index < len(chars) and scripts[index]:
                index += 1
        elif _LETTER.fullmatch(value):
            kind = "word"
            index += 1
            while (index < len(chars) and not scripts[index]
                   and _LETTER.fullmatch(chars[index].char_unicode or "")):
                previous, current = chars[index - 1], chars[index]
                size = max(previous.pdf_style.font_size, current.pdf_style.font_size)
                # PDF 可以只有词间定位间距，没有空格字形；不能把 of 与 O(n²) 合为 ofO。
                if (current.box.x - previous.box.x2 > .15 * size
                        or abs(current.box.y - previous.box.y) > .15 * size):
                    break
                index += 1
        elif value.isdecimal():
            kind = "number"
            index += 1
            while index < len(chars) and not scripts[index]:
                following = chars[index].char_unicode or ""
                if not following.isdecimal():
                    if not (following == "." and index + 1 < len(chars)
                            and (chars[index + 1].char_unicode or "").isdecimal()):
                        break
                index += 1
        else:
            kind = "op" if value in _OPERATORS else value
            index += 1
        text = "".join(c.char_unicode or "" for c in chars[start:index])
        tokens.append((kind, text, start, index))
    return tokens


def _complete_inline_atoms(tagged):
    chars = [entry[0] for entry in tagged]
    if not chars:
        return tagged
    tokens = _tokens(chars, _script_flags(chars))

    def connected(left, right):
        a, b = chars[tokens[left][3] - 1], chars[tokens[right][2]]
        size = max(a.pdf_style.font_size, b.pdf_style.font_size)
        return (a.xobj_id == b.xobj_id and not a.vertical and not b.vertical
                and -size <= b.box.x - a.box.x2 <= .9 * size
                and abs(b.box.y - a.box.y) <= .8 * size)

    def function_connected(left, right):
        # 函数名与左括号必须直接相连；源空格或几何词间距表示普通词边界。
        if tokens[left][3] != tokens[right][2] or not connected(left, right):
            return False
        a, b = chars[tokens[left][3] - 1], chars[tokens[right][2]]
        return b.box.x - a.box.x2 <= .15 * max(a.pdf_style.font_size, b.pdf_style.font_size)

    def atom(index, depth):
        if index >= len(tokens) or depth > 16:
            return None
        kind, text, _, _ = tokens[index]
        scripted = False
        if kind == "op" and text in "+-−±∓":
            if index + 1 >= len(tokens) or not connected(index, index + 1):
                return None
            operand = atom(index + 1, depth + 1)
            if operand is None:
                return None
            end, scripted, _ = operand
            return end, scripted, True
        elif kind in _BRACKETS:
            inside = expression(index + 1, depth + 1)
            if inside is None:
                return None
            end, scripted, operator = inside
            if (end >= len(tokens) or tokens[end][1] != _BRACKETS[kind]
                    or not connected(index, index + 1) or not connected(end - 1, end)):
                return None
            end += 1
        elif kind in ("word", "number"):
            has_script = index + 1 < len(tokens) and tokens[index + 1][0] == "script"
            # 多字母标识符只有紧跟上下标时才接受，普通单词始终作为边界。
            if kind == "word" and not (len(text) == 1 or (len(text) <= 3 and has_script)):
                return None
            end, operator = index + 1, False
        else:
            return None
        if end < len(tokens) and tokens[end][0] == "script" and connected(end - 1, end):
            end += 1
            scripted = True
        # O(n²) 等单字母函数调用与括号内容组成同一个数学原子。
        if (kind == "word" and len(text) == 1 and end < len(tokens)
                and tokens[end][0] == "(" and function_connected(end - 1, end)):
            argument = atom(end, depth + 1)
            if argument:
                end, subscripted, suboperator = argument
                scripted |= subscripted
                operator |= suboperator
        return end, scripted, operator

    def expression(index, depth=0):
        result = atom(index, depth)
        if result is None:
            return None
        end, scripted, operator = result
        while end + 1 < len(tokens) and tokens[end][0] == "op":
            if not connected(end - 1, end) or not connected(end, end + 1):
                break
            right = atom(end + 1, depth)
            if right is None:
                break
            end, right_script, _ = right
            scripted |= right_script
            operator = True
        return end, scripted, operator

    index = 0
    result = list(tagged)
    while index < len(tokens):
        candidate = expression(index)
        if candidate:
            end, scripted, operator = candidate
            first, last = tokens[index][2], tokens[end - 1][3]
            if (scripted or operator) and any(entry[1] for entry in tagged[first:last]):
                # 原始字形对象保持不动；同一个 PdfFormula 由 BabelDOC 整体搬移。
                result[first:last] = [(char, True, False) for char in chars[first:last]]
                index = end
                continue
        index += 1
    return result


def install_inline_formula_patch():
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import StylesAndFormulas

    original = StylesAndFormulas._classify_characters_in_composition
    if getattr(original, "_ztf_inline_atoms", False):
        return

    @functools.wraps(original)
    def classify(self, *args, **kwargs):
        tagged, first_is_bullet = original(self, *args, **kwargs)
        return _complete_inline_atoms(tagged), first_is_bullet

    classify._ztf_inline_atoms = True
    StylesAndFormulas._classify_characters_in_composition = classify
