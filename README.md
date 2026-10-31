# BabelDOC for Zotero

<p align="center">
  <a href="https://github.com/Veiluna/babeldoc-for-zotero/releases/latest"><img alt="最新版本" src="https://img.shields.io/github/v/release/Veiluna/babeldoc-for-zotero?label=%E7%89%88%E6%9C%AC"></a>
  <a href="https://github.com/Veiluna/babeldoc-for-zotero/releases"><img alt="下载量" src="https://img.shields.io/github/downloads/Veiluna/babeldoc-for-zotero/total?label=%E4%B8%8B%E8%BD%BD"></a>
  <img alt="Zotero 7–10" src="https://img.shields.io/badge/Zotero-7%E2%80%9310-CC2936">
  <img alt="Windows" src="https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-Windows-0078D6">
  <a href="LICENSE"><img alt="AGPL-3.0" src="https://img.shields.io/github/license/Veiluna/babeldoc-for-zotero?label=%E8%AE%B8%E5%8F%AF%E8%AF%81"></a>
</p>

在 Zotero 里一键把外文 PDF 翻译成**保持原版式的简体中文 PDF**，也可以生成**分栏中英对照 PDF**。翻译引擎是开源的 [BabelDOC](https://github.com/funstory-ai/BabelDOC)，模型用你自己的 OpenAI 兼容接口（填写接口地址、API key 和模型名即可）。

当前版本：**0.8.0**　|　[下载最新版](https://github.com/Veiluna/babeldoc-for-zotero/releases/latest)　|　[更新日志](CHANGELOG.md)　|　[问题反馈](https://github.com/Veiluna/babeldoc-for-zotero/issues)　|　[English](#english)

> 从旧版 Translate Files for Zotero 升级：Zotero 会把本插件识别为新插件，设置会自动迁移；旧版 Translate Files for Zotero 需手动卸载。

## 功能

- **右键即译**：在 PDF 附件上右键 → **BabelDOC for Zotero** → **翻译成中文 PDF** 或 **生成双语对照 PDF**。只在你点击时运行，不会自动翻译。
- **保持版式**：译文放回原来的位置，图内可编辑的文字（坐标轴、图例、题注）也会翻译；原文的内部跳转、引用和外部链接在译本中仍可点击。
- **分栏双语对照**：双栏论文的每一页拆成“左栏英文 | 左栏中文”和“右栏英文 | 右栏中文”两页；单栏文档输出“原文整页 + 中文整页”。中文放不下时页面加高，字号不变。在 Zotero 阅读器里拖选时，英文侧和中文侧互不混入。
- **自动导入**：译本作为新附件放到原条目下，标题为“原标题（中文译本）”或“原标题（中英对照）”，原 PDF 不变。
- **省请求**：整篇文档按阅读顺序合并成尽量少的请求发送，适合有次数限制的接口。
- **失败也有结果**：个别段落翻译失败时保留原文并照常导入译本，完成提示会标明有多少段没译好，诊断日志里有页码。
- **一键安装环境**：首次启动时自动下载所需的 Python 和 BabelDOC，无需手动安装 Python 或 Miniconda。

## 系统要求

- Windows（含 Windows PowerShell 5.1）。
- Zotero 7–10，主要在 Zotero 10（64 位）上测试。
- 一个 OpenAI 兼容的 Chat Completions 接口、API key 和模型名。模型需要支持单次输出 50000 token。
- 首次使用需联网下载数百 MB 的运行环境、字体和模型。

## 安装

1. 在 [Releases](https://github.com/Veiluna/babeldoc-for-zotero/releases/latest) 下载 `babeldoc-for-zotero-<版本>.xpi`。
2. 打开 Zotero **工具 → 插件**，选择 **从文件安装**，选中下载的 `.xpi` 文件。
3. 插件装好后启动 Zotero 时，插件会自动在 `%LOCALAPPDATA%\babeldoc-for-zotero` 创建独立的 Python 3.12 环境，并在后台安装 BabelDOC；不会修改系统 PATH。通常需要几分钟，没装完时第一次翻译会从中断处继续。

之后插件会通过 Zotero 自动更新。

也可以从源码打包：在仓库根目录运行 `powershell -ExecutionPolicy Bypass -File .\scripts\build-xpi.ps1`，产物在 `build\babeldoc-for-zotero-<版本>.xpi`。

## 设置

打开 Zotero **设置 → BabelDOC for Zotero**：

| 设置项 | 说明 |
| --- | --- |
| 接口地址 | 例如 `https://api.example.com/v1`。也可以直接填完整的 `.../chat/completions` 地址。地址里不要带 `?` 查询参数。 |
| API key | 接口服务商提供的密钥。 |
| 模型名 | 服务商支持的模型名称。 |
| 翻译参考文献 | 默认关闭，参考文献保留原文。 |
| 下载源 | 默认国内镜像，也可切换为官方源。 |

填好后点 **测试连接**，插件会发送一条很短的测试请求。环境损坏时可以点 **修复环境**，它会删除整个运行环境后重新安装。

## 使用

1. 右键 PDF 附件（或带 PDF 附件的条目），选择 **BabelDOC for Zotero → 翻译成中文 PDF** 或 **生成双语对照 PDF**。
2. 进度窗口会显示当前阶段和用时。多个任务按顺序排队，一次处理一篇。
3. 完成后，译本出现在原条目下方。完成提示会显示用了多少次请求，以及是否有段落保留了原文。

## 翻译范围

- 会翻译：正文、标题、摘要、图表题注、图内可编辑文字。
- 保留原文：页眉、页脚、页码、版权和下载水印、DOI；人名、作者、单位、邮箱、网址；公式、数字、缩写和专有名词；参考文献（默认）。
- 暂不翻译：表格内的文字和表注（表格题注照常翻译）；图片里的文字（不做 OCR）。

## 已知限制

- 只支持单栏和左右对称的双栏版式，暂不支持三栏及以上或左右不对称的版式。
- 双语对照中的划词隔离依赖 Zotero 阅读器内部结构，Zotero 升级后可能失效；其他 PDF 阅读器不保证同侧划词。
- 跨页划词在 Zotero 10.0.7 上未生效（[#57](https://github.com/Veiluna/babeldoc-for-zotero/issues/57)）。
- 中英文行的基线只是近似对齐（[#63](https://github.com/Veiluna/babeldoc-for-zotero/issues/63)）。
- 目前只支持 Windows。

## 隐私与 API key

- 除首次下载运行环境外，插件只把需要翻译的文字发送到你填写的接口。
- API key **以明文**保存在 Zotero 的 `prefs.js` 中（设置页只是遮挡显示），请保护好你的 Windows 账户和 Zotero 数据目录。
- API key 通过环境变量交给本机翻译进程，不会出现在进程命令行或日志中。
- 每次翻译会在 Zotero 数据目录的 `babeldoc-for-zotero/logs/` 写一份诊断日志（保留最近 20 份），只含摘要和最多 80 字的预览，不含 API key 或完整正文。

## 常见问题

**第一次翻译为什么很慢？**
首次使用要下载运行环境、字体和版面模型。之后的耗时主要取决于文档长度和模型速度。

**提示 401 / 403 / 404 / 429？**
401/403：检查 API key 和调用权限。404：检查接口地址和模型名。429：频率限制会自动重试两次；余额不足会直接停止，请检查账户额度。

**服务商不支持 50000 token 输出？**
插件会明确报错，不会自动降低数值。请换一个支持长输出的模型。

**如何卸载？**
在 Zotero **工具 → 插件** 中移除。如需彻底清理，再手动删除 `%LOCALAPPDATA%\babeldoc-for-zotero` 和 `%USERPROFILE%\.cache\babeldoc`。

## 参与开发

欢迎通过 [Issues](https://github.com/Veiluna/babeldoc-for-zotero/issues) 反馈问题。测试在 `tests/` 下：Node 测试用 `node tests/<文件>.test.js` 逐个运行，Python 测试用 `python tests/<文件>.py` 运行。

## 许可证与致谢

本项目采用 [GNU Affero General Public License v3.0 only](LICENSE)（AGPL-3.0-only）。感谢 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 提供 PDF 版面解析与翻译引擎。

## English

**BabelDOC for Zotero** is a Zotero 7–10 plugin for Windows that translates PDF attachments into Simplified Chinese while keeping the original layout, using [BabelDOC](https://github.com/funstory-ai/BabelDOC) and any OpenAI-compatible Chat Completions API you configure (base URL, API key, model).

- Right-click a PDF attachment → **BabelDOC for Zotero** → *Translate to Chinese PDF* or *Generate bilingual PDF*.
- The bilingual output places each column's English and Chinese side by side; links, figures' editable text and the table of contents are preserved.
- The result is imported as a new attachment; the original PDF is never modified.
- The Python runtime and BabelDOC are installed automatically on first start.
- The whole document is packed into as few API requests as possible.

Download the `.xpi` from [Releases](https://github.com/Veiluna/babeldoc-for-zotero/releases/latest) and install it via **Tools → Plugins → Install Plugin From File**. The user interface is in Chinese and English. Licensed under AGPL-3.0-only.
