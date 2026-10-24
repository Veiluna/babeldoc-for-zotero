# S6（#45）降级文案、GLOSSARY 与 README：第 1 轮审查

日期：2026-10-24。审查对象：`runner.js`/`queue.js` 降级提示 Fluent 化、zh-CN/en-US Fluent 资源、GLOSSARY、README 及对应 node 测试（`importer.js` 经核对无需改动）。两轴审查者为新开的 Codex gpt-6.1-sol 只读会话。Lead 本机：全部 16 个 node 测试通过。

## Standards 轴：VERDICT: PASS

Fluent 键中英齐全；测试使用 l10n mock，覆盖异步提示合并、本地化失败后队列继续与原警告保留；`git diff --check` 通过。（审查沙箱拒绝运行 node 测试，结论为静态审查。）

## Spec 轴：VERDICT: CHANGES

- **[P2] `queue.js:626`（规格 §6）**：本地化失败或返回空值时，降级原因显示内部键名 `babeldoc-for-zotero-split-dual-fallback`，用户看不懂；新测试接受了这一结果。应保留可读的兜底提示，测试同时验证"分栏双语生成失败"和"已导入单语译本"两层语义。
- 可选建议：README 可补规格"风险"中的引用识别可能出现重复条目（标"未核实"），以及跨栏识别质量需样本人工核对。
- 其余通过：降级文案中英对应且与 §6 一致；导入命名不变；划词规则、表格封存、回退描述与规格一致；无"整页左右对照"旧说法残留，无夸大承诺。

## 处理

交回实现者修改（含可选建议），修改后新开两个审查者复审。

## 第 2 轮复审（2026-10-24）

修改内容：本地化异常、空值、空白或返回键名时显示可读兜底提示并记录日志；README 补充引用重复条目（未核实）与跨栏识别需人工核对的风险。

- Spec：VERDICT: PASS（上一轮 P2 修复，可选建议已处理，无回归；16 个 node 测试通过）。
- Standards：VERDICT: CHANGES
  1. `queue.js:629`（§3、§12）：兜底提示把中文直接硬编码，en-US 本地化失败时用户看到中文。
  2. `queue.js:630`（§7）：`localization_failed` 日志缺计数字段，且经 `log()` 带 `queue: ` 前缀，不是合法 JSON 行。

Lead 裁决：兜底只在 Fluent 本身失败时使用，无法再从 Fluent 取文案；改为按 `Zotero.locale`（以 zh 开头用中文，否则英文）选择代码内的中英两条最后兜底常量，并测试两种语言；日志改为"统一调试前缀 + 一行 JSON"（含 event、reason、count），补断言。修改后新开两个审查者复审。

## 第 3 轮复审（2026-10-24）

- Standards：VERDICT: PASS（兜底按 Zotero.locale 分语言；诊断为统一前缀 + 单行 JSON，含 event、reason、count；16 个 node 测试通过）。
- Spec：VERDICT: PASS（降级语义两层含义保留，降级导入行为未变）。
- 两轴通过，S6 提交。
