# T1 清理历史代码 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS（无阻塞项）

## 建议（已由 Lead 采纳，均为文档）

1. GLOSSARY / CODING_STANDARDS §11 仍写单栏页输出一页 → 已注明 0.8.0 起为两页（ADR 0009）。
2. CODING_STANDARDS §2 helper 清单仍列表格模块 → 已更新为实际模块。
3. docs/archive/table-translation.md 恢复步骤措辞含糊、`test_table*.py` 会误含仍保留的生产测试 → 已改为分别恢复 helper 到 addon/content/python/、6 个封存单测到 tests/，并列出文件名（ADR 0007 同步）。

context7：/nodejs/node、/microsoftdocs/powershell-docs。
