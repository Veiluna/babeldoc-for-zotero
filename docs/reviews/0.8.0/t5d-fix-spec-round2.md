# 真机问题修复 — 规格轴评审第 2 轮

评审者：Codex gpt-6.1-sol（xhigh，全新会话，只读，不跑测试）。基线 abd954d。
结论：VERDICT: PASS（无阻塞）

- 第 1 轮两项已修复：跨栏图形检查覆盖 zh、unknown 等所有层并有中文层重复反例；column_origin_status() 区分单侧与双侧缺失，单侧记 column_origin_missing 并整篇按原文整页 + 中文整页输出，双侧缺失零平移。
- 只读抽查 build/t5e：Chen 6 页、FoBa 8 页；FoBa 跨栏页各 88 张图片；各页保留 hf/span/en/zh 层标记；quality.json、summary.json 均 PASS，mock_closed=true。
- 建议（不阻塞）：body_starts() 在逐页安全网之外调用 _owners()，若将来 sidecar 畸形可能升级为整篇 split_compose_failed，可补边界回归；质量检查器重复实现正文起点统计，可共用纯函数；q/Q 偏移测试未覆盖层标记本身被外层包裹的情况。
