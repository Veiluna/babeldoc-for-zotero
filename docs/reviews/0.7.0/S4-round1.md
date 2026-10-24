# S4（#43）链接、目录与页码标签：第 1 轮审查

日期：2026-10-24。审查对象：`ztf_split_compose.py` 中新增的链接、目录、页码标签重建（相对 0c8b79c 的改动）、`tests/test_split_links.py`、`tests/acceptance_split_compose.py` 追加的验收（实现汇报：94 项单测、9 项验收通过；P05 22 页、282 条链接、19 条目录、22 个标签）。两轴审查者为新开的 Codex gpt-6.1-sol 只读会话（审查沙箱无法创建临时目录，测试改用内存验证）。

## Standards 轴：VERDICT: CHANGES

1. `ztf_split_compose.py:621`（§4、§6）：外部目录动作直接传给 `set_toc`；PyMuPDF 1.28.2 遇到合法的 `LINK_GOTOR` 目录项抛 `AttributeError: 'tuple' object has no attribute 'x'`，整份合成失败。需按该版本接口限制处理、隔离单条失败并结构化记录，补回归。
2. `ztf_split_compose.py:541`（§4、§12）：无效 `zoom` 被静默重置为 0，没有原因与计数。

## Spec 轴：VERDICT: CHANGES

真实样本：`[4, 7, 18]` 指向 9a 的同侧参考文献条目；Section 3.1 指向 3a 的对应侧；19 条目录页与侧正确；页码标签正确。18 条页顶回退中 17 条合理；单语缺失的 12 条属上游夹具缺失；英文跨栏省略 2 条符合规格。

1. **中文链接回退时可能落到左侧页顶**（`ztf_split_compose.py:511`；目标 6、§4 同侧跳转）。中文第 13 页引用 Figure 6 的链接跳向 12a 的 `(0, 0)`，应为中文侧页顶或跨栏元素本身；原因是未映射目标的最近区域候选包含 `span`，回退直接取其左边缘。
- 可选建议：验收 `tests/acceptance_split_compose.py:350` 允许 `span.output_box[0]` 作为回退横坐标，会接受错侧结果，应独立检查语言侧边界。

## 处理

交回实现者修改，修改后新开两个审查者复审。

## 第 2 轮复审（2026-10-24，两轴均因 Codex 额度耗尽中断，无 VERDICT）

修改内容：外部目录动作保留标题与层级并停用动作、结构化记录 `unsupported_toc_action`；无效 zoom 返回固定原因 `invalid_zoom` 并计数；回退横坐标取目标页同语言侧左边界（P05 中文 Figure 6 链接现落到 12a 的 (312, 0)）。Lead 本机：S4 相关 103 项单测通过。

中断前的中间结论（未定稿）：
- Spec：上一轮回退错侧已修复；P05 282 个链接框逐一匹配，18 条页顶回退均在正确语言侧，19 条目录与 22 个页码标签核对通过。
- Standards：上一轮两条看起来已处理；**新线索**：合法页码标签含括号或反斜杠时，PyMuPDF 1.28.2 的 `set_page_labels` 直接拼接字符串，会损坏标签或抛异常，当前处理会使整份合成失败（内存复现，未给行号）。

处理：额度 20:23 刷新后，先把页码标签转义问题交实现者修复（含回归），再新开两个审查者做第 2 轮复审。

## 第 3 轮复审（2026-10-24）

修改内容：页码标签直接写 /PageLabels 数字树，前缀按 PDF 字面量规则转义、非 ASCII 用 UTF-16BE 十六进制；单项失败隔离并计数（102 项测试）。

- Standards：VERDICT: CHANGES；Spec：VERDICT: CHANGES（同一问题）。`ztf_split_compose.py:687`：读取源页码标签用 PyMuPDF 1.28.2 的 `get_label()`，它对含括号、反斜杠、非 ASCII 的标签返回失真文本（`(i)`→`\i\`、`A\1` 反斜杠重复、`第一页`→`<FEFF7B2C4E009875>`），写入端转义正确但写的是失真文本，且日志显示 `labels_unresolved=0`；测试 `tests/test_split_links.py:328` 对该读取接口打桩，没覆盖实际路径。
- 其余复核通过：P05 22 页、282 个链接框与目标逐条正确、18 条回退同侧、19 条目录、22 个普通标签经 pdf.js 读回正确；外部目录动作隔离与计数、无效 zoom 固定原因均通过。

处理：交回实现者修正源标签读取（按 PDF 字符串规则自行解码源 /PageLabels，可参考 `xref_get_key()` 取得的解码字符串），补不打桩、直接走真实读取路径的回归；修改后新开两个审查者复审。

## 第 4 轮（2026-10-24）

修改内容：源页码标签改为自行解析 /PageLabels 数字树（含嵌套 /Kids、各编号样式、/St、PDF 字符串解码），读取失败回退页序号并计数（105 项测试）。
- Standards：VERDICT: CHANGES——`tests/test_split_links.py:284` 默认单测依赖不进 git 的 build/research 下 pdf.js（§6）。
- Spec：会话中断，未完成。
- 处理：pdf.js 交叉验证移到 tests/acceptance_split_compose.py（缺资源时说明跳过），默认单测改用底层 /PageLabels 解码断言（105 项单测、11 项验收通过）。

## 第 5 轮（2026-10-24）

- Standards：VERDICT: PASS（测试分层问题解决，无其他违例）。
- Spec：VERDICT: PASS（特殊标签经完整合成路径与 pdf.js getPageLabels() 核对通过；P05 22 页、282 个链接框与目标、18 条同侧回退、19 条目录、22 个普通标签无回归；读写失败计数为 0）。
- 两轴通过，S4 提交。
