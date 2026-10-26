# S5（#44）阅读器划词钩子：第 7 轮复审

日期：2026-10-24。审查对象：第 6 轮"空字符页截止"修改后的实现（169 项测试通过，全部 node 测试 15/15）。两轴审查者为新开的 Codex gpt-6.1-sol 只读会话（Spec 审查者的测试命令被其自动审批拒绝，结论基于静态核对）。

## Standards 轴：VERDICT: CHANGES

- `readerSelection.js:509`（关联 `:21`；§12、§6）：空页截止调用 `diagnose()` 后设置 `record.logged`，屏蔽该阅读器后续不同故障的诊断。复现：先拖入空页并松开，再让迟到隔离触发菜单几何计算异常，日志只有 `selection_empty_page_cutoff`，缺少 `selection_popup_unavailable`，第 5 轮要求的诊断再次被静默吞掉。22 个复刻原生函数与本机源码一致。

## Spec 轴：VERDICT: CHANGES

历史场景（第 1–6 轮、单个跨栏元素规则）静态复核未见回归。新问题：

- **[P2] 中文页尾选区按 Shift+ArrowLeft 触发阅读器异常**（`readerSelection.js:354`，测试 `:1363`；规格 §5"不得抛错影响阅读器"）。双击中文 `中译` 后松开再按 Shift+ArrowLeft：原生把终点从 8 缩到 7，setter 用尚未推进的 `state.head` 重建，恢复为 `headOffset = 8 = chars.length`；随后原生 `reader.js:78682` 调 `_scrollSelectionHeadIntoView()`，在 76753–76756 读 `chars[8].rect` 抛 `TypeError`。键盘夹具缺少原生滚动调用，掩盖了问题。

## 处理

交回实现者第 8 轮修改，修改后新开两个审查者复审。
