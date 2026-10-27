# T3 页边内容离线夹具

来源：本机 `G:/ProgramData/zotero-translate-files/ztest/samples/0.8.0/`，
PyMuPDF 1.28.2。未复制任何 PDF。`make_fixtures.py` 可重建四份 JSON。

生成命令：

```powershell
& 'G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe' tests/fixtures/split080_margins/make_fixtures.py
```

测试命令：

```powershell
& 'G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe' -m unittest tests.test_split_margins
```

## 格式与隐私

每页的行数组为 `[x0,y0,x1,y1,baseline,text_or_length]`；下标即原始行号。
只保留人工核实的页眉、页码、IEEE 下载戳文本；全部正文只保留坐标和长度。
所有浮点数原样序列化，不舍入。`tagged`、`appended` 是独立提取的文字框。
夹具共 7,971 行、765,476 字节（约 748 KiB），没有逐字符数据或图片。

测试直接构造冻结的 `Line`，不依赖 T2 的 `build_lines`。正文长度还原为每行唯一占位，
避免数字归一制造原文并不存在的重复；因此正文真实文本的跨页重复另用本机样本实测核验。
这些夹具是坐标回归，不能冒充 BabelDOC 实机翻译或 runner 接入验证。

## 2026-10-28 本机原文核验

直接读取未脱正文的 rawdict，构造 Line，再调用两个冻结公开接口；没有真实 API 请求。

| 样本 | 页数 | 按 1 起页码逐页统计 margin 行数 |
| --- | --- | --- |
| FoBa | 19 | 3,3,3,3,3,3,3,3,3,3,3,3,3,3,3,3,3,3,3 |
| Chen | 18 | 3,3,3,3,3,3,3,3,3,3,3,3,3,3,3,3,3,3 |
| P01 | 9 | 1,2,2,2,2,2,2,2,2 |
| P05 | 13 | 1,3,3,3,3,3,3,3,3,3,3,3,3 |

FoBa 第 17 页（代码 page=16）：共 141 行，3 行 margin，138 行正文均未误判。
IEEE 下载戳为 `repeated`，基线精确等于其他页；额外获得 appended 辅证。
源页 `/Contents [299 0 R 300 0 R 301 0 R]`，末流 xref 301 是下载戳，
开头 `Q` 恢复先前流的 `q`，所以不能把尾流直接当完整页面来解析。
FoBa / Chen 的全部页面各获得 1 个 appended 框；样本本身无合格 tagged 框。

## 常量与依据

- 覆盖比例 `count * 3 >= page_count`：issue #53 / 冻结接口的 ≥1/3。
- 至少 2 个不同页：跨页重复的定义；同页多次不能凑数，单页只能用标签命中。
- 数字串占位 `#`：使 9 与 10 等页码在数字归一后相同；空白用 split/join 归一。
- 坐标容差、基线舍入、字体阈值：均无。heights 不用于限定页边区域。
- `PDF_LEXBUF_SMALL`（本机 256）：MuPDF 自带 lexer 初始缓冲区大小，可增长，非判定阈值。
- 夹具生成器的 `CANDIDATE_BASELINES`：人工从各样本 origin 核实，只决定哪些文本
  可以入库；不传入产品判定，也不用于生成预期结果。
- appended 不用固定字数/行数阈值：连续独立末流总字数小于前面某个正文流，
  并须在流内先 BT、Tf、定位再写字。只作辅证，绝不写入 MarginReason。

## Docs checked (context7)

- `/pymupdf/pymupdf`：已分别查询标记内容框、Contents 流提取，但服务返回
  `Monthly quota reached`，未得到文档结论。
- Python 标准库：已调用 resolve-library-id，同样因额度失败，未取得库 ID。
- 按项目回退规则核实[官方低层接口文档](https://pymupdf.readthedocs.io/en/latest/recipes-low-level-interfaces.html)、
  [文本提取标志](https://pymupdf.readthedocs.io/en/latest/vars.html)、
  [unittest](https://docs.python.org/3.12/library/unittest.html)、
  [re](https://docs.python.org/3.12/library/re.html)、
  [logging](https://docs.python.org/3.12/library/logging.html) 与 math 文档。
- 以本机 `babeldoc-env/Lib/site-packages/pymupdf/__init__.py` / `mupdf.py` 为准核实：
  `get_contents` 保留数组 xref 顺序，`xref_stream` 解压流，`xref_get_key` 解析嵌套键，
  `set_contents` 只改内存中的页字典；`get_text('dict'/'rawdict', sort=False)` 可保留流顺序。
- 本机原生 `pdf_lex` / `PdfLexbuf` / `fz_tell` 对字符串、注释、转义 NAME 的行为已实测；
  用空 `/ActualText ()` 遮住非目标文字可保持文字推进及完整图形状态，合成 PDF 测试覆盖。

## 已知限制

- inline image（BI/ID/EI）页暂不给 PDF 证据并记录 `inline_image_evidence_unsupported`，
  该页仍可参加跨页重复；没有猜测二进制图像边界。
- 只扫描页面 Contents 中的标记，Form XObject 内部的独立标签不递归；有 Form 的页
  记录 `form_internal_tags_not_scanned`。页面外层标签包含的 Form 仍交给 MuPDF 正常执行。
- appended 是根据流边界、局部文字定位和相对字数提取的候选，不能证明 PDF 编辑历史；
  页眉正文分流也可能构成辅证，因此绝不据此单独判定。
- 精确基线按要求不容差匹配；实际布局略有偏移的重复内容可能漏判。
- runner 接入、helper 复制清单和共享类型由 Lead 处理，本票没有修改这些文件。
