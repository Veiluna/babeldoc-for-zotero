# T7 跨页划词修复 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS（无阻塞项、无建议）

- 修复保留原生隔离标记与版面类别，解决两侧字符因同为 isolated 而错误合组的根因；无启发式掩盖、静默回退或日志泄露（§5、§7、§12、ADR 0008）。
- 恢复旧分组逻辑后新增最小复现（tests/readerSelection.test.js:1529）失败；当前实现 453 项 readerSelection 测试与 wiring 回归通过，覆盖加高页、双向跨页、原生隔离迟到。
- 真机验收由 future 在 Zotero 完成。
