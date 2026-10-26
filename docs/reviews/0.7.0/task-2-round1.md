# 代码审查归档：T-B3 / #38（表格字号 patch）第 1 轮

- 范围：git diff HEAD（2739e3c）含新增 helper 与测试（4 文件约 973 行）；每轮新开 2 个独立子代理（Standards 055da1a2、Spec e07fb60f）。

## 第 1 轮 Spec — VERDICT: CHANGES（修复中）

核对成立：mock 下源表 2 字号 9.96 → green 8.97（0.9×9.96=8.964，取整合理；RED 4.98）；job.jsonl 中 requests=1、patch_enabled=true、rescaled=1、overflow=1、patch_error=0；表 2 单元格进入请求并输出中文。

必须修复：
1. ztf_table_layout.py:241 只判 typeset_units is None；返回 ([], False) 时清空 composition，极端窄格丢字（已复现）。
2. ztf_table_layout.py:151-154 无行线回退表底，borderless 表增高跨行覆盖。
3. runner.py:7535-7555 patch_error 被当 overflow，文案"保持 0.9"与实际回退（可能 <0.9）不符。
4. 文档（Lead 已更新 ADR 0006 后果/限制、spec T3/T4 字号注记）。
5. 证据修正：clip 含右栏/表下正文（34 CJK span 仅 21 在表格区）；measure.py Desktop fallback 是已翻译 PDF；真实重跑须用 P01。

建议：优先 1.0/0.95+增高再落 0.9；overflow warning 加页码/表名；补 9 格完整译文/正文对比/patch error/unavailable 测试；日志记录实际 BabelDOC 版本。

发布门禁（Lead）：P01 + T01 真实重跑，验证 min≥8.96、可译单元格中文、overflow/warning、requests 不变、表 2/3 不回归。

## 第 1 轮 Standards — VERDICT: CHANGES

已核实：helper docstring 中文且含职责/IO/失败模式；无网络/文件资源；日志结构化、preview≤80、无密钥；_load_helper + runner.js 清单 + helper-manifest 同步；三层测试齐全。复跑：helper 单测 7/7、集成 3/3、manifest 通过；集成真实触发 5 格 measured 0.5–0.65→0.9，表格译文字号 9.0pt（源 10.0）、5 格各 11 字完整、仅 1 次请求；P01 mock 8.97≥8.96；运行 29.5s 与 red 29.9s 持平，无请求/性能回归。

必须修复（1 项）：无内部行分隔线的表（booktabs 仅顶/底框线、wireless_table_cell）增高会到表底、跨行覆盖；应取"行线/表格底边/下方最近 paragraph/character/figure 上边界"最小值，无边界则不增高走 0.9+overflow，增高仅限单元格标签，补无内部横线多行表 bbox 不重叠测试。
建议：吞异常补 patch_error 事件；patch_error 与 overflow 分开计数/文案；版本常量去重；降级 runner 级断言；overflow 字段/文案断言；body 测试下限；几何兜底收紧；清理死代码/注释；测试注释中文化。

已合并下发实现者（含 Spec 必须项 1/2/3/5 与证据修正）。
