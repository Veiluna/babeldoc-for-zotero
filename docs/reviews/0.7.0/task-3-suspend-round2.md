# 代码审查归档：表格翻译封存（task-3）第 2 轮

- 范围：git diff HEAD（2739e3c）封存变更 + 题注合并修复 + 基线重生成 + 文档修正；每轮新开 2 个独立子代理（Standards 6daec0fb、Spec e106fbf4）。

## 第 2 轮 Standards — VERDICT: CHANGES

第 1 轮 5 项均闭环：_merge_caption_fragment_layouts 已接入生产包装（ParagraphFinder 之前）、不拆表格、题注精确框 5/5 无拉丁；_repair 改名并接入；replay 2/2 OK（p01 639→576，页 1-3/9 与 HEAD 一致，页 8 -63 为表 5 单元格/碎片；liu2022 831 稳定）；test_table2_runner 4/4 且 discoverable；文档已改；P01 重跑与 evidence 一致。

必须修复：tests/test_table_roles.py 改成普通函数 + main()，unittest discover 0 tests → 恢复 TestCase（已下发）。
建议：statistics import 未用；_request_layout_label 纯中间层；table_header/_TRANSLATABLE_TABLE_HEADERS 不可达但未记录（archive 文档已补记）；evidence 题注 ±10 带入邻接表头（建议精确框 + 5/5 无拉丁断言）；archive §2.2 旧接线清单与 output-fidelity:127 标历史（Lead 已处理）。

## 第 2 轮 Spec — VERDICT: CHANGES

必须修复：
1. liu2022 p4 表注被撕裂翻译（"* Values accurate at time of 个分ion..."；基线 137 kept/138 translated/139 kept）——表注边界未定 + 部分翻译回归；应整条合并后统一翻译，加生产断言。
2. 文档边界冲突（表外附注/星号说明、图内小表头）——用户已定界：表注/表外附注完整翻译（Q1-B），图内文字照常翻译（Q2-A）；Lead 已更新 ADR 0007/README/archive。
3. issue #37/#38 仍带 ready-for-agent ——已移除并加 needs-info（OPEN，待后续版本）。

建议：evidence 题注按行取样；提交说明记录实际基线（p01 639→576：119 translated→kept、6 kept→translated、-63 行；liu2022 832→831：132 translated→kept、1 kept→translated）；diagnostics 增高表述标历史（Lead 已处理）；门禁待 Lead。

## Lead 处理

- 文档：ADR 0007/README/archive 明确"表注/表外附注/图内文字照常翻译，仅表内单元格保留"；output-fidelity:127 与 archive §2.2 标历史；archive §2.5 记录不可达死代码；diagnostics 增高表述标历史。
- issues #37/#38：移除 ready-for-agent、加 needs-info（OPEN）。
- 门禁脚本与提交说明待修复完成后执行。

## 第 3 轮（进行中）

- 待实现者完成：test_table_roles TestCase、表注整条合并翻译（liu2022 p4）、evidence 精确题注框、statistics/_request_layout_label 清理、replay 重生成。
