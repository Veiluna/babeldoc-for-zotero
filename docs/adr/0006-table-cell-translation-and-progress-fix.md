# ADR 0006：表体翻译策略调整与进度窗口修复（0.7.0）

> 状态：**表格相关决策（决策 1、2、4 与“后果”中的表格部分）已被 ADR 0007 取代**——0.7.0 起表格内容与表注保留原文、仅题注翻译；本文件中表格相关表述仅作封存前记录。进度窗口修复（决策 3）仍有效。

## 背景

0.6.0 发布后，用户论文（Sharafaldin 等 - 2018，CIC-IDS2017 数据集论文）出现两个线上问题：

1. 表 2（Days/Labels）部分单元格翻译、部分保留英文；已翻译单元格字号被 BabelDOC 缩到源字号的 65%（9.96pt -> 6.48pt）。
2. 翻译进度窗口一直 0、计时 0、停在"正在准备 BabelDOC"（旧版本同样存在）。

诊断证据与复现材料见 docs/diagnostics/0.7.0.md。根因：

- 表体规则把 1–3 词短值一律视为保留（Benign/Monday/Harrison/Network Scans 未送译）；
- Q11 中文有效性要求 >=2 个 CJK 实词，短枚举/混合译文被误判回退（日志 21 次）；
- 表体单元格字号来自 BabelDOC typesetting 的 _get_optimal_scale 按单元格宽度自动缩放（0.65），与正文 0.95 缩放无关；
- 进度：Zotero 10.0.6 的 ProgressWindow 异步打开，插件注册的 popup "unload" 监听在 about:blank 文档卸载时提前触发，stopProgressTimer 把 active 置 false，之后 tick 与 ZTF_PROGRESS 均被丢弃。

## 决策

1. **表体内容策略**：只有纯标识符/缩写（含 YES/NO 等大写缩写与 ChangeMamba/CenterPoint 等 CamelCase 标识符）/数字/URL/代码保留；其余英文（1–2 词、普通词形式的人名/产品名如 Swin、整句）一律送译。模型原样返回时按 kept_verbatim 保留原字形与位置，不计为失败。
2. **Q11 中文有效性**：放宽为 >=1 个 CJK 字符、非纯数字/标点、去空白后不等于原文；且只作用于送译短值（词数 <=3 或长度 <=40），整句不应用该回退。
3. **进度窗口**：popup 窗口 load 之前触发的 unload 忽略；load 之后的真实 unload 才停止每秒计时。保留 window.close / closeProgressWindow / stopProgressTimer 的既有收尾路径。
4. **表格字号（#38，T-B3 简化方案，用户定案；0.7.0 已封存，见 ADR 0007）**：patch BabelDOC 表格单元格排版，每格最小缩放 0.9（阶梯 1.0→0.95→0.9）；**严格使用原单元格边框，不做增高/扩展/跨行重排**；0.9 放不下时保持 0.9 并允许文字溢出单元格，写结构化 table_layout_overflow（含页码/表名）；不改变正文 0.95 策略。helper 缺失或 typesetting API 缺失时提示原因并回退原排版（unavailable）；BabelDOC 版本与插件预期不符时 runner 保持严格阻断（见下）。

## 后果

- 回放分类基线更新：189 条 kept -> translated（单向，无反向；两个样本请求数仍各 1 次）。其中 **131 条**可归因本票规则变化（189-58；逐文件：liu2022 90-33=57、p01 99-25=74），另 **58 条**为 HEAD 基线漂移（liu2022 33、p01 25；漂移是 189 条的子集）。HEAD 副本复跑 2 次结果一致（33/25）；连同工作树运行共 4 次观测，均只有 1 个 paragraph_classification 事件。
  复核方法：
  - 用 `git archive HEAD | tar -x -C <临时目录>` 得到 HEAD 副本；
  - 在副本运行 `ZTF_BABELDOC_PYTHON=<env python> <env python> tests/test_replay_classification.py`（不设 ZTF_UPDATE_REPLAY_BASELINE），得到 HEAD 代码 vs HEAD 基线的漂移 33/25；
  - 新基线 vs 旧基线的 decision diff 为 90/99，全部 kept->translated，无其他字段变化；
  - 样本 sha256：liu2022/original.pdf `26cd0f7d7b1ce274d0db1be470942a8641b8697be306e40f70db9d2037d97d7c`，P01.pdf `426ab22a7c04ca18d7708764495717418c5567e878beaa21c0cf2656b26d9049`。
- 表体少量人名/产品名会发送给模型（通常回声返回，保留原文）；同一请求内批量处理，不增加请求数。
- 规格文档同步更新：docs/specs/output-fidelity-and-progress.md（Q5/Q10/Q11/T4）、docs/specs/translation-quality-and-bilingual.md（用户故事 4、表格条款）。
- 需要 0.7.0 真实重跑（用户在计划内授权）验收表 2 与进度窗口。
- uncertain 表的完成提示已在 0.7.0 修正：走 Fluent 新键（"表头未能可靠识别，表体按内容规则处理"），runner 的 ZTF_WARNING 文本同步更新；旧"整表保留原文"表述已从代码与当前功能文档移除，README 0.4.2 历史章节保留当时记录并加 0.7.0 注记。

## 表格字号（#38）的限制与验收（历史，已封存）

> 0.7.0 暂停表格翻译，本节仅为封存前记录；当前行为见 ADR 0007 与 docs/archive/table-translation.md。

- 仅 patch BabelDOC 0.6.4 的 typesetting API；BabelDOC 版本与插件预期不符时 runner 在翻译前直接报错阻断（保护排版结果），插件文档注明仅支持 0.6.4，升级需适配——不是“安全降级”。
- 严格使用原单元格边框，不增高、不跨行、不手工重排；0.9 放不下时文字允许溢出单元格（用户已确认接受），记录 overflow warning。
- 字号目标为每格 ≥0.9（阶梯 1.0/0.95/0.9），不要求整表统一；未翻译标识符/数字保持源字号。
- 验收：mock 渲染（P01 表 2）已翻译单元格最小字号 8.97pt（源 9.96，目标 ≥8.96），原边框不变，正文 9.96 不变，requests 不变；真实重跑与 T01 质量夹具由 Lead 在发布门禁执行。

## 参考

- issues #36（进度）、#37（表体规则/回退）、#38（字号 patch）
- docs/diagnostics/0.7.0.md
- CODING_STANDARDS.md §8（行为变更需要 spec/ADR）
