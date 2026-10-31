# T2 底层数据认栏 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS

- 签名与 docs/specs/0.8.0-interfaces.md 一致；ztf_split_types.py 未改。
- helper 不写日志，只返回页号与固定原因码；无静默吞异常。
- 9 份压缩夹具 713,368 字节，只含坐标、段落编号、源文件名与版本，无正文。
- tests.test_split_lines + tests.test_split_columns：18 项通过。
- 建议（不阻塞）：tests/test_split_columns.py:147 样本回归只断言"两栏且栏缝为正"，建议加代表性行与段落归属断言（§6）。
- context7 额度耗尽，改查 Python 3.12 与 PyMuPDF 官方文档、本机 PyMuPDF 1.28.2 源码。
