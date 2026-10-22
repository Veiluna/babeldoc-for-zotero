# CODING_STANDARDS — BabelDOC for Zotero

> 状态：0.7.0 起生效（草案，等待用户审阅）。本文件是代码编写、测试、审查与发布的规范来源。
> 与 AGENTS.md / CLAUDE.md 冲突时：AGENTS.md 中旧的模型分工流程不适用于当前工作方式；编码约定以本文件为准。
> 用户明确需求 > 本文件 > AGENTS.md 旧条款。

## 1. 适用范围

适用于 `addon/`（Zotero 插件 JS）、`addon/content/python/`（runner 与 helper）、`tests/`、`scripts/`、`docs/`。

## 2. 架构与模块边界

分层（自外向内）：

1. **插件 JS**：右键菜单、队列、进度窗口、子进程边界、附件导入。不直接解析 PDF。
2. **runner**（`ztf_babeldoc_runner.py`）：BabelDOC 编排、请求预算、翻译验证、后处理调度、`ZTF_*` stdout 协议。唯一允许发起模型请求的模块。
3. **helper 模块**：单一职责、无网络。现有：figure_text、table2、links、punctuation_cleanup、table_text、table_headers、inline_formulas、style_guards。
4. **测试与夹具**：质量夹具 `tests/pdf_quality_check.py` 属于验收门，不是产品代码。

规则：

- helper 不得发起网络请求；表2 补译是唯一例外，且必须在 runner 中经 `RequestBudget`、每次文档最多 1 次。
- 新 helper 必须同时：放 `addon/content/python/`、在 runner 用 `_load_helper(name)` 加载、加入 `runner.js` 的复制清单、被 `tests/helper-manifest.test.js` 自动覆盖。
- 模块接口用明确签名 + docstring；跨模块 dict 结构在 docstring 写明键；禁止未文档化的隐式全局状态。

## 3. 语言与命名

- 代码标识符、文件名、协议字段：英文。注释、docstring、提交信息、文档：中文。
- **界面文字一律 Fluent**（`addon/locale/zh-CN` 与 `en-US` 同时提供键）；禁止硬编码中英文 UI 文案；测试用 l10n mock 注入断言。
- Python：模块常量 `UPPER_SNAKE`；函数/变量 `snake_case`；私有 `_prefix`；类型标注用 3.12 语法（`X | None`）。
- JS：变量 `camelCase`、构造器 `PascalCase`；新增代码与所在文件既有风格一致。

## 4. Python 规范

- 仅标准库 + pymupdf（runner 例外：openai 经 BabelDOC）；不新增第三方依赖。
- 每个模块顶部 docstring：职责、输入输出、失败模式。
- **资源**：PyMuPDF Document 必须 `try/finally` 关闭；写 PDF 用"临时文件 + `os.replace`"；异常路径清理临时文件。
- **异常**：后处理失败不得阻断 PDF 交付（记录结构日志 + warning）；禁止无记录的裸 except；用户可见错误必须脱敏。
- **不允许静默地部分完成**：任何回退/降级/跳过都必须写结构化日志（event + 原因 + 计数）。
- 坐标/数值计算先做有限性检查；redaction 必须显式 `images=0, graphics=0`（除非有意删除图像）。
- **中文有效性（Q11）**：只用于"送译的短值/表头"；只拒绝纯数字、纯标点、与原文相同的回声；含专名/数字的中英混合译文视为有效；**整句不应用该回退**。
- 表格字号与正文字号解耦：表格段落不参与正文统一缩放（0.95 等）。

## 5. JavaScript（Zotero 插件）规范

- 纯 JS、无构建步骤；只使用 Zotero 10.0.6 实际存在的 API（以安装目录源码为准，不用记忆中的 API）。
- 进度/队列：所有 UI 定时器必须可停止；任何 UI 异常不得中断队列；进度更新不得产生模型请求。
- 进程边界：API key 只经环境变量传递；子进程参数、日志、错误消息必须脱敏。
- 新 UI 文案必须同时更新 zh-CN 与 en-US Fluent 文件。
- 持久化只用 `Zotero.Prefs`（前缀 `extensions.babeldoc-for-zotero.`）。

## 6. 测试规范

- 新逻辑必须配回归测试；**bug 修复必须先写失败测试**（红 → 绿）。
- 测试分层：
  - 纯函数单测；
  - 合成 PDF 端到端（pymupdf 内存/临时文档，不依赖真实论文路径）；
  - runner 集成（mock OpenAI client，断言请求次数与输出）；
  - 真实重跑只用于发布验收，默认用 mock/replay。
- 断言行为而非实现细节；文件操作用 `tempfile`；性能/进度相关断言"每秒文本变化且请求数不变"。
- 组织：Python `tests/test_<module>.py`（unittest）；JS `tests/<area>.test.js`（node 直跑）。
- **发布门**：全量测试绿 + T01 质量夹具 6/6 PASS（真实重跑一次）+ 两轴 code review 无未闭环 CHANGES。

## 7. 日志、数据与安全

- 日志只写结构化 JSONL：event、原因、计数、≤80 字预览；不写正文全文、不写密钥、不写文件内容。
- 脱敏在写日志/错误展示之前完成。
- 不提交 `build/`、`*.xpi`、临时日志、API key。

## 8. 文档规范

- 行为变更：`docs/specs/` + `docs/adr/`；术语变更更新 `GLOSSARY.md`。
- 发布：README 变更记录、handoff、release notes；版本号三处同步（manifest / updates.json / README）。
- 每个模块交付后更新 `docs/architecture/modules.md`：职责、输入输出、不变量、失败模式、依赖。
- 文档与源码冲突：以源码为准并修正文档。

## 9. 提交与审查

- 提交信息：`type(scope): 中文摘要`，正文分点说明原因与影响；一次提交聚焦一件事。
- 每个功能/修复必须经过两轴 code review（Standards + Spec）；CHANGES 未闭环不合并。
- PR 必须包含：变更摘要、测试证据（命令与结果）、已知限制、发布影响。
- 发布清单：全量测试 → T01 → 构建 xpi 并校验 helper 齐全 → Release → 滚动 updates.json → 关闭 issues → 更新 handoff/modules。

## 10. 性能与请求预算

- 每篇文档默认请求数 = 主翻译 + 合并补译；额外请求（表2 补译）必须显式计数、写日志、每篇最多 1 次。
- 进度/UI 不得增加请求。
- 图/表/链接/标点后处理只读源 PDF 与输出 PDF，不调用模型。
- 真实重跑目标：13 页论文 ≤ 15 分钟（基准见 handoff）。

## 11. 版本与兼容性

- 0.7.0 新增"分栏双语"输出模式；保留 0.6.0 单语/双语输出与后处理链。
- 分栏双语：每输出页左半=英文、右半=中文；双栏源页拆 2 个输出页；单栏源页输出 1 页；跨栏元素（标题/作者/摘要/图/表/公式）只保留译文版本并**横跨整页替换**（无英文对照、无留白）；链接/引用/页码按页数翻倍重映射。
- 不得破坏 0.6.0 已发布行为（除非 spec 明确变更并有 ADR）。

## 12. 禁止事项

- 跳过/禁用测试、删除断言以过关。
- 硬编码 UI 文案（必须 Fluent）。
- 用启发式补丁掩盖未定位的根因；修 bug 前必须先有复现。
- 静默吞异常、静默回退、静默跳过。
- 日志/参数中出现 API key。

## 13. 执行方式（Team Agents，0.7.0 详细）

### 13.1 角色与职责

- **Lead**：写 CODING_STANDARDS / spec / tickets / ADR / handoff / 诊断报告；统一接入 `runner.py`、`runner.js` 复制清单与共享接口（**接入层例外**，业务实现仍全部由 teammate 完成）；分配任务；执行全部 git commit / push / PR 合并；跑最终门禁与发布；按里程碑向用户汇报。
- **实现 teammate**：只写所分配票的实现与测试。不得修改 `runner.py`、共享接口、规范与 spec；不得 commit/push；交付时报告：变更文件、测试命令与结果、已知限制。
- **审查者**：每次审查**新开**独立子代理，**不得复用**原实现者或之前任何审查者（包括复审）；每次审查开 2 个：Standards 轴与 Spec 轴各一个，互不共享上下文。
- **Lead 不做代码审查**，只组织审查、归档结论、执行最终门禁与裁决。

### 13.2 任务与依赖

- **GitHub issues** = 正式 tickets：目标、验收标准、阻塞边（blocked_by）、写入范围（write_scopes）。
- **共享任务板** = 内部分配与状态：Lead 创建并指派，teammate claim/complete。
- 票的 write_scopes 必须互不重叠；`runner.py`、`runner.js` 复制清单与公共接口由 Lead 单点修改。
- tickets 拆分结果需用户审批后冻结。

### 13.3 并行与冲突

- 同时 2–3 个实现 teammate，按 tickets 的阻塞边并行；共享文件相关的票串行。
- 并行前先冻结接口（函数签名、数据结构、错误码），写入票描述。
- 写冲突：遇到 FS_STALE 先读后改；不得覆盖他人改动；Lead 合并前检查完整 diff。

### 13.4 审查流程（严格执行）

- 每票完成后：Lead 组织审查 → **新开 2 个独立子代理**（Standards、Spec）。
- 审查输入：`git diff <固定点>...HEAD`（或该票 diff）、对应 issue、spec 路径、测试命令与输出证据。
- 审查者只读：不修改文件、不提交、不跑真实 API。
- 输出：`VERDICT: PASS` 或 `VERDICT: CHANGES` + 必须修复项清单（引用文件行号/规范条款）。
- Lead 归档到 `docs/reviews/0.7.0/<ticket>-<round>.md`，并同步 PR 评论。
- CHANGES：**由原实现 teammate 修改** → 重新提交 → **再新开 2 个子代理复审**（同样不得复用）。
- 同一审查意见连续 3 轮未解决 → 暂停并升级给用户裁决。
- 两轴均 PASS 才可合并进入下一票。

### 13.5 提交与发布

- 只有 **Lead** 执行 git commit / push / 合并 PR；teammate 与审查者不得提交。
- 提交粒度：一票一提交（必要时一票多个小提交）；格式 `type(scope): 中文摘要`，正文写原因与影响。
- 发布门禁：全量测试绿 + T01 质量夹具 6/6（真实重跑）+ 两轴审查 PASS + XPI 含全部 helper + 文档更新。
- 发布流程：PR → main → tag → GitHub Release → 滚动 `updates.json` → 关闭 issues → 等市场抓取。
- **用户审批点仅三个**：规范定稿、spec 定稿、tickets 拆分结果；其余按计划自动执行（异常时暂停询问）。

### 13.6 真实 API 重跑

- 每个发布周期自动允许 **≤2 次**真实重跑（每次约 3 个请求）；超出前先询问用户。
- 重跑产物与 T01 输出归档到 `build/tXX/`，结论写入诊断报告与 handoff。
- 默认用 mock/replay 验证，不消耗额度。

### 13.7 失败处理

- teammate 同一任务**连续失败 3 次** → 暂停并询问用户，不静默接管。
- 环境类失败（网络、额度、沙箱）先修复环境再重试，不计入失败次数。
- 阻塞超过 30 分钟：向用户汇报现状与选项。

### 13.8 汇报与文档

- 里程碑汇报：规范定稿 → bug 诊断报告 → spec → tickets → 每票完成 → 每轮审查结论 → 发布结果。
- 每里程碑更新 `docs/handoffs/0.7.0-handoff.md`；诊断报告 `docs/diagnostics/0.7.0.md`；模块逻辑 `docs/architecture/modules.md`（交付时）。
- 每次汇报必含：结论、证据（命令 + 关键输出）、风险、下一步、需用户决策点。

### 13.9 上下文与技能路线

- 每票实现与每轮审查都使用独立上下文（teammate fresh / 子代理 fresh）。
- 技能路线：`/diagnosing-bugs`（两个 bug）→ `/grill-with-docs`（补细节）→ `/to-spec` → `/to-tickets` → 逐票 `/tdd` 实现 → 每轮 `/code-review`（新审查者）→ `/pr` → 发布 → `/retro`。
- 划词可行性先走 `/research` + `/prototype`，结论回填 spec。
