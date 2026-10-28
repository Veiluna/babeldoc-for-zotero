# T7 跨页划词第 2 部分 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS（无阻塞、无建议）

- readerSelection.js:816–819 重读当前主视图并在等待后核对挂载结果；失败时记录诊断、完整撤销并返回 false（§5、§12、ADR 0008 决策 4）。
- readerSelection.js:20–26、558–559、757：fallback 按 reader/reason、hook 按 view/reason 去重，不随每次拖动刷日志；载荷只有 event/reason/count（§7）。
- 新增竞态与诊断测试覆盖两种替换时序与未处理起点；复制的两个 worker 函数与本机安装包源码一致（§6）。readerSelection 470 项通过。
