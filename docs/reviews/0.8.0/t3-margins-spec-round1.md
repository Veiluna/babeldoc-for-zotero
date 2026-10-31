# T3 页眉页脚水印判定 — 规格轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS

- FoBa 第 17 页 /Contents = [299, 300, 301]，IEEE 下载水印在 xref 301，判为 repeated 并有 appended 辅证；页眉、页码命中，138 行正文无误判。
- Chen、P01、P05 页眉与页码判定与样本一致；夹具与原 PDF 一致。
- 建议（不阻塞，已记 issue）：ztf_split_margins.py:229 遇 inline image 放弃整页标签证据、Form 内部标签未递归；tests/test_split_margins.py:127 P01/P05 补逐页命中预期。
