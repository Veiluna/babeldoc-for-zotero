# 代码审查归档：task-1 第 5 轮

- 范围：git diff HEAD（ce07da7）；每轮新开 2 个独立子代理（Standards 09317ebc、Spec f6d7f021）。

## 第 5 轮 Spec — VERDICT: CHANGES（已修）

必须修复：
- P2-A：output-fidelity:97 误把 Swin 列入 is_identifier 保留（实测 is_identifier("Swin")=False，e2e 断言 Swin 送译）。已改为仅列 ChangeMamba/CenterPoint 等 CamelCase 标识符；README/ADR 同步限定"普通词形式产品名送译，回声 kept_verbatim"。
- P2-B：提交说明轮次表述不一致（commit-1 称 4 轮却指向第 5 轮；commit-2 称第 4 轮最终又注明待补）。已统一为 5 轮（Standards/Spec 各 5 轮；Spec 第 2 轮未返回最终报告，以第 3-5 轮为准），并补 round4/round5 归档。
P3：ADR:26 "4 次"改为 HEAD 副本 2 次 + 工作树 2 次；round1 注记对齐（28/90 与 33/25、57/74 归因）；diagnostics §3 补 v3 日志；最终树重跑 fragment e2e 与 table-headers e2e。

通过项：#36/#37 验收完整；P2-1（e2e rmtree/精确文件集/一键红绿）、P2-2（table_rule_error 与注入测试）、P2-4（计数与算术核实）、P3-6 清理均已验证。

## 第 5 轮 Standards — VERDICT: CHANGES（已修）

必须修复：output-fidelity:97 误列 Swin 为 is_identifier 例子（Lead 已改：Swin 属普通词送译，CamelCase 标识符保留）。
实质建议（已修）：日志脱敏链路（_record_rule_error 统一入口，传入 api_key，error/preview 脱敏，注入 "sk-SECRET-123" 不落盘）；queue.js 未编号表改 Fluent 键 + fallback 保留页码（83/83 键）；Q11 异常记录 chinese_check_error（每类 ≤3 条）。
不修并记录：ztf_table_headers.py/e2e 顶部英文 docstring、_write_job_log/_record_rule_error helper 重复、fragment e2e 环境路径问题。

## 第 6 轮（最终复审）

- 新开 2 个独立子代理（Standards c609924b PASS、Spec ec6bd740 CHANGES：提交说明 82→83 键等，已修复；见 round6）。

## 最终验证（Lead）

- 全量测试：13 核心 Python + 14 Node 全绿；replay 2 OK（各 1 请求）；e2e 在最终树重跑结果见 build/diag/final-*.log。
- 提交：fix / docs / replay 基线三件套；提交说明见 build/commit-msgs/。
