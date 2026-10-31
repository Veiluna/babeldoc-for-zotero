# T5 合成重写（重做版）+ 水印方案 2 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）。diff 指纹 af89ee30a47f801c。
结论：VERDICT: PASS（无阻塞）

- process_page 钩子只保留字符引用，失效路径有固定原因码，采集后在 finally 清理；变体过滤只改副本。
- 产品与测试代码无 ztf_split_plan 残留引用；复制清单一致；readerSelection.js、ztf_split_types.py 未改。
- 新夹具仅含水印及几何数据；改写测试的临时目录在 build/。
- 建议（不阻塞）：ztf_split_compose.py:152 无穷/非有限范围过滤补固定原因码与计数；ztf_split_compose.py:695 安全网原因码细分（sidecar、源页分析、合成异常）。
- 评审沙箱只读，四个 Python 测试模块未跑完。

## Lead 本机复核

本机跑 split 相关 8 个测试模块共 97 项：96 通过，1 失败——tests.test_split_variants.MockRunnerTests.test_chen_two_pages_with_local_synthetic_translation（Chen 第 1 页跨栏中文变体实际文字少于测试期望）。两位评审均因只读沙箱未能跑到此测试。已派只读诊断，结果与用户商讨后再处理；在此之前不提交。
