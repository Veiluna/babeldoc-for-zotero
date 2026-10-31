# T3 页眉页脚水印判定 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: PASS

- 签名符合冻结接口；日志无正文；inline image 跳过有固定原因记录；夹具正文只存长度，候选行保留原文本；ztf_split_types.py 未改。
- 建议（不阻塞）：tests/test_split_margins.py:127 P01/P05 只断言存在命中，建议补逐页数量与正文不误判断言（§6）。
- 只读沙箱无法创建临时文件，PDF 证据测试由 Lead 在本机补跑。
