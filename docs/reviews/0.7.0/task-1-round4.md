# 代码审查归档：task-1 第 4 轮

- 范围：git diff HEAD（ce07da7）；每轮新开 2 个独立子代理（Standards 0d6c7a00、Spec d07232c1）。

## 第 4 轮 Standards — VERDICT: PASS

逐项闭环：uncertain 注释准确；新增注释中文；Fluent 键（zh/en 82 键同序）、异步兜底与测试覆盖；URL 规则与边界用例；A1 helper 部署与红绿成立；未放宽既有断言。
建议（不阻塞）：silent catch、兜底丢页码/中文兜底、deployed 清单/清理、Swin 角色断言、FTL 键存在测试、表名本地化、ztf_table_headers.py 旧 docstring。

## 第 4 轮 Spec — VERDICT: CHANGES（已修）

P2-1：e2e deployed 目录不清理，红绿不可自动复现 → 已改 rmtree + 按 runner.js pythonHelpers 部署并断言精确文件集（红绿一键复现）。
P2-2：_table_body_should_translate 静默 except → 已加结构化日志 table_rule_error（脱敏、preview ≤80、每类 ≤3 条）与注入异常测试。
P2-3/P2-4/P2-5：提交说明/ADR 计数与轮次表述 → Lead 修正（2 次 HEAD 复跑 33/25、131=189-58、57/74 算术、子集、可复现命令、4 轮审查与诊断插件复刻说明）。
P3-6：translation-quality:73、ztf_table_headers.py 旧表述 → 已清理。
P3-7：产品名/CamelCase 限定 → 第 5 轮 P2-A 修正（Swin 属普通词送译）。

## 修复后测试

test_table_headers_e2e 2 OK（8 helper）、test_table_headers、test_runner_policy（含新日志用例）、test_table_text 7 OK、test_table_roles 2 OK、table2/table2_runner、diag_rule_loop 0 FAIL、14 Node 测试全绿。
