# 开发与接续说明

本文件写给接手这个仓库的开发者和 AI 模型：读完它就能知道代码在哪、怎么验证、不能碰什么、下一步可以做什么。面向用户的说明见根目录 [README.md](../README.md)，版本变化见 [CHANGELOG.md](../CHANGELOG.md)。

## 1. 项目一句话

Zotero 桌面插件（纯 JavaScript，无构建步骤）+ 本机 Python 子进程（BabelDOC 0.6.4）。用户右键 PDF 附件，插件启动 Python runner，用用户的 OpenAI 兼容接口把整篇 PDF 翻译成保持版式的简体中文 PDF（或分栏中英对照 PDF），再作为新附件导入 Zotero。

- 仓库：`Veiluna/babeldoc-for-zotero`，许可证 AGPL-3.0-only。
- 插件 ID：`babeldoc-for-zotero@veiluna.github.io`；偏好前缀：`extensions.babeldoc-for-zotero.`。两者已定，**不得更改**（ADR 0003）。旧前缀 `extensions.zotero-translate-files.` 只用于一次性迁移。
- 目标：Zotero 10（64 位，Windows），兼容 Zotero 7+；`manifest.json` 声明 `6.999`–`10.*`，必须保留 `applications.zotero.id`、https `update_url`、`strict_max_version`。
- BabelDOC 固定 0.6.4。runner 大量依赖它的内部函数，升级 BabelDOC 前必须重新验证。

## 2. 代码地图

```
addon/
  bootstrap.js                 插件生命周期，加载 content/modules/*
  manifest.json / prefs.js     清单与默认偏好
  locale/{zh-CN,en-US}/*.ftl   全部界面文字（Fluent），两种语言同时加键
  content/
    main.js                    入口，注册菜单、队列、阅读器钩子
    preferences.{xhtml,js}     设置页（测试连接用 Zotero.HTTP.request，不用 window.fetch）
    modules/
      menu.js                  右键菜单两项：单语 / 双语
      queue.js                 FIFO 队列、进度窗口、完成提示
      runner.js                启动 Python 子进程、复制 helper、解析 stdout 协议
      importer.js / attachments.js  译本导入为新附件（兼容 Attanger 等插件）
      installer.js             首次启动自动安装 uv + Python 3.12 + BabelDOC 0.6.4
      config.js                偏好读写、接口地址归一化、报错脱敏
      readerSelection.js       双语 PDF 的阅读器划词隔离（依赖 Zotero 阅读器内部结构）
    python/
      ztf_babeldoc_runner.py   编排 BabelDOC、请求预算与打包、译文检查、合并补译、日志。唯一能发模型请求的模块
      ztf_figure_text.py       图内文字恢复与对齐
      ztf_links.py             内部跳转与外部链接恢复
      ztf_inline_formulas.py   行内公式整体排版
      ztf_punctuation_cleanup.py  重复标点归一
      ztf_style_guards.py      样式保护
      ztf_split_*.py           0.8.0 分栏双语：types（共享结构）、columns（认栏）、lines、margins（页眉页脚水印）、variants（各栏中文排版变体）、compose（合成输出页）
scripts/build-xpi.ps1          打包 XPI，并生成 build/updates.json
tests/                         Node 与 Python 回归测试、mock 接口、质量检查脚本、fixtures
docs/adr/                      架构决策记录（编号递增，只追加，被取代时标注）
docs/specs/                    各版本规格
GLOSSARY.md                    领域术语（写代码、写文档时用这里的词）
CODING_STANDARDS.md            编码、测试、审查、发布规范（规范来源）
```

### JS 与 Python 的接口

- runner.js 把 `addon/content/python/` 下的 helper 复制到运行目录（清单在 `runner.js` 约 200 行处），再启动 `ztf_babeldoc_runner.py`。
- **新增 helper** 必须同时：放进 `addon/content/python/`、在 runner 用 `_load_helper(name)` 加载、加入 `runner.js` 复制清单；`tests/helper-manifest.test.js` 会检查。
- 输入通过环境变量：`ZTF_OPENAI_API_KEY`（runner 读取后立即从环境中删除）、`ZTF_SPLIT_DUAL`、`ZTF_TRANSLATE_REFERENCES`、`ZTF_TOKEN_PARAM`、`ZTF_JOB_LOG_PATH` 等。
- 输出通过 stdout 行协议：`ZTF_PROGRESS <json>`、`ZTF_RESULT <json>`、`ZTF_WARNING <json>`、`ZTF_ERROR <json>`。
- 每个任务在 Zotero 数据目录 `babeldoc-for-zotero/logs/<时间>-<附件 key>.jsonl` 写脱敏日志，保留最近 20 份。排查问题先读这个日志。

## 3. 关键行为（改动前必须知道）

- **请求策略**：整篇按阅读顺序收集送译段落，按输出 token 预算贪心打包成最少的连续批次；估算 `ceil(源 token × 1.6 + 20)`，每批最多用 `50000` 的 90%。截断时只确认已完整解析的项，按实际扩展比例重新打包剩余部分。单篇不限请求次数，但所有网络尝试都要计数并记日志。
- **重试**：连接、超时、408/409、非余额不足的 429、5xx 最多重试两次（间隔 2、4 秒）；鉴权、余额不足、明确配置错误直接停止。SDK 自带重试保持 0。只有在服务端明确拒绝参数名时，才在 `max_tokens` / `max_completion_tokens` 之间切换，数值不变。
- **补译与降级**：主翻译后始终有一轮合并补译；断句组仍失败时拆片段再译 1 次。最终失败段落保留原文。只要 BabelDOC 产出了 PDF 且至少一段翻译成功，就降级导入（ADR 0004）。
- **保留段落**：页眉页脚（同基线文字出现在 ≥1/3 页）、页码、版权和下载水印、DOI、参考文献（默认）、首页含邮箱且不是完整句子的作者块。人名、单位、术语的取舍**交给提示词和模型**，不在代码里写匹配规则。
- **表格翻译已封存**（ADR 0007）：表内文字与表注保留原文，题注照常翻译。
- **分栏双语**（ADR 0008、0009）：认栏只用 PDF 底层数据（内容流回跳点为主、同基线空隙为备），**不用视觉启发式或 pt 容差**；YOLO 版面框只用于图、表、公式范围。跨栏元素只放中文版。单页合成出错只影响该页。
- **API key**：只经环境变量传给子进程，不得出现在命令行参数、日志或报错中；所有报错文本统一脱敏。日志里服务端返回的原文只记固定诊断类别。

## 4. 本地开发

- 打包：`powershell -ExecutionPolicy Bypass -File scripts/build-xpi.ps1` → `build/babeldoc-for-zotero-<版本>.xpi`。`build/` 和 `*.xpi` 不提交。
- 调试 Zotero 时用独立配置：`zotero.exe -no-remote -profile <临时目录> -ZoteroDebugText`；只关闭自己启动的进程，不碰用户正在用的 Zotero。
- 不需要 Zotero 也能跑 runner：建一个 Python 3.12 虚拟环境装 `babeldoc==0.6.4`，用 `tests/mock_openai_server.py` 当接口，设置上面的环境变量后直接运行 `ztf_babeldoc_runner.py`。
- **不要消耗真实接口额度做测试**，一律用 mock 或回放。

## 5. 测试

Node 测试逐个运行：

```powershell
node tests/<文件>.test.js
```

Python 测试（需要 BabelDOC 环境的会在文件头说明）：

```powershell
python tests/test_runner_policy.py
python tests/test_min_requests.py
python tests/test_degraded_output.py
python tests/test_request_policy_e2e.py
python tests/test_split_columns.py       # 以及其他 tests/test_split_*.py
python tests/test_replay_classification.py   # 回放回归，约 1.5 分钟，不耗请求
```

- 回放回归检查每个段落是“送译”还是“保留”。有意改变分类时，用 `ZTF_UPDATE_REPLAY_BASELINE=1` 重写 `tests/fixtures/replay/` 基线，并在提交里说明变化。
- 分栏双语质量检查：`tests/split_quality_check.py`；整体质量夹具：`tests/pdf_quality_check.py`。
- 真实 PDF 样本不在仓库里（`ztest/samples/`），相关测试可用 `ZTF_FRAGMENT_REPAIR_PDF`、`ZTF_BABELDOC_PYTHON` 指定路径；缺文件时报错，不会跳过。
- 不得跳过或禁用测试；新增的非平凡逻辑必须配回归测试。

## 6. 规则

- 编码、测试、审查和发布规范以 [CODING_STANDARDS.md](../CODING_STANDARDS.md) 为准；与之冲突时，用户的明确要求优先。
- 优先最小改动；不为单篇论文的现象新增匹配规则，能放进提示词的放进提示词。
- 审查只拦三类问题：主路径错误、与规格不符、已知样本实测不达标。边界情况记 issue，不阻塞。
- 做出影响架构的决定时，在 `docs/adr/` 追加一篇 ADR；新术语写进 `GLOSSARY.md`。
- 提交信息格式 `type(scope): 中文摘要`；代码标识符用英文，注释和文档用中文。
- 问题与规格记在 GitHub Issues。

## 7. 发布流程

1. 改 `addon/manifest.json` 的版本号，更新 `CHANGELOG.md` 和 README 中的当前版本。
2. 跑测试并打包。
3. PR 合并到 `main`，打 `v<版本>` 标签，创建 GitHub Release 并上传 XPI。
4. 再把 `build/updates.json` 上传到预发布标签 `release` 下，覆盖 `updates.json`（先上传 XPI，后更新清单）。Zotero 从 `https://github.com/Veiluna/babeldoc-for-zotero/releases/download/release/updates.json` 检查更新。**不要删除 `release` 标签。**

## 8. 当前状态与待办（0.8.0 之后）

- 0.8.0 已发布（分栏双语重做，认栏只用 PDF 底层数据）。
- 已知较慢：typesetting 阶段在长文档上可能耗时很久，正在优化。
- 开放 issue：#57 跨页划词（Zotero 10.0.7 上钩子未挂上，已搁置）；#59 #60 #61 #63 #64 为非阻塞的审查建议。
- 路线：先把主要功能做稳，macOS 与多翻译服务以后再做。
- 不支持三栏及以上与左右不对称版式；不做图片文字 OCR。
