# S1（#40）runner 接线：第 2 轮复审

日期：2026-10-24。审查对象：第 1 轮意见由 Lead 修改后的接线。两轴审查者为新开的 Codex gpt-6.1-sol 只读会话（审查沙箱无法创建临时目录，`tests.test_split_wiring` 未在沙箱内运行；Lead 本机 32 项通过）。

## Standards 轴：VERDICT: CHANGES

上一轮第 1、3、4 条已修复；第 2 条仍有遗漏：

1. `ztf_babeldoc_runner.py:357、6225`（§4、§7、§12）：无效 `output_box` 被静默置为 None，导出与归一化都没有计数。
2. `runner.js:476`、`tests/translation-runner.test.js:484`（§3、§5、§12）：新降级提示硬编码中文，未走 Fluent。
3. `tests/test_split_wiring.py:48、185`（§4）：PyMuPDF Document 只在正常路径 `close()`。
- 可选建议：纯函数测试与创建临时 PDF 的 setUp 分开。

## Spec 轴：VERDICT: PASS

上一轮三条均已修复：核对 BabelDOC 0.6.4 `fix_media_box` 与解析、恢复源码，非旋转页坐标换算一致；旋转页逐页回退；降级文案符合 §6，两种模式都传 `--no-dual`，合成路径不调用翻译模型。内存构造 CropBox 偏移、非零 MediaBox 原点、两者叠加、叠加后旋转 90° 四类页面走真实规划与合成：正文无丢失、无重复，仅旋转页回退。

## Lead 修改与裁决

- 第 1 条：导出时无效译文框计入 `invalid_output_box`；归一化时三元素等畸形框计入 `malformed_boxes`、被丢弃的译文框计入 `dropped_output_boxes`，均进入结构化日志；补回归断言。
- 第 3 条：测试中的 PyMuPDF 文档改为 try/finally 关闭。
- 第 2 条：`runner.js` 现有全部用户提示都硬编码中文、尚未接入 Fluent，这是存量问题；按已审批的拆票，"合成失败降级提示文案、Fluent 中英齐全"属于 S6（#45）。本票保留与规格 §6 一致的文案，Fluent 化在 S6 完成，届时同步改测试为 l10n mock 断言。
- 修改后新开两个审查者复审。

## 第 3 轮复审结论（2026-10-24）

- Standards：VERDICT: PASS（第 1、2 轮其余意见闭环；Fluent 化按裁决留至 S6 #45）。
- Spec：VERDICT: PASS（CropBox、旋转回退、降级文案保持正确；无效译文框计数与测试资源释放无回归）。
- 两轴通过，S1 提交。
