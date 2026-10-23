# 分栏双语排版可行性调研（0.7.0）

> 目的：在写 spec 前确认"左英右中、按栏拆页、跨栏只留译文、划词按语言隔离、链接页码重映射"四项需求能否在 Zotero 10.0.6 + BabelDOC 0.6.4 上实现，以及代价。
> 证据脚本与样例在 `build/research/`（不提交）：`omni/`（从 `G:/Program Files/Zotero/app/omni.ja` 解出的阅读器源码）、`proto/compose_split.py`（拼页原型）、`proto/pdfjs_order.mjs`（用 Zotero 自带 pdf.js 读文本层顺序）、`proto/p05_split.pdf` 与 `p05_split_*.png`。
> 日期：2026-10-23。

## 1. 结论摘要

1. **拼页可行，原文与 BabelDOC 中文版可以按栏直接对位。** P05 的 mono 输出与原文同为 612×792、13 页，左栏 x≈53–296、右栏 x≈318–560，中文段落仍落在原栏位置；用 PyMuPDF `show_pdf_page(clip=栏框)` 把"原文栏"放左半、"译文栏"放右半，原字号不缩放即可放下。
2. **BabelDOC 自带 dual 不能直接用**：只有整页并排（默认）和隔页交替（`--use-alternating-pages-dual`），没有按栏拆分（context7 `/funstory-ai/babeldoc` README）。需要在 mono 产出后由我们自己合成。
3. **栏外文字必须先删掉再嵌入。** `show_pdf_page` 的 clip 只裁画面，栏外文字仍在内容流里，会进文本层。原型先用涂黑（`apply_redactions`，只删文字、保留图片和矢量）去掉栏框外的文字，再嵌入；pdf.js 读出的文本层中左半只有英文、右半没有串入左栏文字（见 §3）。
4. **Zotero 划词按内容流顺序工作，PDF 本身只能做到"页内同侧连续"。** 选区是字符数组上从 anchor 到 head 的连续区间，数组顺序就是 PDF 内容流顺序（行内按 x 排序，没有分栏识别）。把输出页的内容流排成 [跨栏译文][英文栏][中文栏] 后，页内在同一侧拖选只会选中该侧；但跨页选择时，第 k 页取"anchor 到页尾"，会把第 k 页另一侧也带上。任何固定顺序都无法同时满足"英文跨页"和"中文跨页"。
5. **Zotero 阅读器有现成的"隔离字符"机制，可以完全满足需求 3，但要借用私有内部。** 选区算完后会执行 `applySelectionRangeIsolation`：起点字符带 `isolated` 标记时只保留带标记的字符，否则只保留不带标记的字符，跨页同样生效。插件若在按下鼠标时按"起点所在区域"（英文/中文/跨栏）重写所有已加载页字符的 `isolated` 标记，就能做到"英文侧只选英文、中文侧只选中文、混选忽略跨栏元素、单独划跨栏元素可选"。代价是依赖 `_internalReader._primaryView._pdfPages` 等非公开结构，Zotero 升级可能失效，需要检测失败后回退到第 4 条的内容流行为。
6. **链接、目录要自己重建。** `show_pdf_page` 不带注释和链接；需按"源页+坐标 → 输出页+坐标"的映射重建链接与大纲。仓库已有 `ztf_links.py` 和 `*.links.json`，可复用其链接提取结果。
7. **栏位与跨栏元素可从 BabelDOC 中间结果拿。** runner 已经在处理 IL 页的 `page_layout`（`class_name`、box）和段落 `layout_label`（如 `ztf_babeldoc_runner.py` 中 `_merge_caption_fragment_layouts`），可以顺带导出每页版面 JSON，供合成步骤判定栏边界和跨栏元素；原型用的"跨中线即跨栏"粗判会把作者行这类分块的跨栏元素误分到栏内（见 §3 问题 1），不能用于正式实现。

## 2. Zotero 10.0.6 阅读器划词实现

源码位置均指 `build/research/omni/` 下解出的文件。

| 环节 | 位置 | 行为 |
| --- | --- | --- |
| 字符来源 | `resource/reader/pdf/build/pdf.worker.mjs` 约 61190 行 `_structuredCharsProvider` | `page.extractTextContent` 按内容流收集字符，再交给 `getStructuredChars` |
| 字符排序 | 同文件约 58260–58475 行 `split` / `getStructuredChars` | 保持内容流顺序；只在"同一行"内按 x 排序。换行判据是基线变化且 x 回退，没有分栏识别 |
| 页眉页脚隔离 | 同文件约 59948 行 `getContentRects`、约 61240 行 `getProcessedData` | 跨页重复行（页眉页脚、页码）之外的外接矩形为正文区，区外字符标 `isolated` |
| 选区计算 | `resource/reader/reader.js` 约 42707 行 `getRangeBySelection`、43293 行 `getSelectionRanges` | 按鼠标坐标找最近字符 offset，取 anchor 到 head 的连续区间；跨页时首页取到页尾、末页从页首取 |
| 选区隔离 | 同文件约 43200 行 `applySelectionRangeIsolation` | 以起点字符的 `isolated` 决定只保留隔离字符或只保留非隔离字符，跨页生效 |
| 隔离标记写入 | 同文件约 75766 行 `_initProcessedData`、75793 行 `_mergeProcessedPageData` | 把 worker 算出的隔离下标合并进 `this._pdfPages[pageIndex].chars` |
| 拖选入口 | 同文件约 75510 行（iframe window 捕获阶段 pointerdown）、约 78126 行（pointermove 中 `getModifiedSelectionRanges`） | 拖动时直接赋值 `this._selectionRanges`，不经过 `_setSelectionRanges`，所以只包装 `_setSelectionRanges` 不够 |
| 插件可达性 | `chrome/content/zotero/xpcom/reader.js` 约 234 行 | `Zotero.Reader` 实例持有 `_iframeWindow` 和 `_internalReader`（`wrappedJSObject.createReader` 创建），插件能拿到视图对象 |

推论：

- **只靠 PDF 结构（方案 B）**：内容流排成 [跨栏译文][英文栏][中文栏]。页内在英文侧或中文侧内部拖选都只选该侧；从英文拖到中文会两边都选上；跨页选择会带上起始页另一侧的全部文字；跨栏译文在英文选区跨过它时会被选中。
- **插件钩住阅读器（方案 A）**：按下鼠标时根据输出页版面元数据判断起点区域，把该区域字符设为非隔离、其余区域设为隔离（Zotero 原有的页眉页脚隔离单独作为一类保留），然后交给 Zotero 自带的隔离过滤。四条划词规则都能满足。真机验证结果见 §3.1：T1–T5 五条用例全部符合预期；分屏视图、键盘扩选等尚未验证。
- 隔离标记还影响 Zotero 的引用识别（worker 中 `getCitationAndReferenceOverlays` 排除隔离字符），但那是 worker 内的独立副本，插件在阅读器侧改标记不会影响它。双语页上中英两份参考文献可能让 Zotero 的引用跳转识别出重复条目，属于未核实风险。

## 3. 拼页原型（P05 第 1–2 页 → 4 个输出页）

命令：

```
python -I build/research/proto/compose_split.py "G:/Program Files/Zotero/datas/storage/JYCINM3Z/P05.pdf" build/t09/out/input.no_watermark.zh.mono.pdf build/research/proto/p05_split.pdf 0,1
```

做法：每个源栏生成一个输出页，页面尺寸同源页；顶部跨栏块（取自中文版）居中横放，下方左半放原文栏、右半放同一栏框的译文；嵌入前用涂黑删掉栏框外的文字；`show_pdf_page` 调用顺序即内容流顺序（跨栏 → 英文 → 中文）。

验证结果：

| 输出页 | 左半 CJK/拉丁字母 | 右半 CJK/拉丁字母 | pdf.js 文本项顺序 |
| --- | --- | --- | --- |
| 1 | 0 / 2172 | 602 / 231 | 跨栏与左栏英文 57 项连续，其后全是右半 |
| 2 | 0 / 2731 | 765 / 127 | 左栏 57 项连续，其后全是右半 |
| 3 | 0 / 2854 | 786 / 168 | 页眉 1 项，左栏 63 项，其后右半 |
| 4 | 0 / 2954 | 806 / 118 | 左栏 59 项，其后右半 |

右半的拉丁字母是译文中保留的术语、数字和引用编号，不是串栏。pdf.js 统计命令：`node build/research/proto/pdfjs_order.mjs build/research/proto/p05_split.pdf`（用 Zotero 自带 `pdf.mjs`，Node 下补了 `DOMMatrix` 和 `Uint8Array.toHex` 桩）。

原型暴露的问题（正式实现需解决）：

1. 作者行由三个窄块组成，单块都不跨中线，原型把它们分进了左右栏，导致作者信息在第 2 个输出页重复出现。需要用 BabelDOC 版面结果按"行带"判定跨栏，而不是按单块。
2. 中文比英文短，右半下方留白较多，中英段落的纵向位置只能大致对齐（中文段落按 BabelDOC 原位排版）。
3. 页码、页眉页脚在左右两侧各出现一次，需要在 spec 里定处理方式（建议只保留一份）。
4. 单栏页正文宽约 450pt，放进半页（306pt）需缩到约 0.65 倍；或者把输出页加宽到约两倍正文宽度、保持原字号。
5. 跨栏元素位于页中部（如跨栏大图）时，左栏页可在原纵向位置放置它，右栏页需要收拢该处的空白。

## 3.1 真机验证划词隔离（方案 A，Zotero 10.0.6）

做法：临时配置 `G:/ProgramData/zotero-translate-files/ztest/split-probe/profile`（独立数据目录，`-no-remote`，结束后只关自己启动的进程）加载一次性探针插件 `split-probe/xpi/bootstrap.js`：导入原型输出 `p05_split.pdf`，打开阅读器，在 `view._iframeWindow` 上注册捕获阶段 `pointerdown` 监听，按起点所在区域（英文栏 / 中文栏 / 跨栏 / Zotero 原有页眉页脚隔离区）重写所有已加载页字符的 `isolated`。然后在页面坐标上合成 pointerdown/mousedown/mousemove/pointerup 拖选，读取 `view._selectionRanges` 并截图。每条用例先关钩子跑一次（基线）、再开钩子跑一次。

| 用例 | 基线（只靠内容流） | 开钩子 |
| --- | --- | --- |
| T1 第 3 页英文拖到中文 | 混选：x 40–564，含 480 个汉字 | 只选英文：x 40–296，0 个汉字 |
| T2 英文从第 3 页拖到第 4 页 | 第 3 页带上整栏中文（786 个汉字） | 两页都只有英文：x ≤ 296 |
| T3 中文从第 3 页拖到第 4 页 | 第 4 页带上整栏英文（2965 个字母） | 两页都只有中文侧：x ≥ 316 |
| T4 英文向上拖进跨栏中文标题 | 选中标题字符（顶部 y=27，3 个汉字） | 标题被忽略：顶部 y=57，0 个汉字 |
| T5 只在跨栏标题内拖选 | 连带整页英文（2243 个字母） | 只选中标题"多步骤攻击分析的新型警报数据集" |

截图（`split-probe/shot-base-T2.png` 与 `shot-hook-T2.png`）显示阅读器画出的选区高亮与 `_selectionRanges` 一致，开钩子后中文侧不再高亮。证据日志：`split-probe/zotero-stdout.log` 中的 `SPLITPROBE` 行。

注意事项（实现时必须处理或在 spec 中写明）：

- 从 chrome 侧调用阅读器方法时，传入的数组和对象要用 `Components.utils.cloneInto` 克隆到阅读器窗口，否则报 "Permission denied"。
- 拖选途中才加载的页面没有标记；正式实现要同时包装 `_mergeProcessedPageData` 或监听页面数据加载，给新页补标记。
- 未验证：双击选词、三击选行、Shift+方向键扩选、分屏视图（`_secondaryView`）、复制到剪贴板的文本。合成事件不等于真人鼠标，最终验收需人工拖选。

## 4. 方案对比

| | 方案 A：PDF 合成 + 插件划词隔离（推荐） | 方案 B：只做 PDF 合成 |
| --- | --- | --- |
| 需求 1 版式 | 满足 | 满足 |
| 需求 2 跨栏只留译文 | 满足 | 满足 |
| 需求 3 划词 | 满足（依赖 Zotero 私有内部，失效时自动退回方案 B 的行为） | 部分：页内同侧连续可选；跨页、跨侧、跨栏元素都会混选 |
| 需求 4 链接/目录/页码 | 满足（自建映射） | 同左 |
| 主要风险 | Zotero 升级改动阅读器内部；分屏视图和键盘扩选需单独验证 | 用户日常跨页摘抄会混入另一种语言 |
| 预计改动 | 新增 Python 合成模块（版面 JSON → 输出 PDF + 链接/目录重建）；runner 导出版面 JSON；JS 新增阅读器钩子模块；菜单与导入接线 | 去掉 JS 钩子模块，其余同左 |

## 5. 设计决定（用户 2026-10-23 选定）

1. 划词采用**方案 A**（插件钩住阅读器，失效时退回方案 B 行为）；写 spec 前先在真机验证。
2. 新排版**替换现有"双语"右键菜单项**（整页并排双语不再提供），菜单仍为单语、双语两项。
3. 单栏页或过宽的栏：**加宽输出页**，保持原字号。
4. 页眉、页脚、页码**只保留原文一份**，放在整页宽度上。
5. 页中部跨栏元素：按默认处理，放在左栏输出页原纵向位置，右栏页收拢空白；顶部元素放左栏页、底部元素放右栏页（spec 中写明，用户审批 spec 时可改）。

## Docs checked

- context7 `/funstory-ai/babeldoc`：dual 输出只有整页并排与 `--use-alternating-pages-dual` 交替页两种，结论见 §1 第 2 条。
- Zotero 10.0.6 阅读器与 pdf.js：未查外部文档，直接读本机 `omni.ja` 中的打包源码（以本机源码为准），行号见 §2。
- PyMuPDF 1.28.2 `show_pdf_page` / `add_redact_annot` / `apply_redactions`：以本机安装版本实测为准（§3），未另查文档。
