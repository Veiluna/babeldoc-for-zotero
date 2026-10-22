"""表单元格内容规则：判定是否送译、中文译文是否有效。

职责：
- is_identifier：判断数字、缩写、URL、代码等应保留原文的标识符；
- classify_table_cell：按表头/表体角色给出 keep/translate 及原因；
- meaningful_chinese：判断模型返回的译文是否为有效中文（Q11 兜底）。

输入输出：
- 输入为 PDF 文本片段（任意对象，按 str 处理）；输出为 bool 或
  {"action": "keep"|"translate", "reason": str}。纯函数，不读写文件、不发请求。

失败模式：
- 非字符串/空值按空串处理并返回 keep/False；正则与字符串操作不抛异常，
  调用方不需要 try/except。
"""

from __future__ import annotations

import re


_ABBREVIATION_RE = re.compile(r"^(?:[A-Z]{2,}[0-9]*|[A-Z][a-z]*[A-Z][A-Za-z0-9]*)$")
_CODE_RE = re.compile(r"(?:https?://|www\.|[_/\\@#$%^&*+=<>{}|~]|\b\d)")
# 整格 URL：以 http(s):// 或 www. 开头，且后面没有空白或 CJK 正文。
# URL 后紧跟中文（无空格）或其它文字时视为“URL+正文”，按散文送译。
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s\u4e00-\u9fff]+", re.IGNORECASE)
# URL 前缀：用于识别“以 URL 开头但并非整格 URL”的散文。
_URL_HEAD_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_NUMBER_RE = re.compile(r"^[\d\s.,%+\-–—/()]+$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _normalize(text: object) -> str:
    return " ".join(str(text or "").split())


def is_identifier(text: str) -> bool:
    """数字、缩写、URL 与代码类标识符应保留原文。

    输入先经 str() 归一化（非字符串按空串处理）。整格就是一个 URL（含端口、
    查询串、片段、结尾标点）时无条件保留，不受词数影响；URL 后紧跟空白或
    中文正文时不算整格 URL，整句仍属散文。长句只有整格是单个代码 token 时
    才算标识符，例如 "New cred_disp parameter combination in Audit logs."
    仍送译。短值例外：无空格且含代码符号/数字的单元（如 "17"、"T1042"、
    "A-Acc-Chr1"、"YES/NO"）视为标识符保留。
    """
    value = _normalize(text)
    if not value:
        return True
    if _NUMBER_RE.fullmatch(value):
        return True
    if _URL_RE.fullmatch(value):
        return True
    if _URL_HEAD_RE.match(value) and (re.search(r"\s", value) or _CJK_RE.search(value)):
        # 以 URL 开头但后面还有空白/中文正文：整句是散文，不能整格保留。
        return False
    if " " not in value and _CODE_RE.search(value):
        return True
    words = _WORD_RE.findall(value)
    if len(words) >= 4:
        return False
    if _CODE_RE.search(value):
        return True
    if " " not in value and re.search(r"[_\-]", value):
        return True
    tokens = value.split()
    if tokens and all(
        token.isupper() or token.isdigit() or _ABBREVIATION_RE.fullmatch(token)
        for token in re.split(r"[\s/\-]+", value)
        if token
    ):
        return True
    return False


def classify_table_cell(text: object, role: str = "body") -> dict[str, str]:
    """判断表单元格是否送译，并记录判定原因。

    参数：
    - text：单元格文本，非字符串按空串处理；
    - role："header"（表头）或 "body"（表体，默认）。

    返回 dict，键固定为：
    - "action"："keep"（保留原文）或 "translate"（送译）；
    - "reason"：判定原因，取值为 "empty"/"identifier"/"no_words"/"header"/
      "body"，仅用于诊断日志。

    表头中的普通英文也送译；表体只保留标识符/数字/URL/代码，其余普通英文
    （含人名、产品名、1-2 词标签）一律送译。没有英文词的单元格保持原文。
    """
    value = _normalize(text)
    if not value:
        return {"action": "keep", "reason": "empty"}
    if is_identifier(value):
        return {"action": "keep", "reason": "identifier"}
    words = _WORD_RE.findall(value)
    if not words:
        return {"action": "keep", "reason": "no_words"}
    if role == "header":
        return {"action": "translate", "reason": "header"}
    return {"action": "translate", "reason": "body"}


def meaningful_chinese(source: object, translated: object, min_cjk: int = 1) -> bool:
    """判断模型返回的译文是否为有效中文，而不是回声、数字或噪声。

    Q11：至少 min_cjk 个 CJK 字符、非纯数字、非纯标点、去除空白后不等于原文；
    完全不含 CJK 字符（纯英文/数字/符号）同样拒绝。放宽后短枚举
    （"sO、sA、sW、sR、sL 和 B）"）与专名混合译文都算有效，只有回声、
    无 CJK、纯数字和纯标点被拒绝。
    """
    source_text = _normalize(source)
    translated_text = _normalize(translated)
    if not translated_text:
        return False
    if re.sub(r"\s+", "", translated_text) == re.sub(r"\s+", "", source_text):
        return False
    if translated_text.isdigit():
        return False
    if not any(character.isalnum() for character in translated_text):
        return False
    return len(_CJK_RE.findall(translated_text)) >= min_cjk
