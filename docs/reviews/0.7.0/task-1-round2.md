# 代码审查归档：task-1（0.7.0 两个 bug 修复）第 2 轮

- 范围：git diff HEAD（ce07da7）；审查者：新开 2 个独立子代理（Standards + Spec），不复用第 1 轮。

## 第 2 轮 Standards — VERDICT: CHANGES

已闭环：
- P1 URL 前缀误判：_URL_RE 改整格 fullmatch，纯 URL/端口/查询串/片段/结尾标点 keep，"URL 开头整句"translate；红绿证据与用例齐全。
- P2 spec 同步：Q5/Q10/Q11/T4、用户故事、ADR、回放单向变化与代码一致；Q11 阈值与 issue 等价。

必须修复（已下发实现者，task-1 revision 8）：
1. runner.py uncertain 表注释仍称"整表保留"，实际表体按内容规则送译。
2. 新增英文注释（queue.js load/unload 门控、translation-queue.test.js mock 早期 unload）不符合 §3 中文注释要求。
3. 用户可见文案错误（阻塞 0.7.0 发布）：queue.js:550 / runner.py:7362 "已整表保留原文"与实际行为不符且硬编码中文；需改准确并走 Fluent（zh/en 新键 + 测试断言），ADR 另票不解除发布门。

建议项（部分已处理）：URL 边界参数化（端口/查询/片段/中文紧邻）、docstring 精确化、except 静默补日志、load 不触发兜底测试。

## 第 2 轮修复（实现者 + Lead）

- runner 注释改准确；英文注释中文化（含新增 Fluent 预取与真实 unload 注释）。
- queue.js 删除硬编码旧文案，新增 formatTableWarning（Fluent 新键 babeldoc-for-zotero-table-header-unreliable，zh/en 各 80 键顺序一致；异步兜底仅表名/页码）；translation-queue.test.js 断言新文案 + Fluent 键/参数 + 失败兜底；runner ZTF_WARNING 文本同步更新，tables 结构化字段保留。
- URL fullmatch 收紧为 (?:https?://|www\.)[^\s\u4e00-\u9fff]+；新增 _URL_HEAD_RE 处理"URL 开头但后接正文"；端口/查询/片段/结尾标点 keep，CJK 紧邻正文 translate；参数化用例与红证据。
- 测试：test_table_text 7 OK、test_table_roles 2 OK、test_runner_policy passed、diag_rule_loop 全 PASS、node translation-queue passed、全部 14 个 node 测试通过、test_table_headers_e2e（真实 BabelDOC+mock）2 OK。
- Lead 文档：README 功能简介、GLOSSARY 不确定表格、translation-quality spec、ADR 0006 旧文案修正；output-fidelity 增 0.7.0 注记。

## 第 2 轮 Spec

- 第 2 轮 Spec 未返回最终报告即进入第 3 轮；其关注点（回放基线区分与提交计划、runner 注释）已在第 3 轮复核范围并闭环（见 round3）。

## 第 3 轮

- 新开 2 个独立子代理；结论见 round3（Standards PASS；Spec CHANGES，修复项见 round3/round4）。
