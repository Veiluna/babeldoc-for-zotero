# Translate Files for Zotero

当前版本：**0.2.3**。主要支持 **Zotero 10.0.6（64 位）**，兼容 Zotero 7–10。这是一个 Windows 插件。它调用本机安装的 BabelDOC 和用户配置的 OpenAI 兼容接口，生成保持版式的简体中文 PDF，并把译本作为新的文件附件导入 Zotero。

## 功能简介

- 只在用户从条目或 PDF 附件的右键菜单选择“翻译成中文 PDF”后运行，不会自动翻译。
- 按 PDF 的文字层进行翻译，包括可编辑的矢量图表文字和题注；v1 不对图片中的文字做 OCR。
- 只输出单语中文 PDF，不生成双语 PDF；原 PDF 保持不变。
- 结果作为新的已存储文件附件加入 Zotero。同一父条目下的译本标题为“原标题（中文译本）”。
- 插件没有设置页数上限。多个任务按 FIFO 顺序排队，同一时间处理一个；失败后提示错误并继续下一个任务，不自动重试。
- 如果本次进入段落翻译的内容没有任何段落成功，任务会按失败处理。部分段落失败时，任务仍可完成，进度窗口会显示失败段数，失败段落保留原文。
- 整篇文档按阅读顺序合并并尽量用最少的请求翻译；自动术语表提取和逐段重发已关闭，失败段落默认会在主请求后进入一次合并补译。每篇文档的完成状态会显示实际请求次数。

## 系统要求

- Windows 和 Windows PowerShell 5.1。
- Zotero 7–10。当前主要支持 Zotero 10.0.6（64 位）；插件清单声明的版本范围为 6.999 至 10.*。
- Miniconda。安装脚本会创建独立的 Python 3.12 环境，不会激活环境，也不会修改 PATH。
- 一个可用的 OpenAI 兼容 Chat Completions 接口、API key 和模型名。

## 安装

### 1. 安装 Miniconda

如果尚未安装，请先安装 Windows 版 Miniconda。安装时无需将 Conda 加入 PATH；安装脚本会查找常见位置，也可以通过 `-CondaPath` 指定 Miniconda 安装目录或 `conda.exe`。

### 2. 安装 BabelDOC

在仓库根目录打开 Windows PowerShell，运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-babeldoc.ps1
```

`-ExecutionPolicy Bypass` 只对这次 PowerShell 命令生效。常用参数如下：

| 参数 | 作用 |
| --- | --- |
| `-CondaPath` | Conda 安装目录或 `conda.exe` 路径；省略时自动查找。 |
| `-EnvPrefix` | 环境目录；默认是 `%LOCALAPPDATA%\zotero-translate-files\babeldoc-env`。 |
| `-PythonVersion` | Python 版本，默认 `3.12`。 |
| `-BabelDocVersion` | 可选的 BabelDOC 版本，例如 `0.6.4`；省略时安装与 Python 版本兼容的最新版。 |
| `-SkipWarmup` | 跳过 BabelDOC 字体和布局模型资源预下载。 |
| `-IndexUrl` | 指定 pip 包索引。国内用户可按需使用镜像，例如清华 PyPI 镜像；该参数只传给 pip。 |
| `-OfflineAssets` | 从已有的 BabelDOC 资源文件或目录恢复字体和模型资源。它不离线安装 Conda/Python 依赖。 |

例如，指定 Miniconda 路径和 pip 镜像：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-babeldoc.ps1 `
  -CondaPath 'G:\ProgramData\miniconda3' `
  -IndexUrl 'https://pypi.tuna.tsinghua.edu.cn/simple'
```

使用已有离线资源时，可添加 `-OfflineAssets 'D:\babeldoc-assets.zip'`。安装脚本默认会预下载资源；若添加 `-SkipWarmup`，则会跳过在线预下载，也会跳过指定的离线资源恢复。

安装脚本使用 [scripts/babeldoc-constraints.txt](scripts/babeldoc-constraints.txt) 固定依赖约束 `bitstring<5`，以兼容 BabelDOC 对 `bitstring.BitStream` 的使用。安装完成后，脚本会输出 BabelDOC 可执行文件路径。

### 3. 打包插件

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-xpi.ps1
```

脚本会把插件打包到 `build\zotero-translate-files-<版本>.xpi`。也可以直接使用已构建的 `.xpi` 文件。

打包时也会根据 `addon/manifest.json` 生成 `build/updates.json`。发布自动更新时，将该文件作为 `release` 版本的 `updates.json` 资源发布，并将对应版本的 XPI 发布到 `v<版本>` 标签下。清单中的自动更新地址为 `https://github.com/Veiluna/zotero-translate-files/releases/download/release/updates.json`。由于此 GitHub 仓库目前是私有仓库，Zotero 在仓库或发布资源公开前无法读取更新清单，自动更新检查会静默失败；这不影响从本地 XPI 安装和使用。

### 4. 在 Zotero 安装插件

打开 Zotero 的 **工具 → 插件**，选择 **从文件安装**，然后选中 `.xpi` 文件。

### 5. 填写插件设置

打开 Zotero **设置 → Translate Files for Zotero**，填写：

1. **接口地址**：例如 `https://api.example.com/v1`。插件会把根地址归一化为 Chat Completions endpoint，例如 `https://api.example.com/v1/chat/completions`。主机根地址会补成 `/v1/chat/completions`；版本路径（如 `/v1`）和 `/v1beta/openai` 会补上 `/chat/completions`；已填写完整的 `/chat/completions` endpoint 保持不变。
2. **API key**：接口服务提供的密钥。
3. **模型名**：接口服务支持的模型名称。
4. **BabelDOC 路径**：粘贴安装脚本输出的路径。可填写环境中的 `Scripts\babeldoc.exe`，也可填写环境根目录下的 `python.exe`。
5. **模型单次最大输出 token 数**：默认 `8192`，最小为 `1024`。估算按源文 token 的约 `1.6` 倍加每段 `20` token，并保留 `10%` 余量；超过预算的段落会按阅读顺序装入后续请求。
6. **单篇最多请求次数**：默认 `20`，最小为 `1`。每次实际 Chat Completions 调用都计数，包括响应截断后的续传和拆分请求。Chatanywhere 对单篇有 `100` 次硬上限，建议保持较低值；到达本项上限后插件会停止任务，避免继续消耗接口额度。

这些设置分别使用 Zotero 偏好键 `extensions.zotero-translate-files.baseURL`、`extensions.zotero-translate-files.apiKey`、`extensions.zotero-translate-files.model`、`extensions.zotero-translate-files.babeldocPath`、`extensions.zotero-translate-files.maxOutputTokens`、`extensions.zotero-translate-files.maxRequests`、`extensions.zotero-translate-files.repairFailed` 和 `extensions.zotero-translate-files.translateReferences`。

点击 **测试连接** 后，插件会向接口发送一次简短的 `ping` 请求，并检查 BabelDOC 可执行文件及对应的 Python 路径是否存在。它不会运行一次完整的 PDF 翻译。

## 使用

1. 在 Zotero 中右键点击普通条目或 PDF 附件，选择 **翻译成中文 PDF**。
2. 插件会显示入队提示。队列按顺序逐个处理，当前任务会打开进度窗口，显示阶段、进度和排队数量。
3. 成功后，译本 PDF 会作为新附件导入对应的父条目，标题为“原标题（中文译本）”；原始 PDF 文件不变。
4. 如果所有进入翻译的段落都失败，任务会显示失败提示且不会导入译本。部分段落失败时，进度窗口的完成状态会提示失败段数。

## 请求次数与术语一致性

插件先完成段落预处理并剔除不需要翻译的内容，再按文档阅读顺序收集段落（包括跨页和跨栏段落），最后按输出 token 预算贪心打包。对正数段落估算值，这种顺序打包会得到最少的连续批次数；整篇文档放得下时只发一个请求。估算值为 `ceil(源文 token 数 × 1.6 + 20)`，批次最多使用配置输出上限的 `90%`，为中文扩展和 JSON 留出余量。截断后会将最后一个解析项视为未确认，只确认此前的完整项，再按观察到的扩展比例重新打包未确认后缀；如果没有完整项，才将剩余批次二分。单段仍被截断时保留原文并计入失败段数。观察到实际输出长度后，插件会更新扩展比例并重新打包尚未发送的段落。

翻译提示词要求整篇文档内的术语、缩写和专名保持一致，并沿用首次出现时的译法。插件不运行额外的自动术语表请求，也不会在批次失败后逐段重发；失败段落默认进入一次合并补译流程，BabelDOC 的翻译缓存也会绕过，避免失败时保留的原文被后续任务当作译文复用。请求上限统计所有实际发出的 Chat Completions 调用，达到配置上限就停止；HTTP 429 等接口错误只尝试一次。

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

插件会在失败提示中给出脱敏后的技术详情；HTTP 429 等接口错误只尝试一次，不会自动重试。任何截断响应的最后一个解析段落都视为未确认，并与剩余段落按更新后的输出估算重新打包续传；只有没有完整段落时才会将剩余批次拆分。单段响应仍被截断时会保留原文。续传和拆分请求都会计入“单篇最多请求次数”。

### 为什么第一次翻译较慢？

首次运行可能需要下载 BabelDOC 的字体、布局模型等资源，PDF 页面分析和翻译也需要时间。资源缓存通常位于 `%USERPROFILE%\.cache\babeldoc`。

### 如何卸载？

在 Zotero **工具 → 插件** 中移除本插件。BabelDOC 的 Conda 环境可以单独卸载：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-babeldoc.ps1
```

脚本默认删除 `%LOCALAPPDATA%\zotero-translate-files\babeldoc-env` 前要求确认，并保留 BabelDOC 资源缓存。添加 `-Force` 可跳过环境删除确认；只有明确添加 `-RemoveCache` 才会删除 `%USERPROFILE%\.cache\babeldoc`。例如，确认后同时删除环境和缓存：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-babeldoc.ps1 -Force -RemoveCache
```

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
```

`tests/test_min_requests.py` 文件头说明了如何启动本地 mock OpenAI 服务，并用已安装的 BabelDOC 环境对一页 PDF 做端到端检查。
`python tests/test_fragment_repair_e2e.py` 会使用 `ztest/samples/liu2022/original.pdf` 和真实 runner 验证坏片段响应被整组补译；可用 `ZTF_FRAGMENT_REPAIR_PDF` 指定样本文件、用 `ZTF_BABELDOC_PYTHON` 指定 BabelDOC 环境的 Python。显式运行时缺少所需文件会报告路径错误，不会跳过。

## 许可证与致谢

本项目采用 GNU Affero General Public License v3.0 only（AGPL-3.0-only）；许可证全文见 [LICENSE](LICENSE)（亦可参阅 [GNU 官方文本](https://www.gnu.org/licenses/agpl-3.0.txt)）。感谢开源项目 BabelDOC 提供 PDF 版面解析与翻译引擎。

## 0.2.1 翻译质量与版式控制

- BabelDOC 识别出的跨栏、跨页连续段落会以 `continues_in` / `continued_from` 标记发送给模型。提示词要求模型分别翻译各自的连续片段，不搬动内容，也不返回空文本。片段仍检查空输出、未翻译的英文和占位符；长度与编辑距离检查只对片段放宽。
- **失败段落合并补译**默认开启。主翻译完成后，所有空输出、缺失项、质量检查失败或占位符不匹配的段落进入一次合并补译流程。补译请求会携带前后段落作为只读上下文；为满足输出 token 上限，补译可能分成多个请求，但都计入“单篇最多请求次数”。补译仍失败的段落保留原文并计入失败数。
- **翻译参考文献**默认关闭。检测到 References / Bibliography / 参考文献标题或参考文献版面类后，后续文献条目会留在原文。页眉、页脚、页码、IEEE 授权下载戳、DOI、ISBN 与版权边注也会保留原样，不发送给模型；即使版面分析把页眉页脚误标为正文，只要同一段文字（忽略其中的数字）在两页以上的页面顶部或底部重复出现，也会按页眉页脚保留。
- 正文段落按原始字号分组；同组最终字号比例目标相差不超过 5%。系统先压缩段间距，再使用字号组的共同缩放比例，比例下限为 0.8。只有单段在压缩段间距并缩到 0.8 后仍无法排下时，才允许该段单独低于下限，并记录在日志里。图表、表格和独立公式不应用此正文策略，包含行内公式的正文仍会参与分组。
- 每个任务会在 Zotero 数据目录的 `zotero-translate-files/logs/<时间>-<附件 key>.jsonl` 写入脱敏日志，保留最近 20 个任务日志。日志只含请求和段落摘要、最多 80 字的输入预览、检查结果、补译状态及字号统计，不含 API key 或完整正文。失败提示和完成消息会显示日志路径。

设置页新增两项：**失败段落合并补译（单轮；超出单次预算时可用多个请求，均计入请求上限）**（默认开启）和**翻译参考文献**（默认关闭）。补译最多执行一个合并轮次；当失败段落超过单次输出预算时，该轮会拆成多次请求，且每次都计入请求上限。

## 0.2.3 改进

- 部分模型（包括 chatanywhere 上的部分模型）不再接受 `max_tokens`，要求改用 `max_completion_tokens`；也有模型不接受 `temperature`。服务端会先返回不消耗额度的 HTTP 400。插件遇到这种拒绝时，会自动换成对方要求的参数，在同一次计数的请求里重发一次，并记住结果：测试连接成功后，以及翻译完成后，都会保存到隐藏设置 `extensions.zotero-translate-files.tokenParam`，之后的请求直接使用。其他错误不重试。

## 0.2.2 改进

- 网址、邮箱、域名、两个词以内的标识符（如 "HTTP"、"CIC-IDS-2017"），以及带有作者证据（邮箱，或紧贴姓名的单位标记，如 `Liu{v1}`、`Liu1`、`Liu*`）的作者署名行，可以原样保留（仍检查占位符），不再被当成"未翻译"而送去补译。标题（如 "II. BACKGROUND AND RELATED WORK"、"A Novel Deep Learning Architecture"）和普通英文句子仍必须翻译。
- 接口返回错误时，日志的 `provider_message` 字段只记录一个固定的诊断类别：`quota_or_rate_limit`、`context_or_max_tokens`、`model_unavailable`、`authentication`、`json_mode_unsupported`、`content_filtered`、`timeout`、`invalid_request` 或 `unclassified`。服务端返回的原文一律不写入日志，因为其中可能包含被回显的文档内容。

真实 PDF 回归应使用 `ztest/samples/liu2022/original.pdf` 和 `ztest/samples/P01.pdf`，以 `tests/mock_openai_server.py --bad-fragments` 提供故意错误的跨段输出。检查 JSONL 中的主请求数、片段条目数和补译请求数；用 PyMuPDF 比较每页正文中同一原始字号组的最小/最大字号；并确认页眉、页脚、下载戳和参考文献文本与原文逐字节一致，输出文本中没有 `{v…}` 占位符。
