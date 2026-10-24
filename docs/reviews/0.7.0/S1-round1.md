# S1（#40）runner 接线：第 1 轮审查

日期：2026-10-24。审查对象：Lead 编写的接入层（`ztf_babeldoc_runner.py` 版面导出与合成调用、`runner.js` 参数与 helper 清单、相关测试）。两轴审查者为新开的 Codex gpt-6.1-sol 只读会话（此前两次因 Codex 服务中断与额度耗尽未完成）。

## Standards 轴：VERDICT: CHANGES

1. `ztf_babeldoc_runner.py:324`、`:357`（§4）：页面尺寸与旋转值缺少有限性检查，可能导出 NaN/Infinity，旋转值非有限时在排版回调内抛错。
2. `:326`、`:331–332`、`:347–348`（§4、§7、§12）：几何读取失败置零、缺少快照、无效版面框被跳过时没有结构化日志，属于静默部分完成。
3. `:6133`、`:6188–6189`（§4、§7、§12）：非法开关值直接返回 False、清理失败直接 `pass`，均无日志；已有失败日志缺少计数。
4. `:308`、`:6126`、`:6147`、`:6210`、`:6178`（§3）：新增 docstring 与注释为英文。
- 可选建议：`test_missing_layout_is_logged` 应断言日志。

## Spec 轴：VERDICT: CHANGES

1. **[P1] 非默认 CropBox 导致正文静默丢失**（`:323`、`:334–346`；规格 §1 CropBox 相对坐标）。BabelDOC 解析前把 MediaBox 归零并清除 CropBox（`babeldoc/format/pdf/high_level.py` `fix_media_box`），写出时再恢复，IL 坐标并非最终 PDF 可见页坐标。MediaBox 600×800、CropBox [50,100,550,700] 时合成输出文本为空且不报错。
2. **[P2] 旋转页不触发逐页回退、反而使整份合成失败**（`:357`）。IL `Page` 没有 `rotation` 字段，导出恒为 0；旋转 90° 的页面被规划为单栏，合成校验抛 `rotation_requires_fallback`，整份降级为单语。
3. **[P2] 双语合成失败提示是旧文案**（`runner.js:476`；规格 §6）。应为"未能生成分栏双语 PDF，已导入单语中文译本"。
- 已确认无问题：正常与降级路径都在单语链接恢复之后合成；两种模式都传 `--no-dual`。

## Lead 修改（原实现者即 Lead）

- 新增 `_normalize_split_layout_geometry`：合成前用原 PDF 每页的 `transformation_matrix` 把 IL 用户空间坐标换算为最终页面坐标（CropBox 相对、左上原点），页面尺寸取原页 `rect`、旋转取原页 `rotation`；非有限框丢弃并计数。版面导出新增 `il_top`（IL MediaBox 上边界）。
- `_finite_or_none` 统一做有限性检查；导出跳过的元素按原因计入 `split_layout_skips` 并写 `split_layout_export_skips` 日志；几何换算写 `split_layout_geometry` 日志。
- `_split_dual_setting` 区分非法取值并记录 `invalid_setting`；清理失败记录 `split_dual_cleanup_error`；失败日志带页数计数；JSON 写出 `allow_nan=False`。
- docstring 与注释改为中文；`runner.js` 降级文案改为规格 §6 原文。
- `tests/test_split_wiring.py` 扩展到 12 项（CropBox 与旋转换算、非有限框、跳过计数、非法开关、清理失败、各日志断言）；`translation-runner.test.js` 断言新文案。

修改后新开两个审查者复审。
