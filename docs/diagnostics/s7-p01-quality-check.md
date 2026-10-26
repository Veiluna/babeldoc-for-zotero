# S7：P01 两项质量门误报（2026-10-25）

输入为已有 mock 端到端目录 `build/s7/mock/20261025T013526-19600`，不是实际翻译或 Zotero 真机验收。本轮未修改产品代码、用户配置或已有 PDF，未提交、推送或调用翻译接口。

## 根因与修复

1. **headers_footers 是文本子串误判。** 输出第 15 页的 hf 带为 y=54.37–65.89；源页第 8 页图中的 `Trafﬁc` 框为 `[139.13,114.39,153.62,120.95]`，属于跨栏图，远离页眉带。原检查器遍历全部源行，只要短行文字出现在长页眉中，就把图中同词误认成页眉副本。现在仅对源 hf 带内的行检查其他区域的副本；字符数、重复边缘文字检查保留。另以 layout 的非 marginal 段落逐字符检查 hf，避免扩大带吞正文时，期望值也跟着元数据扩大而误通过。
2. **authors 的比较基准错误。** P01 作者及机构被 BabelDOC mock 翻译，单语没有原姓名字符串。runner 的 `_is_first_page_author_block` 明确要求邮箱证据；P01 无邮箱，导出 `author=false`、`translated=true`。这不是本轮分栏缺陷：规格目标 3 和 GLOSSARY 要求取单语版本，非目标也明确不改单语行为。源作者锚点仍独立从原文识别；检查器用对应 paragraph 的 `output_box` 定位单语 PDF 的完整行，逐行核对双语中的内容及份数。全部行必须完整位于首页 A 的同一个横跨中线的 span，且其他侧/页不能有副本；单语已翻译时额外保留的原作者行也拒绝。对 P05 原本就有的姓名长页眉、正文参考文献，按对应源页、输出 part、侧及完整输入行的份数配额确认其为非作者内容；不凭“输入全文出现过”无限豁免。无单语输入时沿用原文基准；对应单语区域没有文本时失败。

修复文件：`tests/split_quality_check.py`。回归文件：`tests/test_split_quality_check.py`。

## P01 源第 0 页规划

`plan_page` 返回 2 栏，无回退；区域纵向范围（pt，源页左上原点）：

| 内容 | 区域 | y0–y1 | 放置 |
| --- | --- | --- | --- |
| 标题 | spanning | 90.177–124.890 | A 顶部 |
| 作者、机构 | spanning | 148.305–175.890 | A 顶部 |
| 关键词行 | spanning | 232.732–245.890 | A 顶部 |
| 摘要及左侧标签/图标 | spanning | 255.890–422.890 | A 顶部 |
| 页码 | header_footer | 744.890–756.890 | A、B 各一份 |
| 底部引文信息 | spanning | 759.890–809.890 | B 底部 |
| 左栏正文 | column 0 | 449.215–724.890 | A 左英右中 |
| 右栏正文 | column 1 | 452.203–717.890 | B 左英右中 |

跨栏作者区为一个完整带。单语与双语的两行文本及框完全对应：姓名译文框 `[159.64,146.66,451.57,162.40]`，机构译文框 `[137.72,165.74,308.09,176.36]`。合成的 span 向外扩至 y=146.66–176.36，未缺字、未拆栏、未重复。规划符合目标 3 和 D5，不需要修改 planner/compose/runner。

## TDD 与验证

- 先新增失败正例：独立图中的 `Traffic` 不应被判为页眉副本；已翻译作者应以单语为基准。分别观察 FAIL 后修复至 PASS。
- 负例包括：页眉缺字、页眉复制到正文、页眉带吞入正文；作者缺字、整组重复、译文跨栏之外额外保留原作者、拆到两侧、拆成两个 span。其中吞正文和额外原作者副本先观察 FAIL 后补齐检查。第 1 轮独立两轴审查又发现“后续源页副本”和“行内附加文字的原作者副本”漏检；补 3 个失败用例，再修复至 13/13 PASS。第 2 轮补齐同类的译文行内副本，两个新用例先红后绿，15/15 PASS（审查记录见 `docs/reviews/0.7.0/s7-quality-round1.md`、`s7-quality-round2.md`）。
- 第 3 轮发现全局匹配引入 P05 合法提及误报（该中间版本 P05 authors FAIL），补 P05 式正例、配额重复与错误 part 负例后修复，18/18 PASS；记录 `docs/reviews/0.7.0/s7-quality-round3.md`。
- Python：`G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe -B -X utf8 build/s7/run_split_tests.py`，140 项 PASS，含 18 项新增质量检查用例，覆盖全部 `test_split*.py`（含 wiring）；日志 `build/s7/split-unit-fix.log`。
- 直接逐文件初跑中 `test_split_links.py` 缺仓库根模块路径，`test_split_wiring.py` 的临时目录触发 Windows 沙箱 0700 ACL 问题；上述临时 harness 设置根路径、临时目录及继承 ACL（沿用现有 compose 夹具方式），没有跳过/改写测试。
- Node：按文件执行 `node tests/<file>.test.js`，16 个文件全部 PASS，含 readerSelection 的 416 项。
- 既有产物复查：`G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe -B -X utf8 build/s7/recheck_existing.py`；最终 P01、P05 均 12/12 PASS，输出 `build/s7/P01-quality-fix.json`、`build/s7/P05-quality-fix.json`，日志 `build/s7/quality-fix.log`，包含 Zotero pdf.js 5.7.0 文本顺序与元数据。未重跑完整 mock 端到端，由 Lead 后续执行。
- 第 4 轮全新独立 Standards / Spec 审查均 PASS；两位审查者各自只读重算已有 P01、P05，均 12/12 PASS。归档 `docs/reviews/0.7.0/s7-quality-round4.md`。

## Docs checked (context7)

Context7 已调用 `resolve-library-id`（PyMuPDF），但返回月度配额耗尽，因此未获得库 ID，不能声称 Context7 核验成功。按 AGENTS.md 回退到[官方 Page 文档](https://pymupdf.readthedocs.io/en/latest/page.html)和[官方 Rect 文档](https://pymupdf.readthedocs.io/en/latest/rect.html)，并检查本机绑定 `VersionBind=1.28.2` 的接口签名。

已核实：`get_text("rawdict", sort=False)` 提供行与字符 bbox；`clip` 会排除未完整包含的文字，所以基准提取先读完整行再按中心定位，不直接截取字框；Rect 坐标为左上原点；`apply_redactions(images=0, graphics=0)` 只删文字，测试破坏字符时显式传参。`show_pdf_page(clip=...)` 本身不会排除裁框外文本资源，现有合成先删除区域外文字的策略保持不变。

测试夹具还核对了 `insert_text(point, text, fontsize=..., fontname=...)`、`insert_font(fontname=..., fontbuffer=...)`；PDF 元数据破坏用例按[官方 Document 文档](https://pymupdf.readthedocs.io/en/latest/document.html)使用 `xref_get_key` / `xref_set_key`、`saveIncr()`，字符串用[官方 Functions 文档](https://pymupdf.readthedocs.io/en/latest/functions.html)的 `get_pdf_str` 编码。

## 残留范围

本轮只证明分栏对单语作者区域的保真，不评价翻译模型对姓名/机构的翻译质量。无真实翻译调用、无 Zotero 真机操作；完整 mock 端到端与最终发布门禁仍由 Lead 执行。
