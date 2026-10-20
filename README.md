# BabelDOC for Zotero

当前版本：**0.5.0**。主要支持 **Zotero 10.0.6（64 位）**，兼容 Zotero 7–10。这是一个 Windows 插件。它调用本机自动安装的 BabelDOC 环境和用户配置的 OpenAI 兼容接口，生成保持版式的简体中文 PDF，并把译本作为新的文件附件导入 Zotero。

Zotero 会把新 ID 识别为新插件，设置会自动迁移；旧版 Translate Files for Zotero 需手动卸载。

## 功能简介

- 只在用户从条目或 PDF 附件的右键菜单选择“翻译成中文 PDF”后运行，不会自动翻译。
- 按 PDF 的文字层进行翻译，包括可编辑的矢量图表文字和题注；v1 不对图片中的文字做 OCR。
- 输出格式可选单语中文译本、左右双语对照（左原文、右译文，引用在各侧跳转），或两种同时生成；默认单语。原 PDF 保持不变。
- 结果作为新的已存储文件附件加入 Zotero。单语译本标题为“原标题（中文译本）”，双语对照为“原标题（中英对照）”。
- 插件没有设置页数或单篇请求次数上限。多个任务按 FIFO 顺序排队，同一时间处理一个；单次请求遇到短暂故障最多重试两次，任务最终失败后提示错误并继续下一个任务。
- 所有已识别的应译内容必须完成。主翻译后始终执行一轮合并补译；补译后仍失败则整篇失败，不导入任何译本，并报告页码、段落及原因。按保护规则保留的内容不计为失败。
- 整篇文档按阅读顺序合并并尽量用最少的请求翻译；每次请求的最大输出固定为 `50000` token。自动术语表提取和逐段重发已关闭；完成状态显示实际网络尝试次数。

## 系统要求

- Windows 和 Windows PowerShell 5.1。
- Zotero 7–10。当前主要支持 Zotero 10.0.6（64 位）；插件清单声明的版本范围为 6.999 至 10.*。
- 插件装好后启动 Zotero 时，插件会自动在 `%LOCALAPPDATA%\babeldoc-for-zotero` 创建独立的 Python 3.12 环境；无需安装 Miniconda，也不会修改 PATH。
- 一个可用的 OpenAI 兼容 Chat Completions 接口、API key 和模型名。

## 安装

### 1. 打包插件

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-xpi.ps1
```

脚本会把插件打包到 `build\babeldoc-for-zotero-<版本>.xpi`。不需要自行打包时，可以在 [GitHub Releases](https://github.com/Veiluna/babeldoc-for-zotero/releases/latest) 下载已发布的 `.xpi` 文件。

打包时也会根据 `addon/manifest.json` 生成 `build/updates.json`。发布自动更新时，将该文件作为 `release` 版本的 `updates.json` 资源发布，并将对应版本的 XPI 发布到 `v<版本>` 标签下。仓库和发布资源已公开，Zotero 可通过 `https://github.com/Veiluna/babeldoc-for-zotero/releases/download/release/updates.json` 检查更新。发布新版本时应先上传 XPI，再更新自动更新清单。

### 2. 在 Zotero 安装插件

打开 Zotero 的 **工具 → 插件**，选择 **从文件安装**，然后选中 `.xpi` 文件。

### 3. 填写插件设置并首次翻译

打开 Zotero **设置 → BabelDOC for Zotero**，填写：

1. **接口地址**：例如 `https://api.example.com/v1`。插件会把根地址归一化为 Chat Completions endpoint，例如 `https://api.example.com/v1/chat/completions`。主机根地址会补成 `/v1/chat/completions`；版本路径（如 `/v1`）和 `/v1beta/openai` 会补上 `/chat/completions`；已填写完整的 `/chat/completions` endpoint 保持不变。
2. **API key**：接口服务提供的密钥。
3. **模型名**：接口服务支持的模型名称。
4. **输出格式**：默认“单语中文译本”；可选“左右双语对照”或“同时生成两种”。三种模式都只执行一次翻译，不会增加模型请求。
5. **翻译参考文献**：默认关闭，按原有规则保留参考文献。
6. **下载源**：默认使用国内镜像，也可切换到官方源。插件装好后，Zotero 启动时会自动在后台下载并安装 uv、Python 3.12、BabelDOC 0.6.4 及字体和模型；若未装完，翻译时会从中断处继续（通常需要数分钟并下载数百 MB）。设置页的“修复环境”会完整删除 `%LOCALAPPDATA%\babeldoc-for-zotero` 后从头安装；已有环境会先询问确认，正在翻译时不能开始修复。清理不会删除 `%USERPROFILE%\.cache\babeldoc` 或 Zotero 数据目录中的日志。

这些设置分别使用 Zotero 偏好键 `extensions.babeldoc-for-zotero.baseURL`、`extensions.babeldoc-for-zotero.apiKey`、`extensions.babeldoc-for-zotero.model`、`extensions.babeldoc-for-zotero.translateReferences`、`extensions.babeldoc-for-zotero.outputMode` 和 `extensions.babeldoc-for-zotero.downloadSource`。最大输出固定 `50000`、单篇请求不限次数、合并补译始终开启；对应的三项控件已移除，历史偏好及旧版插件传入的环境变量不再影响这些规则。

点击 **测试连接** 后，插件会向接口发送一次简短的 `ping` 请求。它不会运行一次完整的 PDF 翻译。

## 使用

1. 在 Zotero 中右键点击普通条目或 PDF 附件，选择 **翻译成中文 PDF**。
2. 插件会显示入队提示。队列按顺序逐个处理，当前任务会打开进度窗口，显示阶段、进度和排队数量。
3. 成功后，所选产物会作为新附件导入对应的父条目：单语标题为“原标题（中文译本）”，双语为“原标题（中英对照）”；原始 PDF 文件不变。
4. 如果合并补译后仍有应译段落失败，任务会显示页码、段落及原因，不导入译本。按选项保留的参考文献，以及专名、缩写、数字、公式等保护内容不计为失败。

## 请求次数与术语一致性

插件先完成段落预处理并剔除不需要翻译的内容，再按文档阅读顺序收集段落（包括跨页和跨栏段落），最后按输出 token 预算贪心打包。对正数段落估算值，这种顺序打包会得到最少的连续批次数；整篇文档放得下时只发一个请求。估算值为 `ceil(源文 token 数 × 1.6 + 20)`，批次最多使用固定 `50000` 输出上限的 `90%`，为中文扩展和 JSON 留出余量。截断后会将最后一个解析项视为未确认，只确认此前的完整项，再按观察到的扩展比例重新打包未确认后缀；如果没有完整项，才将剩余批次二分。单段仍被截断时进入合并补译。观察到实际输出长度后，插件会更新扩展比例并重新打包尚未发送的段落。

翻译提示词要求整篇文档内的术语、缩写和专名保持一致，并沿用首次出现时的译法。插件不运行额外的自动术语表请求，也不会在批次失败后逐段重发；失败段落始终进入一轮合并补译流程，补译后仍失败则整篇失败。BabelDOC 的翻译缓存会绕过，避免失败时保留的原文被后续任务当作译文复用。

单篇请求不限次数，所有真实网络尝试均计数和记录，包括续传、拆分、补译、故障重试及明确拒绝参数名后的适配请求。连接、超时、HTTP 408/409、非余额不足的 429 和 5xx 最多重试两次，间隔为 2、4 秒；取消可以中断等待，已完成的批次不重发。SDK 自带重试保持 `0`。未取得实际 token 用量时，日志保留未知值，不写成零。鉴权、余额不足和明确配置错误直接停止。服务商不支持 `50000` 时明确失败，不自动降低数值；仅在明确拒绝参数名时切换 `max_tokens` / `max_completion_tokens`，数值仍为 `50000`。

## API key 的处理

插件通过环境变量 `ZTF_OPENAI_API_KEY` 把 API key 交给本机 Python runner。runner 读取后会从环境变量中移除该值，并在 Python 进程内部将它加入 BabelDOC 的参数；密钥不会出现在操作系统可见的进程命令行参数中。

API key 保存在 Zotero 的 `prefs.js` 偏好设置文件中，**以明文存储**。设置页使用密码输入框只会遮挡屏幕显示，不会加密该文件。请保护好自己的 Windows 用户账户和 Zotero 数据目录。

## 常见问题

### 接口地址应该怎么填？

通常填写接口的 API 根地址，例如 `https://api.example.com/v1`。测试连接会按归一化后的完整地址发送请求。完整的 `/chat/completions` endpoint 也可直接填写；带 `?query` 的地址会原样用于测试连接。

翻译任务由 BabelDOC 的 OpenAI 客户端构造请求，它会在 base URL 后追加 `/chat/completions`。因此，未被识别为主机根地址或版本路径的自定义地址会按原样当作 base URL 使用；如果它其实是另一个完整 endpoint，翻译可能无法连接。BabelDOC 不支持在 base URL 中保留查询参数，带 `?query` 的地址可以测试连接，但翻译任务会在启动前报错；请将必要参数改由接口服务配置提供，并移除 URL 查询部分。

### HTTP 401、404 或 429 怎么办？

- **401/403**：检查 API key 是否正确，以及该 key 是否有调用权限。
- **404**：检查接口地址路径和模型名是否符合服务商要求。
- **429**：检查调用频率限制、账户额度和服务端配额。

插件会在失败提示中给出脱敏后的技术详情。频率限制导致的 429 会按 2、4 秒间隔最多重试两次；余额不足导致的 429 直接停止。连接、超时、408/409 和 5xx 使用相同的短暂故障策略；401/403、404 及明确配置错误直接停止。响应截断时，只续传未确认后缀；单段仍失败会进入一轮合并补译，最终仍失败则不导入译本。续传、拆分及重试均进入实际请求计数，单篇不设次数上限。

### 服务商不支持 50000 token 怎么办？

请检查服务商及所选模型是否支持最大输出 `50000`。插件会明确报告配置错误，不自动降档。明确拒绝参数名时允许切换 `max_tokens` / `max_completion_tokens`，两种参数的值均保持 `50000`。

### 为什么第一次翻译较慢？

插件装好后，Zotero 启动时会自动在后台安装 BabelDOC 环境并下载字体、布局模型等资源；进度窗口会显示当前安装步骤，未装完时翻译会从中断处继续。资源缓存位于 `%USERPROFILE%\.cache\babeldoc`。

### 如何卸载？

在 Zotero **工具 → 插件** 中移除本插件。若要删除它下载的运行环境和缓存，请手动删除 `%LOCALAPPDATA%\babeldoc-for-zotero` 与 `%USERPROFILE%\.cache\babeldoc`。

## 测试

仓库中的 JavaScript 回归测试需逐个运行，例如：

```powershell
node tests/config-redaction.test.js
node tests/manifest.test.js
node tests/translation-queue.test.js
node tests/translation-runner.test.js
```

其他 `tests/*.test.js` 文件也可用 `node tests/<file>.test.js` 逐个运行。Python runner 策略测试运行：

```powershell
python tests/test_runner_policy.py
python tests/test_min_requests.py
python tests/test_request_policy_e2e.py
```

`tests/test_min_requests.py` 文件头说明了如何启动本地 mock OpenAI 服务，并用已安装的 BabelDOC 环境对一页 PDF 做端到端检查。
`python tests/test_fragment_repair_e2e.py` 会使用 `ztest/samples/liu2022/original.pdf` 和真实 runner 验证坏片段响应被整组补译；可用 `ZTF_FRAGMENT_REPAIR_PDF` 指定样本文件、用 `ZTF_BABELDOC_PYTHON` 指定 BabelDOC 环境的 Python。显式运行时缺少所需文件会报告路径错误，不会跳过。
`python tests/test_pdf_unicode.py` 验证译本中的标准汉字可直接提取、搜索，并核对修复前后的页面渲染一致。

## 许可证与致谢

本项目采用 GNU Affero General Public License v3.0 only（AGPL-3.0-only）；许可证全文见 [LICENSE](LICENSE)（亦可参阅 [GNU 官方文本](https://www.gnu.org/licenses/agpl-3.0.txt)）。感谢开源项目 BabelDOC 提供 PDF 版面解析与翻译引擎。

## 0.2.1 翻译质量与版式控制

- BabelDOC 识别出的跨栏、跨页连续段落会以 `continues_in` / `continued_from` 标记发送给模型。提示词要求模型分别翻译各自的连续片段，不搬动内容，也不返回空文本。片段仍检查空输出、未翻译的英文和占位符；长度与编辑距离检查只对片段放宽。
- **失败段落合并补译**默认开启。主翻译完成后，所有空输出、缺失项、质量检查失败或占位符不匹配的段落进入一次合并补译流程。补译请求会携带前后段落作为只读上下文；为满足输出 token 上限，补译可能分成多个请求，但都计入“单篇最多请求次数”。补译仍失败的段落保留原文并计入失败数。
- **翻译参考文献**默认关闭。检测到 References / Bibliography / 参考文献标题或参考文献版面类后，后续文献条目会留在原文。页眉、页脚、页码、IEEE 授权下载戳、DOI、ISBN 与版权边注也会保留原样，不发送给模型；即使版面分析把页眉页脚误标为正文，只要同一段文字（忽略其中的数字）在两页以上的页面顶部或底部重复出现，也会按页眉页脚保留。
- 正文段落按原始字号分组；同组最终字号比例目标相差不超过 5%。系统先压缩段间距，再使用字号组的共同缩放比例，比例下限为 0.8。只有单段在压缩段间距并缩到 0.8 后仍无法排下时，才允许该段单独低于下限，并记录在日志里。图表、表格和独立公式不应用此正文策略，包含行内公式的正文仍会参与分组。
- 每个任务会在 Zotero 数据目录的 `babeldoc-for-zotero/logs/<时间>-<附件 key>.jsonl` 写入脱敏日志，保留最近 20 个任务日志。日志只含请求和段落摘要、最多 80 字的输入预览、检查结果、补译状态及字号统计，不含 API key 或完整正文。失败提示和完成消息会显示日志路径。

设置页新增两项：**失败段落合并补译（单轮；超出单次预算时可用多个请求，均计入请求上限）**（默认开启）和**翻译参考文献**（默认关闭）。补译最多执行一个合并轮次；当失败段落超过单次输出预算时，该轮会拆成多次请求，且每次都计入请求上限。

## 0.5.0 改进

- 新增输出格式选择：单语中文译本、左右双语对照或两者同时生成，默认单语；三种模式共享一次翻译，不增加模型请求。
- 双语每页左侧原文、右侧译文，页码一一对应；内部引用在各自一侧跳转，外部链接两侧可用。
- 设置页精简下载源与按钮，窄窗口自动换行；“安装 / 修复环境”统一为“修复环境”，新增“关于”与项目主页链接。

## 0.4.2 改进

- 单次请求最大输出固定为 `50000` token，单篇不再限制请求次数；短暂连接故障最多重试两次，余额、鉴权与配置错误立即停止。
- 主翻译后始终执行一轮合并补译；摘要、正文、标题、图表标题、已识别表头等应译内容仍有失败时整篇失败，不导入译本，并报告页码、段落和原因。
- 行内公式（含底数、上下标与相邻运算符）作为整体排版；摘要局部样式在补译中简化，引用颜色只覆盖引用文字。
- 译本恢复原文内部跳转与外部链接。
- 表格只翻译列标题和有通用含义的行标题，表体、方法名、数据集与缩写保持原文；无法可靠识别表头时整表保留并提示表号与页码。

## 0.4.1 改进

- 翻译进度窗口持续显示当前阶段的实际用时，长请求期间也能看到更新；切换阶段后重新计时。
- 修正部分汉字复制为 CJK 兼容码位的问题，使标准汉字可在译本中正常搜索、复制，页面字形和位置保持不变。
- 更新公开仓库的下载与自动更新说明，并同步插件和更新清单的版本号。

## 0.4.0 改进

- 插件装好后，Zotero 启动时自动下载并安装固定版本的翻译环境；无需手动安装 Python 或 Miniconda。
- 设置页提供下载源选择和“安装 / 修复环境”，修复时完整重新安装翻译环境。

## 0.3.1 改进

- 表格排版：同一行里相邻的单元格不再合成一段，每格单独翻译并放回原位置；同一张表使用统一字号，译文限制在单元格内；OS、IPs 等很短的单元格也会翻译。
- 表题开头几个字母被漏在框外时（如 “Tabl”），会并回表题一起翻译。

## 0.3.0 改进

- 改名为 **BabelDOC for Zotero**，插件 ID 改为 `babeldoc-for-zotero@veiluna.github.io`。首次启动时自动带入旧版 Translate Files for Zotero 的设置；旧插件请在 Zotero 插件管理中手动卸载。
- 安装脚本固定安装 BabelDOC 0.6.4；翻译环境版本不符时直接提示重新运行安装脚本。
- 模型原样返回的段落（人名、单位、表格数字等）保持原 PDF 的字形和位置，不再重新排版。
- 新增回放回归测试：用本机模拟接口检查每个段落是翻译还是保留，不消耗接口请求。

## 0.2.6 改进

- 首页面含邮箱的作者、单位区块保留原始版式，不再送入模型翻译。

## 0.2.5 改进

- 图中的图例等在少数几页重复出现的短文字不再被当作页眉页脚而保留英文。

## 0.2.4 改进

- 不再用规则逐类判断人名、作者行、单位、标签：翻译提示词要求模型保留人名、作者署名、单位、邮箱、网址和数据集/攻击标签等标识符，正文全部翻译。只有至少含 4 个普通小写英文单词的句子才会检查“是否仍是英文原文”，其余短条目原样保留也算成功，不再占用补译请求。
- 与 Zotero Attanger 等自动移动/重命名附件的插件兼容，不再误报 FOREIGN KEY 错误。

## 0.2.3 改进

- 部分模型（包括 chatanywhere 上的部分模型）不再接受 `max_tokens`，要求改用 `max_completion_tokens`；也有模型不接受 `temperature`。服务端会先返回不消耗额度的 HTTP 400。插件遇到这种拒绝时，会自动换成对方要求的参数，在同一次计数的请求里重发一次，并记住结果：测试连接成功后，以及翻译完成后，都会保存到隐藏设置 `extensions.babeldoc-for-zotero.tokenParam`，之后的请求直接使用。其他错误不重试。

## 0.2.2 改进

- 网址、邮箱、域名、两个词以内的标识符（如 "HTTP"、"CIC-IDS-2017"），以及带有作者证据（邮箱，或紧贴姓名的单位标记，如 `Liu{v1}`、`Liu1`、`Liu*`）的作者署名行，可以原样保留（仍检查占位符），不再被当成"未翻译"而送去补译。标题（如 "II. BACKGROUND AND RELATED WORK"、"A Novel Deep Learning Architecture"）和普通英文句子仍必须翻译。
- 接口返回错误时，日志的 `provider_message` 字段只记录一个固定的诊断类别：`quota_or_rate_limit`、`context_or_max_tokens`、`model_unavailable`、`authentication`、`json_mode_unsupported`、`content_filtered`、`timeout`、`invalid_request` 或 `unclassified`。服务端返回的原文一律不写入日志，因为其中可能包含被回显的文档内容。

真实 PDF 回归应使用 `ztest/samples/liu2022/original.pdf` 和 `ztest/samples/P01.pdf`，以 `tests/mock_openai_server.py --bad-fragments` 提供故意错误的跨段输出。检查 JSONL 中的主请求数、片段条目数和补译请求数；用 PyMuPDF 比较每页正文中同一原始字号组的最小/最大字号；并确认页眉、页脚、下载戳和参考文献文本与原文逐字节一致，输出文本中没有 `{v…}` 占位符。
