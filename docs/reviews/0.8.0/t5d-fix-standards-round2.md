# 真机问题修复 — 规范轴评审第 2 轮

评审者：Codex gpt-6.1-sol（xhigh，全新会话，只读，不跑测试）。基线 abd954d。
结论：VERDICT: PASS（无阻塞）

- zh 层已纳入跨栏图形统计并有中文层重复反例；单侧正文起点缺失记 column_origin_missing、整篇输出原文整页 + 中文整页，双侧缺失零平移。
- build/t5e 只读抽查：Chen 6 页、FoBa 8 页质量检查 PASS；FoBa 跨栏页各 88 张图片、507 个图形且位置一致；get_image_info() 用法符合官方文档。
- 建议（不阻塞）：body_starts() 调用 _owners() 在逐页安全网之外，畸形 sidecar 可能整篇失败；新增坐标计算统一加有限性检查；质量检查器与合成器重复实现正文起点统计，可抽共享纯函数；偏移回归未覆盖层标记被外层 q/Q 包裹；column_translation() 对单侧缺失仍返回 0（已不在主路径），建议改显式状态接口。
