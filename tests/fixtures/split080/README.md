# T2 脱文坐标夹具

来源：用户授权的本机 `samples/0.8.0/` 中四份原文 PDF；2026-10-28 用
PyMuPDF **1.28.2**、Python **3.12.15** 生成。未包含源 PDF、正文、字体或图片。
这些是原文 PDF 的 rawdict 几何回归，**不是 BabelDOC IL 导出或 runner 实机验收**。

生成命令（源 PDF 不入库）：

```powershell
& 'G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe' tests/fixtures/split080/make_fixtures.py 'G:/ProgramData/zotero-translate-files/ztest/samples/0.8.0/'
```

只取 Chen 第 10、18 页，FoBa 第 1、17、19 页，P01 第 2、3 页，P05 第 2、4 页。
文件名页码为 1 起算；载荷 `page` 为 0 起算。

每个 `.json.gz` 解压后为 `{source, page, width, height, pymupdf_version, chars}`。
`chars` 每项是 `[x0, y0, x1, y1, paragraph]`，坐标完整保留，不舍入；
`paragraph` 是 rawdict 的 `block.number`。
数组下标代替字符 `id` 和 `order`，`sub_order=0`，`text="x"`，`xobj=None`。
按 `get_text("rawdict", sort=False)` 的 block/line/span/char 顺序遍历，
不保留提取器的行框作为认栏答案。gzip 的 `mtime=0` 使同环境重复生成一致。

FoBa 第 17 页最后 141 个 rawdict 字符是 IEEE 下载水印；测试人工提供
`margins={Line.index: "repeated"}`，不声称经过 T3 页眉页脚分类。
其余样本不提供 margins。测试离线加载夹具，不需要本机 PDF 或 PyMuPDF。

常量及来源：

- 产品算法没有固定 pt 容差或字体参数。`2` 分别来自半页高、严格多数的一半、区间中点的定义；
  `0`/`1` 是空计数、正区间和 0 起算编号；段落 `-1` 是冻结接口的“无段落”。
  `SPANNING=-2`、`MARGIN=-1` 直接导入冻结类型，不重新定义。
- 切行：同一基线上，相邻字符之间只要有正宽空隙（前一字符 x1 < 后一字符 x0）就拆成基线相同的多条 Line，
  不用任何宽度阈值。跨栏行判定：栏缝区间内有字符，或起止 x 跨过栏缝且中间没有栏缝宽的空隙。
- 夹具脚本的页码集合是验收选页，`block.type=0` 是 rawdict 的文本块定义，
  `sub_order=0`、占位 `x` 是夹具格式；gzip 时间戳 `0` 用于可重复生成，
  文件名 `02d` 仅为页码两位显示。
- 合成测试的所有坐标、框高 `10`、字符宽 `5`/`10`、字符数量 `24`、页宽高
  `600×800` 是人工输入数据；`0.001`/`1000` 是同比缩放验证用数据；
  `141` 是上述人工水印标注的字符数。它们都不是产品算法参数。

接口限制：冻结的 `Line` 不携带逐字符横向区间；备用依据依赖切行保留的断开段。
无文字的跨栏图/表/公式只能报告 `spanning_figure` 原因码，
`PageColumns` 没有元素 ID 或元素归属字段；框内完整包含的文字行归 SPANNING。
本票不接 runner，逐页日志由 Lead 使用 `PageColumns.reasons` 接入。

Docs checked (context7)：已请求 `/pymupdf/pymupdf` 及 Python 库解析，
两者均返回月度额度耗尽，未取得 Context7 文档。按仓库规则改查
[PyMuPDF 官方文档](https://pymupdf.readthedocs.io/en/latest/textpage.html)、
[Python 3.12 官方文档](https://docs.python.org/3.12/library/)，并检查本机
`pymupdf.utils.get_text`、`TextPage.extractRAWDICT` 的 1.28.2 源码：
`rawdict` 返回字符 `bbox`，`sort=False` 不走排序分支；Document 在 finally 关闭。
标准库 `unittest`、`math.isfinite`、`json`、`pathlib`、`gzip`、`argparse`
使用均已按 Python 3.12 文档核对；无新增依赖。
