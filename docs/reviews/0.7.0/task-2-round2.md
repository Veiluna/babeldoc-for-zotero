# 代码审查归档：T-B3 / #38 第 2 轮（CHANGES → 方案重定）

- 范围：git diff HEAD（2739e3c）；每轮新开 2 个独立子代理（Standards dff8786f、Spec d39cc3a0）。

## 第 2 轮 Spec — VERDICT: CHANGES

已确认证据成立：P01 源、table-only 三 clip、red 20/19 span 4.98、green 21/20 span min 8.97、分布 [8.97,9.47,9.96]、正文 9.96、requests 1、overflow 2（page5 表2、page8 表5）。

必须：
1. 强制排版仍丢字：_force_typeset_units 对 relocate 异常 continue；_compose_units 先清空再 render；混合 unit 实测 composition 只剩部分且无 patch_error。应全有或全无、先 render 成功再替换、补混合 unit 测试。
2. overflow 与 patch_error 未真正分离：强制排版失败时先 emit overflow 再 emit patch_error，同一格文案矛盾；仅 0.9 成功应用后才 emit overflow，失败只记 patch_error。
3. helper 缺失文案误报为"BabelDOC API 不匹配"，应区分原因。
4. ADR 过度承诺（版本不匹配实际阻断而非降级）；"整表统一 0.9"与证据 [8.97,9.47,9.96] 不符。

## 第 2 轮 Standards — VERDICT: CHANGES

必须：
1. 极端兜底仍丢字 + 测试假绿（test_failed_force_layout 未模拟 render 清空、同时断言 overflow+patch_error）。
2. _content_boundary_below 的 other_top > bottom - 1.0 跳过 1pt 内下方内容，近邻漏判（cell bottom 200、下方 top 199.5 → limit 100.5）。
建议：集成测试未断言 growth（P01 green rescaled=0）；body 下限 0.8 太松；ADR 表述；unavailable reason 区分；table_title 全路径。

## 处理：方案重定（用户 grilling 定案）

用户决定（Q1-A/Q2-A/Q4-A/Q7-A/Q8-A/Q9-A）：
- 继续 typesetting patch 但**简化**：每格最小 0.9（阶梯 1.0/0.95/0.9）。
- **严格使用原单元格边框（不增高/扩展）**；0.9 放不下允许溢出，只记 overflow（含页码/表名）。
- 不做手工 relocate/强制排版兜底；fit=False 也应用 0.9 布局 units（避免丢字）；仅无 unit 时回退原实现 + patch_error（全有或全无）。
- 版本不匹配保持严格阻断（Q9-A），ADR 改准确描述。
- 后续：新开 2 个审查者复审 → P01 + T01 真实重跑门禁 → 提交（Q6-A）。

任务书已重写（build/tasks/0.7.0-table-font.md）；实现者按简化方案重写。
