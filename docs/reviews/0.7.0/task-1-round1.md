# 代码审查归档：task-1（0.7.0 两个 bug 修复）

- 分支：feature/0.7.0；审查范围：git diff HEAD（ce07da7）；审查者：每轮新开 2 个独立子代理（Standards + Spec），不复用。
- 关联：issues #36/#37；任务书 build/tasks/0.7.0-bugfix.md；诊断报告 docs/diagnostics/0.7.0.md。

## 第 1 轮（2026-10-22）

### Standards 轴 — VERDICT: CHANGES

必须修复：

- P1：ztf_table_text 的 _URL_RE 前缀匹配把"以 URL 开头的整句"误判为 identifier（HEAD=translate、改后 keep）：
  - https://example.org/x is the dataset we used
  - www.example.org hosts the data
  要求仅整格 URL 时 keep，并补回归用例。
- P2（Lead）：行为变更未同步 spec；已更新 output-fidelity Q5/Q10/Q11/T4、translation-quality 用户故事 4 与表格条款，并新增 ADR 0006。

轻项（已修）：ztf_table_text 注释/docstring 中文化、classify_table_cell 写明返回 action/reason、模块 docstring 补输入输出/失败模式、runner 容错注释。

确认无问题：queue.js load/unload 生命周期（stopProgressTimer 清理两监听；load 前关窗不会永久泄漏）；回放夹具 diff 单向；min_cjk 2→1 与 is_identifier 无其他消费者；测试红灯可信、未放宽。

### Spec 轴 — VERDICT: CHANGES

必须修复：

1. 回放基线：交付/提交说明需区分 HEAD 既有漂移（liu2022 28/90、p01 25/99）与本票改动（62+74）；HEAD 版回放出现 2–3 个 paragraph_classification 事件（断言要求 1），属本机漂移；基线刷新单独提交或明确授权。Lead 计划：代码 / docs / 回放基线三个提交，基线提交写明区分与证据。

注记（最终复核）：Lead 用 `git archive HEAD` 副本复跑 2 次均得 liu2022 33、p01 25（另一次复审独立复跑记录为 liu2022 28/90，p01 25/99）；差异属 HEAD 版回放的非确定性/快照差异。最终采用 33/25（两次一致）：189=90+99 全部 kept->translated；58=33+25 为 HEAD 漂移（189 的子集）；本票归因 131=189-58（liu2022 90-33=57、p01 99-25=74）。2–3 个 classification 事件在 4 次观测中未复现（均 1 个），已在 commit-3 如实记录。
2. runner.py:5889-5894 过期注释（"every other table cell is preserved"）需改为新语义。

确认无问题：#36 三条验收齐备（门控、文案/请求计数不变）；#37 齐备（表体规则、Q11 短值、URL 回归）；Q11 边界与 issue 等价；reason 改名/min_cjk/Q11 语义均在任务书授权内。

## 第 2 轮（复审，2026-10-22）

- 实现者修复：URL fullmatch + 用例；runner 注释新语义；注释/docstring 中文化；容错注释。
- 真实 Zotero 10.0.6 验收证据（诊断插件 v3，docs/diagnostics/0.7.0.md §2.3）：早期 unload ignored、popup load fired、tick 每秒更新（0:00→0:05、15%→90%）、window.close 后 active=false timer=null。
- 第 2 轮：Standards CHANGES（URL 前缀误判、英文注释、UI 文案硬编码）+ Spec CHANGES（回放基线说明、runner 注释），均已修复；后续轮次记录见 round2-7。
