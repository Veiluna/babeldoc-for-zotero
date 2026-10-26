# 表格翻译处理：功能文档、封存清单与已知问题

> 状态：**封存**（0.7.0 起暂停表格翻译，代码不部署、不加载；仅保留题注翻译）。
> 决策记录：ADR 0007（0.7.0 暂缓表格翻译）。
> 恢复指引见文末。

## 1. 当前版本（0.7.0）行为

- **表格内容完全不翻译**：表内单元格（表头、表体、旋转表头、数字/缩写等）一律保留原文，不做字号 patch、不做旋转表头补译、不发表格补译请求。
- **题注/标题翻译**：表格题注（如 "Table 2: Daily Label of Dataset."）作为普通段落照常翻译。
- **表注/表外附注保留原文**：表格下方星号说明、来源注释等表注与表格内容一并保留原文（0.7.0 收敛方案 A）；曾尝试整条合并翻译，因保留原文路径的 P1 缺陷（runner 失败/丢字）回退，见第 3 节已知问题 10（表注合并方案因 P1 缺陷回退）。
- **表外正文照常翻译**：正文中引用表格的句子（如 "Table 2 shows ..."）属普通正文，不受影响。
- **图内文字照常翻译**：图内坐标轴/标签/小表格文字沿用图内文字翻译与恢复能力（0.6.0 行为），不属于表格内容封存范围。
- 表格相关警告（表头识别/字号 patch）不再产生；插件侧原有告警展示逻辑保留为空兼容。

## 2. 被封存的代码清单

### 2.1 辅助模块（addon/content/python/）

| 文件 | 职责 | 处理 |
| --- | --- | --- |
| ztf_table_text.py | 表体内容规则（标识符/短值/整句送译判定）、Q11 中文有效性 | 封存：不再加载 |
| ztf_table_headers.py | 表头/表体角色识别、uncertain 表分析 | 封存：不再加载 |
| ztf_table2.py | 旋转表头识别、翻译、竖排重绘 | 封存：不再加载 |
| ztf_table_layout.py | 表格单元格字号 patch（0.9 下限） | 封存：不再加载 |

封存方式（用户定案 Q1-B）：4 个文件移动到 addon/content/python/archive/ 目录，顶部 docstring 标注"封存（0.7.0 起不启用，见 docs/archive/table-translation.md）"；从 runner.js 的 pythonHelpers 部署清单移除（其中 table_layout 未接线）；runner 不再加载这些模块；封存模块的单测改为从 archive/ 导入（不依赖生产部署）；scripts/build-xpi.ps1 已排除 archive/，封存代码不随插件发布。

### 2.2 runner 接线点（封存前；多数已删除或不可达）

> 下表为封存前的接线位置与行号，用于恢复时参考；当前代码已移除这些接线（行号会漂移）。

| 区域 | 说明 |
| --- | --- |
| 表格 label 常量（约 109-116 行） | BabelDOC 表格类 layout_label |
| helper 加载入口（曾部署 3 个） | _table_text_helper / _table2_helper / _table_headers；_table_layout_helper 曾新增但未接线 |
| 表体送译判定（约 230 行） | _table_body_should_translate |
| 表格分析（约 6180-6238 行） | table_analysis：表头/表体规划与 warning |
| 表体送译/保留分支（约 6358 行） | prepare_paragraph 表体判定 |
| 旋转表头补译（约 6599 行起） | _table2_translations / _restore_rotated_table_headers |
| 字号 patch（约 4398-4497 行） | typesetting 包装、table_layout_warnings 汇总 |
| 表格几何辅助（约 2025-2753 行） | _is_table_layout_paragraph / _table_cell_layout_box / _with_table_cell_bounds / _apply_common_table_scales 等 |
| 表头/表体 BabelDOC 钩子 | generate_fallback_line_layouts_with_table_repair / process_page_with_table_short_paragraphs |

### 2.3 插件 JS

- addon/content/modules/runner.js：pythonHelpers 中 3 个曾部署条目（table_text/table_headers/table2）；table_layout 从未加入部署清单（0.7.0 新增后未接线即封存）。
- addon/content/modules/queue.js：表格 warning 展示（完成提示表格提示、格式化函数）保留但永不触发（兼容旧日志）。

### 2.4 后处理中的表格部分

- addon/content/python/ztf_figure_text.py：原 _restore_figure_and_table_text 已改名 _restore_figure_text，仅保留图内文字恢复（表格文字恢复逻辑随表格翻译移除）。
- addon/content/python/ztf_style_guards.py：表格单元数字/引用保护规则不再被表格流程使用（可保留）。

### 2.5 现有不可达死代码（记录，未清理）

- ztf_babeldoc_runner.py 中通用回显判定保留 table_header/table_body 词表分支与 _TRANSLATABLE_TABLE_HEADERS：生产路径已不可达，仅被 test_term_verbatim/test_degraded_output 的通用回显用例间接引用；彻底清理会牵动这两个测试，留待后续版本统一处理。

### 2.6 测试

- 封存模块的契约测试保留（直接 import 模块文件）：test_table_text.py、test_table_roles.py、test_table_headers.py、test_table2.py、test_table2_runner.py、test_table_layout.py。
- 生产行为测试更新为"表格内容保留原文 + 题注翻译"：test_table_headers_e2e.py（改为断言语义）、T01 质量夹具（tests/pdf_quality_check.py）表格检查项。
- helper-manifest 测试更新部署清单（移除 4 个封存 helper）。
- 新增 runner 级测试：表格单元格不送译、题注送译、表外正文翻译、无表格请求/无表格 warning。

## 3. 已知问题（后续版本恢复表格翻译时必须解决）

1. **表体规则语义**：0.6.0"短值保留"导致部分翻译；0.7.0 改为"只有纯标识符/缩写/数字/URL/代码保留，其余送译"后，模型回声/人名/产品名处理仍需验证。
2. **Q11 中文有效性回退**：放宽为 >=1 CJK 且仅短值后，整句与混合译文边界仍需真实重跑验证。
3. **表格字号 patch**：
   - 严格原边框 + 0.9 下限方案在 P01 mock 通过（min 8.97pt、边框不变），但存在同格多段落字号不一致导致文字叠印（8.97 与 9.47 两段重叠约 1017pt²）；
   - 无线表（booktabs/无竖线）几何判定退化，跨列表注可能误套 patch；
   - 极端窄格 overflow（P01 2 格）与真实模型译文长度相关；
   - 早期强制排版方案有丢字风险（composition 清空路径），简化方案规避但需真实文档验证。
4. **旋转表头补译（ztf_table2）**：竖排重绘、表头翻译与整表 scale 交互未验证。
5. **表格补译请求（表2）**：请求数与翻译质量权衡未定。
6. **测试**：多段落单元格/无线表/正文回归（>=0.95 源字号）/错误脱敏的测试需补。
7. **脱敏**：历史实现曾把异常文本放入用户可见 warning，恢复时须经 log_safe_error。
8. **版本约束**：所有表格 patch 仅针对 BabelDOC 0.6.4；升级需重新适配。
9. **题注碎片合并**：删除表格 fallback_line 包装后，原先的题注碎片合并逻辑不再被调用，长题注被 BabelDOC 切成碎片（如 "Tabl" + 中文 + "."）。0.7.0 封存时已恢复"题注 fallback-line 合并"（_merge_caption_fragment_layouts，仅题注，不含表注）；后续恢复表格翻译时需保留该合并逻辑并补多行题注回归。
10. **表注合并回退**：曾实现表注（星号/来源说明）整条合并翻译；但合并后单成分 composition 在表注未翻译/保留原文路径会导致 runner 失败或只绘首字符（真实 BabelDOC 复现）。方案 A 已回退为"表注保留原文"，合并/翻译链路删除；后续恢复时需保证 kept/retained 路径使用可渲染的完整 composition。

## 4. 恢复步骤（后续版本）

1. 阅读 ADR 0005（输出保真）、ADR 0006（表体翻译策略与进度修复；表格部分已被取代）、ADR 0007（暂缓表格翻译）与本文档。
2. 将 archive/ 下 4 个 helper 移回 addon/content/python/，并加回 runner.js pythonHelpers 清单与 helper-manifest 期望。
3. 在 runner 中恢复表格分析、表体判定、旋转表头、字号 patch 接线（参考 git 历史）。
4. 修复第 3 节问题（同格多段落协调、无线表判定、正文断言、脱敏、题注碎片合并等）。
5. 先 mock 后真实重跑验证：字号 >=0.9 倍源、同格无重叠、边框不变、正文不变、请求数可接受。
6. 更新 spec/ADR 与 T01 质量夹具，再走双轴审查与发布门禁。
