# PDFMathTranslate-next 参考与 BabelDOC 图形/表格实验（T04）

> 票据 #27（T04）。目的：用真实 BabelDOC 0.6.4 + 本地 mock OpenAI 服务，对公开论文原文跑不同配置，观察图 4、图 5、表 2、表 3 的行为，确定 0.6.0 应采用的配置与必须后处理的项。
>
> 实验脚本位于构建目录 `build/t04/`（`run_one.py`、`analyze.py`、`run_matrix.sh`），按仓库约定不提交；本文只记录方法与结果。

## 1. 结论摘要

1. **BabelDOC 0.6.4 的图/表开关基本无效**：`--figure-table-protection-threshold` 单独设置 1.0/0.0 与默认完全一致；`--translate-table-text` 因 `table_model` 被强制弃用而无效；`--disable-graphic-element-process` 虽快 4.3 倍、文件小 77%，但会丢失热图的全部填充色，不可用。
2. **图 4/5 的 45° 列标签在直跑中根本没有绘制**：原文每图 75 个 detector 标签，BabelDOC 直跑 A–G 均为 0 个；当前 runner 会保留 75 个英文标签（不翻译）。因此“丢失”是直跑行为，“未翻译”是 runner 行为。
3. **图 5 行标签**：直跑翻译为中文但右对齐丢失（x1 74.9–92.65）；runner 右对齐恢复（x1 全为 98.15），但英文残片与中文重叠。
4. **表 2 的 13 个 90° 旋转列标题保持英文**：table 布局内的文字没有进入翻译流程，表体（55 行）也保留原文；表题已翻译。
5. **表 3 的描述句在 BabelDOC 原生流程中全部翻译**（56/57 行；唯一英文是表头，属 mock 回显规则），但当前 runner 只有 19 个 CJK span，复现了“部分翻译部分未翻译”；这是 `ztf_table_headers` 的几何保护造成的，不是 BabelDOC 参数问题。
6. **推荐**：生产参数保持 A 默认；runner 侧后处理修图 5 的英文/中文重叠、补表 2 旋转表头翻译与重绘、移除表 3 的几何门；图 4/5 的 45° 标签以英文原样保留（detector 缩写符合 T4 规则），若需翻译再按 T4 处理。参考 pdf2zh_next 只借鉴配置字段、进度事件和子进程边界，不复制代码。

## 2. 实验环境与复现方法

| 项 | 值 |
| --- | --- |
| 原文 | `G:/Program Files/Zotero/datas/storage/JYCINM3Z/P05.pdf`，13 页，612×792 pt |
| 目标图表 | 第 10 页 Figure 4（8 个场景热图）、第 11 页 Figure 5（攻击阶段热图）、第 11 页 Table 2（检测器评分表）、第 13 页 Table 3（检测器描述/缩写表） |
| 引擎 | 本机 `G:/ProgramData/zotero-translate-files/babeldoc-env`，BabelDOC 0.6.4，PyMuPDF 1.28.2 |
| 模型 | `tests/mock_openai_server.py`，`--synthetic` 输出与源 token 匹配的合成中文，`--echo-pattern '^[A-Za-z0-9_.-]+$'` 让纯标识符（`A-Acc-Chr1` 等）原样返回，接近真实模型对缩写/数据集名的行为 |
| 输出 | mono（`--no-dual`），`--watermark-output-mode no_watermark`，`--ignore-cache` |
| 公共参数 | 与插件 `addon/content/modules/runner.js` 一致：`--openai`、`--lang-in en`、`--lang-out zh`、`--no-auto-extract-glossary`、`--disable-same-text-fallback`、`--report-interval 0.5`，并注入 runner 的 `--custom-system-prompt` 文本 |
| 检查 | `build/t04/analyze.py`，按原文页坐标区域统计 span 数量、旋转方向、CJK、x1 对齐；图 4 区域 (45,75)-(565,212)，图 5 同区域，表 2 区域 (355,260)-(555,322)，表 3 区域 (50,95)-(300,465) |

复现命令（在仓库根目录）：

```bash
python build/t04/run_one.py A
python build/t04/run_one.py B --figure-table-protection-threshold 1.0
python build/t04/run_one.py C --figure-table-protection-threshold 0.0
python build/t04/run_one.py D --disable-graphic-element-process
python build/t04/run_one.py E --translate-table-text
```

## 3. BabelDOC 0.6.4 源码事实（与实验参数直接相关）

### 3.1 `--translate-table-text` 在 0.6.4 已无效

- `babeldoc/main.py:586` 仍在开关为真时构造 `RapidOCRModel()` 并传给 `TranslationConfig`。
- `babeldoc/format/pdf/translation_config.py:328`：`table_model` 非空时记录 warning，随后强制 `self.table_model = None`（“deprecated and ignored; RapidOCR table-text detection has been retired”）。
- `babeldoc/format/pdf/high_level.py:963` 的 `if translation_config.table_model:` 永远为假，`TableParser` 不会执行。
- 因此该开关只影响启动时是否加载 RapidOCR 依赖，不改变任何排版/翻译行为。

### 3.2 `--figure-table-protection-threshold` 只在移除段落线条时生效

- 唯一读取点是 `babeldoc/format/pdf/document_il/midend/styles_and_formulas.py:1246`，位于 `remove_non_formula_lines_from_paragraphs()`。
- 该方法只在 `translation_config.remove_non_formula_lines` 为真时调用（同文件 376-377 行），而 CLI 默认 `remove_non_formula_lines=False`（`main.py:735`）。
- 因此单独设置 0.9/1.0/0.0 不会改变图 4/5/表 2；必须同时加 `--remove-non-formula-lines` 才会生效。实验 B/C 与 F/G 的对比验证了这一点。

### 3.3 `--disable-graphic-element-process` 控制图形元素保留

- `babeldoc/format/pdf/document_il/frontend/il_creater.py:425` 读取该开关；`is_graphic_operation()` 在其关闭时返回 False（同文件 486 行），图形操作符不再作为独立 graphic element 记录。
- 它不决定文字是否送译；文字翻译仍由 layout/paragraph 流程决定。实验 D 观察它是否改变图 4/5 的裁切与重绘。

### 3.4 图 4/5 的旋转标签没有进入段落流程

- 原文第 10 页 Figure 4 的列标签是 75 个 45° 旋转的 detector 名（`dir=(0.707,-0.707)`，y≈179.7），行标签是 8 个场景名（`wilson`…`fox`，右对齐到 x=87.9）；热图单元格数字为 90° 旋转（`dir=(0,-1)`）。
- 原文第 11 页 Figure 5 的行标签是 11 个攻击阶段名（右对齐到 x=98.2），列标签同样是 45° detector 名；表 2 的 13 个列标题为 90° 旋转（`Network Scans`…`Detection Score`）。
- 在 A 组 BabelDOC 的 `translate_tracking.json` 中，第 10 页（`page[9]`）只有 14 个段落：8 个场景行标签、`Detector`、页眉页脚、图注和正文，**没有任何 45° 列标签**；第 11 页（`page[10]`）有 `Network Scans`、`Service Scans` 等图 5 行标签，但**没有 `Robustness Score`/`Detection Score` 等表 2 独有列标题**。
- 结论：图 4/5 的 45° 列标签既没有作为可翻译段落送译，也没有作为公式保留；输出 PDF 中数量为 0。表 2 区域文字整体未进入翻译流程（table 布局保护），而 `--translate-table-text` 已无效。

## 4. 参考仓库可借鉴点

参考仓库：`PDFMathTranslate-next` v2.8.2，commit `3538a8195d8379fe3fb4a0117c88d15c5b7b5e89`，`https://github.com/PDFMathTranslate/PDFMathTranslate-next`。本机路径 `G:/ProgramData/codex/Projects/zotero-connectors/PDFMathTranslate-next`。

### 4.1 配置：字段齐全，但默认值不能直接照搬

- `pdf2zh_next/high_level.py:506` 的 `create_babeldoc_config()` 把 `pdf` 设置逐项映射为 `BabelDOCConfig`，包括 `figure_table_protection_threshold`、`table_model`、`remove_non_formula_lines`、`non_formula_line_iou_threshold`、`skip_formula_offset_calculation` 等。
- `pdf2zh_next/config/model.py:176` 默认 `translate_table_text=True`；同文件 206 行默认 `figure_table_protection_threshold=0.9`；`no_remove_non_formula_lines=False`。
- 这些默认值对 BabelDOC 0.6.4 的实际效果有限：`table_model` 被忽略（见 3.1），保护阈值在默认参数下不生效（见 3.2）。可借鉴的是“字段映射完整、把 BabelDOC 新参数暴露给调用方”的组织方式，而不是默认值本身。

### 4.2 进度：直接消费 BabelDOC 事件，不额外请求

- `high_level.py:677` 调用 `babeldoc.main.create_progress_handler(rich_pbar_config)`，把 `progress_start`/`progress_update`/`progress_end` 事件交给 handler。
- `gui.py:1029-1042` 读取 `stage`、`overall_progress`、`part_index/total_parts`、`stage_current/stage_total`，组装 “阶段 (分片, 当前/总数)” 文本；没有为进度发额外模型请求。
- 这与本仓库 runner 的 `create_progress_handler` 覆盖方式一致（`addon/content/python/ztf_babeldoc_runner.py:6806`）；T5 可在同一事件流上增加“请求开始/结束”本地事件与每秒计时，不需要新 API 调用。

### 4.3 边界：子进程隔离、超时、取消、错误上报

- `high_level.py:313` 的 `_translate_in_subprocess()` 用 `multiprocessing.Process` + 双向 Pipe 运行翻译，30 分钟超时；父进程负责取消信号、管道关闭、`terminate()`/`kill()` 兜底，并在子进程崩溃且无 IPC 错误时抛 `SubprocessCrashError`。
- 可借鉴的是“子进程边界 + 明确错误类型 + 超时/取消”的骨架思路；本仓库已有 `runner.js` 的 `Subprocess.call` 与 runner 的降级输出，不需要照搬其 IPC 实现。

### 4.4 参考仓库没有的能力

- 在 `pdf2zh_next/` 中检索 `get_links`/`insert_link`/表头后处理均无结果；参考实现只调用 BabelDOC，不做链接恢复、不按 bbox 重绘图内文字、不区分表头/表体。
- 因此本仓库 0.6.0 的链接恢复、图内文字重排、旋转表头、表 3 内容规则都必须自己实现（或在 runner 内做），不能指望参考仓库提供现成方案。

## 5. 实验矩阵结果

### 5.1 矩阵

公共参数见第 2 节；每组都是 mono、13 页、mock 请求 164 次（A–G 七组的 `translate_tracking.json` md5 完全一致，见 5.4）。

| 组 | 额外参数 | 退出 | 耗时 | 图 4 数字 | 图 4 45° 标签 | 图 5 行标签 | 图 5 行标签 x1 | 表 2 旋转标题 | 表 3 描述列 | 副作用 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 无（runner 现有参数集） | 0 | 477.8 s | 528 | **0 / 75** | 11 个中文 | 74.9–92.7（原文 98.2） | 13 个英文，0 中文 | 56 行中文 + 表头英文 | 图 4/5 横轴标签全丢；图 5 右对齐丢失 |
| B | `--figure-table-protection-threshold 1.0` | 0 | 450.6 s | 528 | 0 / 75 | 11 个中文 | 74.9–92.7 | 13 个英文 | 同 A | 无可见变化；阈值未生效 |
| C | `--figure-table-protection-threshold 0.0` | 0 | 446.4 s | 528 | 0 / 75 | 11 个中文 | 74.9–92.7 | 13 个英文 | 同 A | 无可见变化；阈值未生效 |
| D | `--disable-graphic-element-process` | 0 | 103.5 s | 528 | 0 / 75 | 11 个中文 | 74.9–92.7 | 13 个英文 | 同 A | **热图填充色全部丢失**；文件 4.07 MB→0.95 MB；快 4.3× |
| E | `--translate-table-text` | 0 | 455.6 s | 528 | 0 / 75 | 11 个中文 | 74.9–92.7 | 13 个英文 | 同 A | 无可见变化；`table_model` 已弃用 |
| F | `--remove-non-formula-lines --figure-table-protection-threshold 1.0` | 0 | 488.7 s | 528 | 0 / 75 | 11 个中文 | 74.9–92.7 | 13 个英文 | 同 A | 与 A 相同（drawings 771/1058，fills 602/827）；无收益 |
| G | `--remove-non-formula-lines --figure-table-protection-threshold 0.0` | 0 | 473.2 s | 528 | 0 / 75 | 11 个中文 | 74.9–92.7 | 13 个英文 | 同 A | 与 A 相同；本文档没有可删除的图/表线条 |

原文基线（同一脚本、同一区域）：图 4 数字 528、45° 标签 75；图 5 数字 200、行标签 11 个且 x1 全为 98.15；表 2 旋转标题 13 个；表 3 描述列 57 行（含表头）。

### 5.2 逐组观察

**A（基线）**

- 图 4（第 10 页）：528 个 90° 数字单元格仍在，8 个场景行标签仍在（`wilson`…`fox`，mock 对纯标识符原样返回），但 75 个 45° 列标签（`A-Acc-Chr1` 等）在输出中为 0 个；图注翻译为中文。渲染图见 `build/t04/out/render/A_p10.png`（不提交）。
- 图 5（第 11 页）：200 个数字仍在；11 个行标签被翻译为中文，但 x1 从原文统一的 98.15 变为 74.9–92.65，**右对齐丢失**；75 个 45° 列标签为 0 个；图注翻译。
- 表 2（第 11 页）：13 个 90° 旋转列标题（`Network Scans`…`Detection Score`）保持英文、方向 90°、位置不变；表体 55 行检测器名/数字保留；表题（`Table 2: …`）翻译为中文。`translate_tracking.json` 中没有 `Robustness Score`/`Detection Score`，说明表 2 区域文字没有送译。
- 表 3（第 13 页）：描述列 57 行中 56 行翻译为中文，唯一英文是表头 `Detector`（mock 的 `--echo-pattern` 对纯字母原样返回，属于本实验的模拟规则，不是 BabelDOC 行为）；41 个缩写（`A-Acc-Chr1` 等）保留；表题翻译。
- 翻译阶段有 2 次 400（mock 无法解析个别 prompt 的 JSON 数组），BabelDOC 回退 2 段，最终 `Successful: 802, Fallback: 2`。

**B / C（保护阈值）**

- 输出与 A 的逐项指标完全相同，`translate_tracking.json` md5 也与 A 相同（`7502ea842ced95e484c58cd798a191fe`）。
- 原因见 3.2：阈值只在 `remove_non_formula_lines=True` 时被读取；单独设置 1.0/0.0 不改变任何行为。

**D（禁用图形元素处理）**

- 图 4/5 的文字指标与 A 相同（45° 标签仍为 0，图 5 行标签仍为中文且右对齐丢失，表 2/3 相同），但**热图的红色/粉色填充块全部消失**，只剩网格线和数字/标签；渲染图 `build/t04/out/render/D_p10.png`、`D_p11.png`（不提交）。
- 文件从 4.07 MB 降到 0.95 MB，耗时从 477.8 s 降到 103.5 s。
- 原因：`il_creater.py` 的 `on_lt_curve()` 在 `enable_graphic_element_process=False` 时直接 `return`（1159 行），图形路径不进入 IL，排版阶段无法重绘填充/线条。
- 结论：不可用于生产；若为提速使用，必须另行保留图形层（例如把原页面作为背景、只叠加译文），代价与后处理相当。

**E（翻译表格文字）**

- 输出与 A 的指标完全相同；`translate_tracking.json` md5 相同；耗时多约 20 s（加载 RapidOCR 依赖）。
- 原因见 3.1：`table_model` 在 `TranslationConfig` 中被强制置空，`TableParser` 不执行。

### 5.3 F/G：`--remove-non-formula-lines` 与保护阈值

- F（`remove-non-formula-lines` + threshold 1.0）：488.7 s，164 请求；文字指标与 A 相同；第 10/11 页 drawings 771/1058、fills 602/827，与 A 相同；PDF 4066280 字节（A 4066281，差 1 字节）。
- G（`remove-non-formula-lines` + threshold 0.0）：473.2 s，164 请求；文字指标、drawings、fills、PDF 大小（4066281）均与 A 相同。
- 解释：本篇论文的图表线条都位于 layout 识别的 figure/table 区域内，`is_curve_in_figure_table_layout` 在 0.0 和 1.0 下都判定为受保护（或没有与段落重叠的非公式线条），因此没有线条被删除。该参数组对本文档无收益；不建议默认开启，其他文档上存在删除图表线条的风险。

### 5.4 关键证据

- A–G 七组的 `translate_tracking.json` md5 相同（`7502ea842ced95e484c58cd798a191fe`），说明七组的翻译输入与输出逐字相同；差异只发生在排版/绘制。
- A 组第 10 页 `translate_tracking.json`（`page[9]`）只有 14 个段落，不含任何 45° 列标签；第 11 页（`page[10]`）有图 5 行标签，但没有表 2 独有的 `Robustness Score`/`Detection Score`。
- 表 2 区域在 A 输出中保留 13 个英文 90° 标题；表 3 描述列 56 行中文（另 1 行为英文表头）。


### 5.5 当前 runner 默认（A_runner）与直跑的差异

为确认“当前产品行为”，用同一 mock、同一原文跑了 `addon/content/python/ztf_babeldoc_runner.py`（A_runner）。它使用 1 次主请求 + 1 次补译请求（602 个 item），559.4 s，输出 13 页 mono，并生成链接映射。

| 指标 | BabelDOC 直跑 A | 当前 runner A_runner |
| --- | --- | --- |
| 图 4 的 45° 列标签 | 0 / 75（全部丢失） | **75 / 75 保留英文**（`A-Acc-Chr1` 等，位置/角度正常） |
| 图 4 数字 / 行标签 | 528 / 8 | 528 / 8 |
| 图 5 的 45° 列标签 | 0 / 75 | **75 / 75 保留英文** |
| 图 5 行标签 | 11 个中文，x1 74.9–92.65 | 中文 11 个 + 英文碎片 11 个**重叠**，x1 全为 98.15 |
| 表 2 旋转标题 | 13 个英文 | 13 个英文 |
| 表 3 描述列 | 56 / 57 行中文 | **19 个 CJK span**（大量描述仍为英文） |
| 表 3 缩写 | 41 个保留 | 41 个保留 |
| 链接 | 未处理 | 源 148、恢复 106、未解决 42（71.6%） |
| 请求 / 段落 | 164 请求，804 段 | 2 请求，602 段（595 成功、7 段保留原文） |

结论：

- runner 的现有后处理**修复了图 4/5 的“标签整体丢失”**：75 个 45° 标签以英文原样保留，位置和角度正确。对 detector 缩写而言，保留原文符合 T4 的标识符规则；若要求翻译，需要额外处理。
- 但图 5 行标签出现**英文碎片与中文重叠**：渲染图中同一行同时可见 `positive`/`ltration` 等英文残片和中文标签；说明译文被叠加到原标签上，原英文没有被清除。右对齐（x1=98.15）已恢复。
- 表 3 的描述列只有 19 个 CJK span（直跑为 56 行中文），大量描述仍为英文，**复现了规格中的“部分翻译部分未翻译”**；这是 `ztf_table_headers` 的几何角色保护造成的，不是 BabelDOC 参数问题。
- 表 2 的旋转列标题仍未翻译；runner 没有表 2 的翻译/重绘路径。
- 链接恢复 106/148（71.6%），低于规格的 95% 目标；属于 T1 的范围，本文只记录，不展开。

## 6. 推荐配置

### 6.1 生产配置

1. **保持 A 的参数集**：与 `addon/content/modules/runner.js` 现有参数一致，不新增图/表相关开关。
2. **不要添加**：
   - `--figure-table-protection-threshold`（单独无效，B/C 已证实）；
   - `--translate-table-text`（`table_model` 已弃用，E 已证实）；
   - `--disable-graphic-element-process`（热图填充丢失，D 已证实）。
3. `--remove-non-formula-lines` 只在明确需要移除段落内装饰线时使用，并必须同时验证图 4/5/表 2 的线条未被删除（F/G 见 5.3）；默认保持关闭。
4. **图/表保真不要依赖 BabelDOC 参数**：BabelDOC 直跑无法保留 45° 标签，也无法翻译 table 布局内的旋转表头；当前 runner 能保留标签（英文），但图 5 行标签重叠、表 2 未翻译、表 3 只翻译了 19/56 行。上述三项必须在 runner 侧后处理。
5. 进度沿用 BabelDOC `create_progress_handler` 事件（`stage`/`overall_progress`/`stage_current`/`stage_total`），T5 只增加本地“请求开始/结束”事件与每秒计时，不新增 API 请求。

### 6.2 建议的最小改动边界

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| 收集图/表旋转文本与表 2 列标题 | runner 的 IL 后处理 | 从源 PDF/IL 获取原 bbox、方向、字号；不依赖输出 PDF |
| 图 4/5 45° 标签保留/可选翻译 | `ztf_links` 同级的新后处理模块（T2） | 当前 runner 已保留英文；若要求翻译，按 T4 规则处理，不重绘时保持原 bbox/角度 |
| 图 5 行标签去重叠 + 右对齐 | 同上（T2） | 清除英文残片、统一中文，按原 x1=98.15 右对齐 |
| 表 2 旋转表头翻译+重绘 | 表格后处理（T3） | 90° 旋转、中文竖排、字形正立 |
| 表 3 内容规则 | `ztf_table_headers`/runner（T4） | 移除“表体一律保护”的几何门，改为文本类型判定 |


## 7. 必须后处理的项

| 区域 | A 组实际结果 | 问题 | 必须的后处理 | 票据 |
| --- | --- | --- | --- | --- |
| 图 4 的 75 个 45° 列标签 | 直跑 0 个；runner 保留 75 个英文 | 直跑丢失；runner 未翻译 | 无需重绘原文；若要求翻译，按 T4 规则（detector 缩写可保留） | T2 |
| 图 5 的 75 个 45° 列标签 | 直跑 0 个；runner 保留 75 个英文 | 直跑丢失；runner 未翻译 | 无需重绘原文；若要求翻译，按 T4 规则 | T2 |
| 图 5 的 11 个行标签 | 直跑中文、x1 74.9–92.65；runner 中文 + 英文碎片重叠、x1 98.15 | 直跑右对齐丢失；runner 有英文残片 | 清除英文残片、统一中文，保持 x1=98.15 右对齐；中文竖排、字形正立 | T2 |
| 图 5 的行标签方向 | 中文竖排（BabelDOC 已排） | 位置不齐 | 保留竖排，重设右边界 | T2 |
| 表 2 的 13 个 90° 列标题 | 英文、方向/位置不变 | 未翻译 | 按 T4 规则翻译（普通英文词），按原 bbox 90° 重绘中文竖排、字形正立 | T3 |
| 表 2 表体 55 行 | 检测器名/数字保留 | 无 | 不处理（数字/标识符按 T4 保留） | T3 |
| 表 3 描述列 56 行 | 直跑 56/57 行中文；runner 仅 19 个 CJK span | 直跑正常；runner 的“部分翻译”来自几何保护 | 移除/放宽 `ztf_table_headers` 的表体保护，改用 T4 文本类型规则；保留事后中文有效性校验 | T4 |
| 表 3 表头 `Detector`/`Abbreviation` | 英文（本实验 mock 回显） | 真实模型通常会翻译；runner 需要允许表头翻译 | 表头走 must-translate 路径，缩写列保留 | T4 |
| 图 4/5 的数字单元格 | 528/200 个保留 | 无 | 不处理 | T2 |
| 图注（Figure 4/5、Table 2/3） | 已翻译为中文 | 无 | 不处理 | T2 |
| `[0, 1], ,` 多逗号 | 本实验未复现（mock 不经过真实分句） | 可能是真实模型的标点/占位符问题 | 由 T2 的标点/占位符校验处理，不新增启发式 | T2 |


## 8. 不复制代码的说明

参考仓库为 AGPL-3.0 项目。本仓库只借鉴配置字段、事件字段与进程边界等“事实/接口形状”，不复制其源码、注释、提示词或界面文本；实现代码仍以 BabelDOC 0.6.4 的公开 API 与本仓库现有 runner 为基础。本文引用的行号用于说明行为，不构成代码复用。

## 9. 遗留问题与风险

- **当前 runner 的现有后处理有缺陷**：A_runner 实测图 5 行标签出现英文碎片与中文重叠、表 3 仅 19 个 CJK span（直跑为 56 行中文）、链接恢复 106/148（71.6%）。图 5 重叠与表 3 部分是 T2/T4 必须在 runner 侧修掉的产品问题；链接恢复率属于 T1。
- **mock 的表头回显是模拟规则**：`--echo-pattern '^[A-Za-z0-9_.-]+$'` 让 `Detector`、`Abbreviation` 等纯字母表头原样返回；真实模型通常会翻译它们。表 3 表头的最终行为需在 T7 的真实重跑中确认。
- **合成中文不代表翻译质量**：mock 输出是随机汉字，只能验证“是否送译、是否绘制、方向/对齐”，不能验证术语准确性；图 5 行标签与表 2 标题的真实译文质量需真实模型复核。
- **45° 标签的翻译策略未定**：图 4/5 的列标签是 detector 缩写（`A-Acc-Chr1` 等），按 T4 规则应保留原文；若用户希望翻译，需要额外的术语表/翻译路径。后处理必须先决定“重绘原文”还是“重绘译文”。
- **表 2 表头与图 5 行标签文本相同**：两者都包含 `Network Scans`、`Service Scans` 等；后处理必须按区域（图 5 热图 vs 表 2 表格）区分，避免重复翻译或把中文写到错误位置。
- **本文档没有触发 `remove-non-formula-lines` 的删除路径**：F/G 与 A 相同，不能据此判断该参数在其他论文上不会删除图表线条；默认应保持关闭。
- **字体/字号匹配**：BabelDOC 日志的 `Add Fonts 1/1 1144…` 显示大量字体/字符映射对象；后处理重绘中文时需要选择与原标签相近的字号/字重，图 5 行标签的竖排方向需要按中文逐字旋转或使用竖排字体。
- **dual 模式未验证**：本实验只跑 mono。dual PDF 的右侧译文页存在拼版偏移，T2/T3 的后处理需要把原 bbox 映射到 dual 页坐标（或只在 mono 上后处理，再由插件合成 dual）。
- **表 4/5 未覆盖**：规格要求表 2/4/5 同规则处理；本实验只覆盖表 2/3，表 4/5 需要用其他论文或后续票据验证。
- **性能**：A 组 13 页耗时 477.8 s（其中翻译约 2 分钟、排版约 6 分钟）；图/表后处理会增加时间，但不会增加模型请求（除表 2 表头翻译可能需要额外请求，需纳入请求预算）。

## 10. 证据与复现材料（build/ 下，不提交）

| 组 | 输出 PDF（相对 `build/t04/out/`） | 大小（字节） | md5 | 耗时 / 请求 |
| --- | --- | --- | --- | --- |
| A | `A/P05.no_watermark.zh.mono.pdf` | 4,066,281 | `98eca3bde7483529cdc937c739caec96` | 477.8 s / 164 |
| B | `B/P05.no_watermark.zh.mono.pdf` | 4,066,281 | `cabecc9ee14ee0e534f4062c0835689d` | 450.6 s / 164 |
| C | `C/P05.no_watermark.zh.mono.pdf` | 4,066,281 | `653ccd93f7e56a170ed50d34e13ded53` | 446.4 s / 164 |
| D | `D/P05.no_watermark.zh.mono.pdf` | 951,148 | `e1f6f350f150af2457c1250c4f9b2843` | 103.5 s / 164 |
| E | `E/P05.no_watermark.zh.mono.pdf` | 4,066,281 | `006d03e3089501e03f4aca5cfa81b596` | 455.6 s / 164 |
| F | `F/P05.no_watermark.zh.mono.pdf` | 4,066,280 | `34eb793b29280ce31344a91ec5996188` | 488.7 s / 164 |
| G | `G/P05.no_watermark.zh.mono.pdf` | 4,066,281 | `a253920e7d0ea5fb810bf0c14b4b1cd4` | 473.2 s / 164 |
| A_runner | `A_runner/P05.no_watermark.zh.mono.pdf` | 4,162,423 | `41ad56f217879cba3d0ce8332140f5f5` | 559.4 s / 2 |

- 翻译记录：`build/t04/work/<组>/P05/translate_tracking.json`；A–G 的 md5 均为 `7502ea842ced95e484c58cd798a191fe`。A_runner 的记录不同（128 MB，602 个 prompt），反映 runner 的批量/补译策略。
- 日志：`build/t04/out/<组>.log`；A_runner 的协议输出在 `build/t04/out/A_runner.log`（ZTF_WARNING/ZTF_RESULT）。
- 分析结果：`build/t04/out/<组>.analysis.json`；渲染图：`build/t04/out/render/{A,D,A_runner}_p10.png`、`..._p11.png`、`A_runner_p13.png`。
- 复现脚本：`build/t04/run_one.py`（BabelDOC 直跑）、`build/t04/run_runner.py`（runner 默认）、`build/t04/analyze.py`（区域统计）、`build/t04/check_drawings.py`（图形对象统计）、`build/t04/render_pages.py`（渲染）。
- A_runner 的关键协议字段：`requests=2, planned=2, repair_request_count=1, translated_paragraphs=301, failed_paragraphs=7`；链接 `source=148, restored=106, unresolved=42`；`table_warnings=[]`。
