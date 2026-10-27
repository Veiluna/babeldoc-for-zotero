# T1 清理历史代码 — 规范轴评审第 2 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS

- 建议（已采纳）：docs/archive/table-translation.md 恢复步骤补充"恢复单测时把 archive/ 导入路径改回生产目录"。
- 验证：JS 16 个测试文件通过；test_runner_policy、test_table_note_rules（13 项）通过；test_min_requests 因只读沙箱无临时目录未跑到断言。
- context7 额度耗尽，改查 Node.js、PowerShell 5.1 官方文档。
