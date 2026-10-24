# S5（#44）插件接线：第 1 轮审查

日期：2026-10-24。审查对象：Lead 编写的接线（`bootstrap.js` 在 `main.js` 前加载 `readerSelection.js`；`main.js` 启动时 `init` 模块、注册 `renderToolbar` 并补挂已打开阅读器，关闭时注销并 `shutdown`）与 `tests/reader-selection-wiring.test.js`。两轴审查者为新开的 Codex gpt-6.1-sol 只读会话。

## Standards 轴：VERDICT: CHANGES

1. `main.js:45`（§7）：模块传入的已脱敏 JSON 行被加上 `reader-selection ` 前缀，再经 `plugin.log` 加一层文本前缀，破坏结构化格式；第 48、56、65、73 行的错误日志也不是结构化 JSON。
2. `main.js:40`（§12）：模块缺失或 Reader 不可用时直接返回、无诊断；第 53 行把异常的 `_readers` 静默降级为空数组。
- 可选建议：用模拟的 `loadSubScript` 执行 bootstrap 验证实际加载顺序。

## Spec 轴：VERDICT: PASS

本机源码确认每个新 PDF 标签页都会渲染工具栏并派发 `renderToolbar`；模块等待 `_initPromise` 与视图就绪；重复触发复用进行中的 Promise、按阅读器与视图去重；`_readers` 补挂覆盖插件启用前已打开的阅读器；关闭时注销事件、模块撤销全部包装与标记，Zotero 也会按插件 ID 自动移除处理器。
- 可选建议：增加真实模块与接线的联合测试（初始化未完成时重复事件及随即停用）。

## Lead 修改

- 新增 `logReaderSelection(reason, error)` 与 `debugLine(line)`：诊断写成"统一调试前缀 + 一行 JSON"（`{"event":"reader_selection_wiring","reason":...,"error":<异常类型>}`），不含异常消息；模块传来的 JSON 行只加统一前缀、不再加二次前缀（仓库 `tests/branding.test.js` 要求所有 `Zotero.debug` 带 "BabelDOC for Zotero: " 前缀）。
- 模块缺失（`module_unavailable`）、Reader API 不可用（`reader_api_unavailable`）、已打开阅读器列表不可用（`open_readers_unavailable`，仍注册新阅读器监听）、挂钩/启动/注销/关闭失败均有诊断。
- 测试改为解析前缀后的 JSON 并逐条断言原因；bootstrap 加载顺序改为实际执行 `startup` 记录 `loadSubScript` 调用来验证。全部 16 个 node 测试通过。

修改后新开两个审查者复审。

## 第 2 轮复审（2026-10-24）

- Spec：VERDICT: PASS（挂钩行为未变；`_readers` 不可用时仍注册新阅读器监听；关闭时完整撤销）。
- Standards：VERDICT: CHANGES——`main.js:42` 诊断缺少计数字段（§7）。上一轮两条已解决。
- Lead 修改：诊断条目加 `count: 1`，测试断言同步；全部 node 测试通过。修改后新开两个审查者复审。

## 第 3 轮复审（2026-10-24，额度刷新后重做）

- Standards：VERDICT: PASS（计数字段已补齐，测试逐条断言）。
- Spec：VERDICT: PASS（挂钩行为未变）。
- 两轴通过，接线提交。
