# 代码审查归档：task-1 第 3、4 轮

## 第 3 轮

### Standards（77c32eb7）— VERDICT: PASS

逐项闭环：runner uncertain 注释准确；新增注释中文；旧"整表保留原文"删除并走 Fluent（zh/en 82 键顺序一致、异步兜底、测试覆盖 zh/en/无页码/失败路径）；runner ZTF_WARNING 同步；URL fullmatch + 前缀散文判定与用例齐全；docs/spec/ADR/GLOSSARY 一致。

建议（不阻塞）：ztf_table_headers.py 顶部 docstring 过时；formatTableWarning 在 entry 本地化失败时丢页码、未编号表硬编码"未编号表"、en-US 用人工 "Table 5" 而 runner 标签为中文；queue.js:599 注释与实现不符；ztf_table_text URL 注释未完全实现。

### Spec（2b837762）— VERDICT: CHANGES（已修）

必须修复：translation-quality:38 仍称"保护整表"（已改）；ADR 0006 措辞与 README 0.4.2 历史注记（已改）；回放基线提交说明/证据需落地（Lead 已写 build/commit-msgs/commit-3-replay.txt：131+58、命令、哈希、事件数）。

新发现：P2 页码硬编码中文（已改为 Fluent entry/separator 键，en-US 全英文）；P3 spec 表述/复现材料/GLOSSARY（已修）。

## 第 4 轮

- 新开 2 个独立子代理：Standards 0d6c7a00（PASS，无必须修复）、Spec d07232c1（CHANGES：P2-1..P2-5、P3-6，均已修复，见 round4）。
