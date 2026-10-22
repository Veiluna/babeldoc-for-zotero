# 代码审查归档：表格翻译封存（task-3）第 3 轮

- 范围：git diff HEAD（2739e3c）封存变更 + 题注/表注合并 + 表内 marker 修复；每轮新开 2 个独立子代理（Standards 16fc1f1e、Spec 9d63bf37）。

## 第 3 轮 Spec — VERDICT: CHANGES

已闭环：liu2022 p4 表注单个 item（HEAD 7 碎段→1）、p3 表注已译、P01 5/5 题注精确框无拉丁、表2 17/17 保留、图内文字照常翻译、# Overlap 保留、文档边界统一、#37/#38 needs-info、XPI 无 archive。

必须：
1. 表框内 "*" 开头 ≥8 字符单元格（*p < 0.05、**p<0.01）被当表注（collect 跳过 + merge 并入相邻单元格）→ 限定表框外 + 回归。
2. 基线口径：HEAD liu2022 832→826（p4 7 碎段→1，-6）、p01 639→576；分类总数（liu2022 translated 327→194、kept 505→632；p01 214→95、425→481）；831 为中间态。
3. P01/T01 真实重跑门禁待 Lead。

建议：文档记录"普通正文脚注不特殊合并，仅 *†‡§¶※ 开头组合并，Source: 类不在范围"；P01 证据补查表5；发布前 bump manifest。

## 第 3 轮 Standards — VERDICT: CHANGES

第 2 轮必须项/建议已闭环（test_table_roles TestCase、statistics 清理、evidence 精确框、archive 标历史）。

必须：
1. 表内注号单元格仍被当表注送译（合成表 * p < 0.05 未 suspended 8/9，进入主请求与补译；_TABLE_NOTE_START_RE 含 *，fallback_line+marker 在几何判定前 continue；†/‡ 同理）→ 仅豁免表框外 marker 或 table_footnote，负向回归。
2. _merge_table_note_paragraphs 无表框/同列约束，可从表内 marker 起始并合并相邻 fallback_line → 一起修。

建议：source_text 在 merge 后刷新；liu2022 合并表注 translate_input 丢空格（minorresidualcorrections）需修正；e2e 表注断言补唯一 item/无重复/over-merge；证据数字口径（832→826、639→576、discover 命令）；GLOSSARY/README 0.4.1 句补表注。

## 修复（实现者，第 4 轮复核对象）

- 新增 _paragraph_inside_table_frame（重叠 >=80% 视为表格单元格），顺序改为"先几何、后 marker"；merge 起点/候选必须表框外；collect 框内一律保留。
- 新增 tests/test_table_note_rules.py 4 tests；e2e 5 tests（marker 不进请求、PDF 保留、表注唯一/无重复/无 over-merge）。
- 空格保真（_make_space_composition）+ source_text merge 后刷新；replay 826/576；discover 30。
- Lead：GLOSSARY/README/ADR 0007 补表注表述；基线口径按 HEAD 记录。
