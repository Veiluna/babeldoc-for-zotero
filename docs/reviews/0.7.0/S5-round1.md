# S5（#44）阅读器划词钩子：第 1 轮审查

日期：2026-10-23。审查对象：`addon/content/modules/readerSelection.js`、`tests/readerSelection.test.js`（未提交工作树）。两轴审查者均为新开的 Codex gpt-6.1-sol 只读会话。

## Standards 轴：VERDICT: CHANGES

- `readerSelection.js:317`：导出的 `init`、`attachToReader`、`shutdown` 缺少接口文档（CODING_STANDARDS §2）。需说明 `options.Zotero`、`options.log` 的结构与是否可选、`log(line)` 约定、`attachToReader` 返回 `Promise<boolean>` 的含义与生命周期要求。
- 验证：`node tests/readerSelection.test.js` 33 项通过。

## Spec 轴：VERDICT: CHANGES

1. **[P1] 起点类别与 Zotero 实际选区锚点不一致**（`readerSelection.js:208`，规格目标 5）。Zotero 按字符左右半部调整 offset（`reader.js:42743`），再以选区首/尾字符的 `isolated` 决定保留哪组（`reader.js:43217–43225`）。在块边界起拖时，首/尾字符可能属于相邻类别，结果保留了其他类别。审查者用本机选区函数复现了三例：英文末字右半部起拖跨页选中中文与下一页页眉/跨栏；英文首字左半部反向拖入跨栏选中跨栏；跨栏末字右半部拖向英文选中英文。测试（`tests/readerSelection.test.js:110`）只检查标记数组，未断言最终选区。
2. **[P1] Zotero 原生隔离结果迟到会使进行中的拖选混选**（`readerSelection.js:234`，规格 §5）。`_initProcessedData` 在视图初始化后异步合并原生隔离标记（`reader.js:75666–75670`、`75775–75789`），包装器保存了新标记但没有与已确定的起点类别协调；从尚未被原生标记的页眉页脚字符起拖，合并后同一拖选变成 `H S E Z` 全选。测试（`:158`）把"全 true"当作通过条件。
- 可选建议：固定一组用 Zotero 10.0.6 原生选区函数的无界面测试，按 mousedown 建立折叠选区、pointermove 扩选的路径断言文本与矩形，覆盖正反向与跨页。

## 处理

CHANGES 交回原实现者修改（§13.4），修改后新开两个审查者复审。
