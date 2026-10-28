# T6 断句组拆片段再译 — 规格轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS

- 保留成员级 placeholder_issues，样式简化补译生效；补译后失败组统一拆为独立片段，最多追加 1 次请求，复用请求预算；失败片段恢复英文并逐片段计数；未新增设置。
- 两条旧 e2e 改为"拆片段再译救回"符合规格；降级路径由 tests/test_fragment_group_split.py 的 empty/malformed/refusal/截断/重复 ID/样式失败、部分成功、上限拦截、应用失败用例覆盖。
- 建议（不阻塞，记 issue）：补"拆片段仍失败"的 e2e（PDF 导出、ZTF_WARNING、失败段数）；补经过真实 create_completion_adapting 的 503/参数适配只尝试一次用例。
