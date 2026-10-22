# 代码审查归档：表格翻译封存（task-3）第 1 轮

- 范围：git diff HEAD（2739e3c）封存变更（archive/ 移动、runner 接线移除、测试/基线/文档）；每轮新开 2 个独立子代理（Standards b503b5cd、Spec 80f47248）。

## Standards — VERDICT: CHANGES（修复中）

通过：4 helper 已移入 archive/ 且仅差 docstring；addon/ 生产代码无引用、_load_helper 仅 4 个非表格 helper；runner.js 清单与 helper-manifest 一致；封存契约测试、test_table_suspend 4/4、node 全绿；P01 requests=1、无表格事件。

必须修复：
1. 题注翻译真实回归：P01 第 8 页题注变成 "Tabl"+中文（fallback_line 碎片未被合并）；e2e 只断言含中文、未断言无拉丁残留（假绿）。
2. 半删死代码：_repair_fallback_line_layouts/_caption_* 无生产调用，仅测试直接调用；应接入题注修复。
3. replay 基线非单向：p01 639→578（page8 缺 61 条）、liu2022 832→831，题注碎片被 kept/单元格合并导致位移。
4. test_table2_runner.py main-only，unittest discover 0 tests，删除了 invalid 用例；应恢复 TestCase。
5. 规格文档矛盾（Lead 已修正）。

## Spec — VERDICT: CHANGES（修复中）

通过：20:50 _merge_caption_fragment_layouts 修复有效（P01 20:51 重跑 page8 题注全中文、表内英文保留、CJK 0、requests 1、无表格事件）；archive/清单/T01/node 通过；mock 未冒充真实重跑。

必须修复：
1. replay 两份基线仍是修复前（Tabl/Values 碎片），重生成并记录变化。
2. 题注碎片合并只有 test_runner_policy 单测；补多行/贴表题注或 P01 page8 生产路径断言；evidence 查全部表题注前不能标 all。
3. 文档仍不一致：output-fidelity 目标/Q4/Q5/Q10/Q11/Q12/T3、translation-quality 用户故事4/决策25/27/测试76、ADR 0006 37-42、README 179、archive 文档 32/43/48；补题注碎片问题/恢复步骤/build-xpi 排除说明/ADR 0007 "3 个曾部署 + 1 个未接线"。
4. 门禁：P01+T01 真实重跑；构建 XPI 确认无 archive/；#37/#38 标暂缓。

## Lead 处理

- 文档全面修正：output-fidelity 目标/Q4/Q5/Q10/Q11/Q12/T3 标注封存；translation-quality 用户故事4/决策25/27/测试76；ADR 0006 表格字号节标注历史；README 0.4.2 注记改封存；archive 文档修正 helper 数量/ztf_figure_text 表述、补题注碎片问题（第 9 条）与恢复步骤；ADR 0007 补"3 个曾部署 + 1 个未接线"与 build-xpi 排除。
- scripts/build-xpi.ps1 排除 addon/content/python/archive/。
- issues #37/#38 已评论：0.7.0 起封存，链接 archive 文档与 ADR 0007。

## 修复中（实现者）

题注 fallback-line 合并接入、半删代码接入、replay 重生成、test_table2_runner 恢复 unittest、题注生产路径断言、evidence 全部表题注。
