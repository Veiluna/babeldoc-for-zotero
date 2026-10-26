"""分栏规划的合成版面回归测试；不读取 PDF，也不调用翻译接口。"""

import copy
from dataclasses import FrozenInstanceError, fields
import json
import math
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon/content/python"))
from ztf_split_plan import PagePlan, Region, plan_page


def paragraph(box, label="text", translated=True, marginal=None, author=None):
    """生成规格 §1 的段落。"""
    result = {"box": list(box), "label": label, "translated": translated}
    if marginal is not None:
        result["marginal"] = marginal
    if author is not None:
        result["author"] = author
    return result


def page(paragraphs, layouts=(), **extra):
    """生成左上原点、单位 pt 的合成页。"""
    return {"index": 7, "width": 600, "height": 800,
            "paragraphs": paragraphs, "layouts": list(layouts), **extra}


def two_column_page(*extra, layouts=()):
    """两栏正文位于 y=150..650，栏间隙 x=280..320。"""
    return page([paragraph((40, 150, 280, 350)),
                 paragraph((40, 450, 280, 650)),
                 paragraph((320, 150, 560, 350)),
                 paragraph((320, 450, 560, 650)), *extra], layouts)


# 离线夹具：2026-10-23 用 PyMuPDF 1.28.2 的 Page.get_text("blocks") 从
# G:/Program Files/Zotero/datas/storage/JYCINM3Z/P05.pdf 提取第 1、2、4 页，坐标保留 3 位。
# 按文本特征人工标注，并非 BabelDOC 分类结果或真实翻译接口输出。
# 每项为 (box, label, translated, marginal, author)，顺序保持 PDF 块号。
# author 与 marginal 均为人工标注，不冒充 runner 的真实导出结果。
P05_FIRST_PAGE_BLOCKS = (
    ((56.368, 82.769, 555.632, 99.985), "title", True, False, False),
    # 三位作者姓名/邮箱/机构及各自的 Vienna, Austria 行（块 1..6）。
    ((75.988, 110.469, 208.620, 145.838), "author_info_hybrid", False, False, True),
    ((110.409, 147.831, 173.960, 157.793), "author_info_hybrid", False, False, True),
    ((240.137, 110.469, 372.769, 145.838), "author_info_hybrid", False, False, True),
    ((274.558, 147.831, 338.109, 157.793), "author_info_hybrid", False, False, True),
    ((404.287, 110.469, 536.919, 145.838), "author_info_hybrid", False, False, True),
    ((438.707, 147.831, 502.258, 157.793), "author_info_hybrid", False, False, True),
    ((53.798, 166.030, 111.943, 176.939), "section_header", True, False, False),  # ABSTRACT
    ((53.467, 181.173, 295.563, 387.462), "text", True, False, False),
    ((53.798, 398.702, 115.664, 409.611), "section_header", True, False, False),  # KEYWORDS
    ((53.798, 413.981, 294.045, 422.857), "keywords", True, False, False),
    ((53.503, 429.464, 295.390, 487.287), "text", True, False, False),  # ACM Reference Format
    ((53.798, 499.769, 156.529, 510.678), "section_header", True, False, False),  # INTRODUCTION
    ((53.484, 515.047, 295.562, 622.571), "text", True, False, False),
    ((53.589, 652.699, 266.625, 667.643), "text", False, True, False),  # 许可戳
    ((53.317, 677.421, 193.853, 709.087), "text", False, True, False),  # 版权/ISBN/DOI 戳
    ((317.686, 167.505, 559.713, 253.221), "text", True, False, False),
    ((317.731, 255.146, 559.582, 395.664), "text", True, False, False),
    ((317.623, 397.747, 559.713, 658.700), "text", True, False, False),
    ((317.955, 660.625, 559.716, 702.513), "text", True, False, False),
    ((300.000, 754.180, 309.000, 763.180), "text", False, True, False),  # 页码 41
)
P05_SECOND_PAGE_BLOCKS = (
    ((53.798, 62.164, 558.199, 69.138), "text", False, True, False),  # 会议名/作者页眉
    ((53.798, 86.852, 295.564, 150.680), "text", True, False, False),
    ((53.368, 152.605, 295.564, 446.571), "text", True, False, False),
    ((53.798, 450.233, 295.563, 615.596), "text", True, False, False),
    ((53.798, 626.706, 249.125, 637.615), "section_header", True, False, False),
    ((53.368, 641.849, 295.556, 683.774), "text", True, False, False),
    ((53.672, 692.183, 290.683, 709.087), "text", True, False, False),  # 栏内数据/代码脚注
    ((317.955, 86.919, 559.720, 205.475), "text", True, False, False),
    ((317.731, 207.535, 559.582, 315.064), "text", True, False, False),
    ((317.641, 317.110, 559.717, 435.612), "text", True, False, False),
    ((317.955, 437.611, 559.719, 687.681), "text", True, False, False),
    ((317.955, 700.594, 546.875, 709.087), "text", True, False, False),  # 栏内 CPTC 脚注
    ((300.000, 754.180, 309.000, 763.180), "text", False, True, False),  # 页码 42
)
P05_FOURTH_PAGE_BLOCKS = (
    ((53.798, 62.164, 558.199, 69.138), "text", False, True, False),
    ((53.798, 86.852, 294.047, 128.763), "text", True, False, False),
    ((53.798, 127.561, 295.556, 216.434), "text", True, False, False),
    ((53.467, 215.239, 295.564, 479.447), "text", True, False, False),
    ((53.798, 491.272, 173.154, 502.181), "section_header", True, False, False),  # 3.3 Scenario Timelines
    ((53.467, 506.496, 295.561, 646.956), "text", True, False, False),
    ((53.798, 648.888, 295.559, 679.810), "text", True, False, False),
    ((53.798, 692.183, 294.022, 709.086), "text", True, False, False),
    ((317.955, 86.869, 559.721, 271.228), "text", True, False, False),
    ((317.623, 273.153, 559.582, 468.508), "text", True, False, False),
    ((317.955, 486.403, 488.497, 497.312), "section_header", True, False, False),  # 3.4 Labeling…
    ((317.686, 501.571, 558.203, 521.540), "text", True, False, False),
    ((317.641, 536.238, 559.715, 654.790), "text", True, False, False),
    ((317.686, 656.715, 559.192, 709.606), "text", True, False, False),
    ((300.000, 754.180, 309.000, 763.180), "text", False, True, False),
)
# 同一源文件 get_text("dict") 的 line.bbox：三位作者各自 4 行，按作者排序。
P05_AUTHOR_LINES = (
    (107.338, 110.469, 177.587, 122.424),
    (94.678, 123.920, 190.020, 133.882),
    (75.988, 135.876, 208.620, 145.838),
    (110.409, 147.831, 173.960, 157.793),
    (271.027, 110.469, 342.280, 122.424),
    (258.424, 123.921, 354.573, 133.883),
    (240.137, 135.876, 372.769, 145.838),
    (274.558, 147.831, 338.109, 157.793),
    (415.558, 110.469, 525.964, 122.424),
    (405.826, 123.921, 535.469, 133.883),
    (404.287, 135.876, 536.919, 145.838),
    (438.707, 147.831, 502.258, 157.793),
)

# 第 4 轮复审的最小离线反例；第 13 页仅复现左右栏几何关系，
# table_body 标签为合法输入变体，不声称来自真实 BabelDOC 导出。
P05_PAGE13_TABLE = {"box": [53.467, 86.852, 295.564, 709.087], "class": "table"}
P05_PAGE13_BODY = (
    ((317.641, 86.852, 559.720, 350), "text", True, False, False),
    ((317.641, 370, 559.720, 709.087), "text", True, False, False),
)
P05_FIRST_PAGE_CORNER = {"box": [0, 72, 26, 98], "class": "figure"}

# 第 5 轮复审的离线几何反例：表格采用复审给出的横线外框，
# 其余框来自同一 P05 第 13 页 get_text("blocks")，坐标保留 3 位。
# 标签变体为人工构造，不冒充真实 layout.json。表框右缘比表文宽 4.982pt。
P05_ROUND5_PAGE13_TABLE = {"box": [69.289, 107.021, 278.554, 704.557], "class": "table"}
P05_ROUND5_PAGE13_TABLE_TEXT = (
    (74.270, 107.021, 273.572, 188.541),
    (74.271, 189.536, 270.933, 231.978),
    (244.033, 243.932, 270.932, 248.914),
    (74.271, 232.973, 268.183, 351.131),
    (74.271, 352.126, 268.258, 413.298),
    (74.271, 414.293, 270.445, 703.808),
)
P05_ROUND5_PAGE13_BODY = (
    ((53.798, 62.164, 558.199, 69.138), "text", False, True, False),
    ((70.898, 84.582, 276.650, 93.549), "table_caption", False, False, False),
    ((317.641, 86.919, 559.717, 413.694), "text", True, False, False),
    ((317.955, 424.386, 502.701, 435.295), "section_header", True, False, False),
    ((316.440, 439.623, 559.713, 514.358), "text", True, False, False),
    ((317.955, 516.377, 558.373, 547.193), "text", True, False, False),
    ((300.000, 754.180, 309.000, 763.180), "text", False, True, False),
)


def p05_page(blocks, index):
    """仅用固化常量生成输入；测试不导入 PyMuPDF、不访问源 PDF。"""
    paragraphs = [paragraph(box, label, translated, marginal, author)
                  for box, label, translated, marginal, author in blocks]
    return page(paragraphs, index=index, width=612, height=792)


# 20261025T013526-19600/{P01,P05}/out/*.layout.json 前两页的原始几何与标签；
# 仅保留每栏一个正文锚点、页码段落和外围 abandon/fallback_line 框。
# 不读取 build 产物；这是实际 mock 导出的版面，未调用真实翻译接口。
S7_PAGE_NUMBER_SHAPES = (
    ("P01", 0, 595.276, 841.89,
     (73.772, 475.664, 286.384, 623.755), (309.03, 452.203, 521.402, 520.087),
     (74.798, 746.984, 88.088, 753.858), (73.0, 744.89, 90.0, 756.89)),
    ("P01", 1, 595.276, 841.89,
     (73.732, 94.639, 286.236, 231.475), (309.129, 94.639, 521.653, 231.47),
     (507.749, 746.984, 521.179, 753.938), (505.0, 745.89, 522.0, 755.89)),
    ("P05", 0, 612.0, 792.0,
     (53.53, 179.004, 295.429, 387.365), (318.027, 165.306, 559.294, 252.047),
     (300.108, 756.916, 308.046, 763.0), (298.0, 756.0, 310.0, 765.0)),
    ("P05", 1, 612.0, 792.0,
     (53.995, 84.683, 298.189, 149.488), (318.027, 84.683, 561.957, 204.301),
     (300.108, 756.916, 308.775, 763.0), (298.0, 755.0, 310.0, 765.0)),
)


class SplitPlanTests(unittest.TestCase):
    def test_mock_page_number_shapes_keep_header_band_and_clean_gutter(self):
        for document, index, width, height, left, right, number, wrapper in S7_PAGE_NUMBER_SHAPES:
            for has_paragraph in (True, False):
                with self.subTest(document=document, index=index, has_paragraph=has_paragraph):
                    paragraphs = [paragraph(left), paragraph(right)]
                    layouts = [{"box": list(wrapper), "class": "abandon"}]
                    if has_paragraph:
                        paragraphs.append(paragraph(number, "abandon", True, True))
                        layouts.append({"box": list(number), "class": "fallback_line"})
                    # 缺段落分支仅保留外围 abandon 框，模拟无可导出文字的页码。
                    source = page(paragraphs, layouts, index=index, width=width, height=height)
                    result = self.assert_planned_page(source, PagePlan(index, 2, (
                        Region("header_footer", (0, wrapper[1], width, wrapper[3])),
                        Region("column", left, column=0),
                        Region("column", right, column=1))))
                    for region in result.regions:
                        if region.kind == "column":
                            self.assertTrue(region.box[2] <= left[2]
                                            or region.box[0] >= right[0])

    def test_small_ordinary_layout_above_body_is_not_a_header(self):
        # S7 审查反例：小型段落标题即便没有段落对象，也应随左栏而非复制到 A/B 页。
        for label in ("paragraph_title", "title", "text", "plain text", "fallback_line"):
            with self.subTest(label=label):
                source = page([
                    paragraph((51, 164, 296, 711)),
                    paragraph((316, 165, 560, 704)),
                ], [{"box": [60, 140, 110, 150], "class": label}], width=612, height=792)
                self.assert_planned_page(source, PagePlan(7, 2, (
                    Region("column", (51, 140, 296, 711), column=0),
                    Region("column", (316, 165, 560, 704), column=1))))

    def test_page_number_layout_wrappers_join_header_footer_without_column_fragments(self):
        # P05/P01 mock-run2 的页码及外围框形状；正文缩减为两框。
        for number, wrapper in (
                ((300, 757, 309, 763), (298, 755, 310, 765)),
                ((74.798, 746.984, 88.088, 753.858), (73, 744.89, 90, 756.89))):
            for label in ("abandon", "fallback_line"):
                with self.subTest(number=number, label=label):
                    source = page([
                        paragraph((51, 164, 296, 711)),
                        paragraph((316, 165, 560, 704)),
                        paragraph(number, "abandon", False, True),
                    ], [{"box": list(wrapper), "class": label}], width=612, height=792)
                    self.assert_planned_page(source, PagePlan(7, 2, (
                        Region("header_footer", (0, wrapper[1], 612, wrapper[3])),
                        Region("column", (51, 164, 296, 711), column=0),
                        Region("column", (316, 165, 560, 704), column=1))))

    def plan(self, source):
        """所有成功规划的新旧夹具都必须通过完整框不变量。"""
        result = plan_page(source)
        if result.columns:
            self.assert_content_covered(source, result)
        return result

    def test_small_edge_layouts_without_paragraphs_do_not_supply_column_evidence(self):
        for box in ((298, 755, 310, 765), (300, 25, 309, 35)):
            with self.subTest(box=box):
                source = page([
                    paragraph((51, 164, 296, 711)),
                    paragraph((316, 165, 560, 704)),
                ], [{"box": list(box), "class": "abandon"}], width=612, height=792)
                self.assert_planned_page(source, PagePlan(7, 2, (
                    Region("header_footer", (0, box[1], 612, box[3])),
                    Region("column", (51, 164, 296, 711), column=0),
                    Region("column", (316, 165, 560, 704), column=1))))

    def test_small_body_paragraph_at_page_edge_is_not_inferred_as_footer(self):
        for translated in (False, True):
            with self.subTest(translated=translated):
                source = two_column_page(
                    paragraph((60, 755, 70, 765), translated=translated),
                    layouts=[{"box": [58, 753, 72, 767], "class": "abandon"}])
                result = self.plan(source)
                self.assertFalse(any(r.kind == "header_footer" for r in result.regions))
                self.assert_unique_ownership(source, result,
                                             [("column", 0)] * 2 + [("column", 1)] * 2
                                             + [("column", 0)] * 2)

    def assert_planned_page(self, source, expected):
        """用显式期望计划校验区域，并逐元素核对类别、唯一归属和完整覆盖。"""
        result = self.plan(source)
        self.assertEqual(result, expected)
        ownership = []
        for item in source["paragraphs"] + source["layouts"]:
            x0, y0, x1, y1 = item["box"]
            x, y = (x0 + x1) / 2, (y0 + y1) / 2
            owners = [(r.kind, r.column) for r in expected.regions
                      if r.box[0] <= x <= r.box[2] and r.box[1] <= y <= r.box[3]]
            self.assertEqual(len(owners), 1, item["box"])
            ownership.extend(owners)
        self.assert_unique_ownership(source, result, ownership)
        self.assert_content_covered(source, result)
        return result

    def assert_unique_ownership(self, source, result, expected):
        """逐元素验证中心点只属于一个区域，并核对显式给定的类别与栏号。"""
        items = source["paragraphs"] + source["layouts"]
        self.assertEqual(len(items), len(expected))
        for item, (kind, column) in zip(items, expected):
            x0, y0, x1, y1 = item["box"]
            x, y = (x0 + x1) / 2, (y0 + y1) / 2
            owners = [r for r in result.regions
                      if r.box[0] <= x <= r.box[2] and r.box[1] <= y <= r.box[3]]
            with self.subTest(box=item["box"], expected=(kind, column)):
                self.assertEqual([(r.kind, r.column) for r in owners], [(kind, column)])

    def assert_content_covered(self, source, result):
        """每个完整输入框只落入一个区域，且不与其他区域有正面积交集。"""
        for item in source["paragraphs"] + source["layouts"]:
            x0, y0, x1, y1 = item["box"]
            with self.subTest(box=item["box"]):
                owners = [r for r in result.regions if r.box[0] <= x0
                          and r.box[1] <= y0 and x1 <= r.box[2] and y1 <= r.box[3]]
                self.assertEqual(len(owners), 1)
                intersecting = [r for r in result.regions
                                if max(x0, r.box[0]) < min(x1, r.box[2])
                                and max(y0, r.box[1]) < min(y1, r.box[3])]
                self.assertEqual(intersecting, owners)

    def test_round4_page13_table_column_with_and_without_table_paragraphs(self):
        for label in ("table_body", None):
            with self.subTest(label=label):
                source = p05_page(P05_PAGE13_BODY, 12)
                if label is not None:
                    source["paragraphs"].append(
                        paragraph((60, 100, 290, 690), label, False))
                source["layouts"] = [copy.deepcopy(P05_PAGE13_TABLE)]
                result = self.plan(source)
                self.assertEqual(result.columns, 2)
                self.assert_unique_ownership(source, result,
                                             [("column", 1)] * 2
                                             + ([("column", 0)] if label else [])
                                             + [("column", 0)])
                self.assert_content_covered(source, result)

    def test_round5_page13_table_labels_and_missing_text_have_identical_plans(self):
        expected = PagePlan(12, 2, (
            Region("header_footer", (0, 62.164, 612, 69.138)),
            Region("header_footer", (0, 754.180, 612, 763.180)),
            Region("column", (69.289, 84.582, 278.554, 704.557), column=0),
            Region("column", (316.440, 86.919, 559.717, 547.193), column=1)))
        for label in ("table_body", "text", None):
            with self.subTest(label=label):
                source = p05_page(P05_ROUND5_PAGE13_BODY, 12)
                source["layouts"] = [copy.deepcopy(P05_ROUND5_PAGE13_TABLE)]
                if label is not None:
                    source["paragraphs"].extend(
                        paragraph(box, label, False) for box in P05_ROUND5_PAGE13_TABLE_TEXT)
                self.assert_planned_page(source, expected)

    def test_round5_layout_internal_paragraphs_never_seed_spanning(self):
        # 中心位于框内即随框处理，即便段落越出框边、命中间隙或 author=true。
        # 第 6 轮裁决限定为已核实的非文本容器，未知类别不能冒充容器。
        for layout_class in ("table", "figure", "image", "formula", "isolate_formula",
                             "formula_hybrid"):
            for label in ("text", "table_body", "author_info_hybrid"):
                with self.subTest(layout_class=layout_class, label=label):
                    source = two_column_page(
                        paragraph((200, 200, 310, 230), label, False, False, True),
                        layouts=[{"box": [190, 190, 280, 240], "class": layout_class}])
                    self.assert_planned_page(source, PagePlan(7, 2, (
                        Region("column", (40, 150, 310, 650), column=0),
                        Region("column", (320, 150, 560, 650), column=1))))

    def test_round6_first_page_body_layouts_keep_two_columns_and_corner(self):
        # 审查的六种输入：正文框精确重合或外扩 1pt，仍保留页边角标。
        for label in ("text", "plain text", "paragraph_hybrid"):
            for padding in (0, 1):
                with self.subTest(label=label, padding=padding):
                    source = p05_page(P05_FIRST_PAGE_BLOCKS, 0)
                    source["layouts"] = [copy.deepcopy(P05_FIRST_PAGE_CORNER)]
                    for item in source["paragraphs"]:
                        if (item["label"] == "text" and not item["marginal"]
                                and not item["author"]):
                            x0, y0, x1, y1 = item["box"]
                            source["layouts"].append({"class": label, "box": [
                                x0 - padding, y0 - padding, x1 + padding, y1 + padding]})
                    with self.assertNoLogs("ztf_split_plan", level="WARNING"):
                        result = self.plan(source)
                    self.assertEqual(result.columns, 2)
                    owners = ([("spanning", -1)] * 7 + [("column", 0)] * 9
                              + [("column", 1)] * 4 + [("header_footer", -1)]
                              + [("spanning", -1)] + [("column", 0)] * 3
                              + [("column", 1)] * 4)
                    self.assert_unique_ownership(source, result, owners)
                    self.assertIn(Region("spanning", (0, 72, 612, 99.985), "top"),
                                  result.regions)

    def test_round6_author_layouts_preserve_explicit_author_seeds(self):
        # 三块都不碰间隙，必须依靠段落自己的 author=true 整行跨栏。
        boxes = ((60, 88, 180, 108), (190, 90, 278, 104), (420, 89, 540, 109))
        expected = PagePlan(7, 2, (
            Region("spanning", (0, 88, 600, 109), "top"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1)))
        for label in ("author_info_hybrid", "plain text", "author"):
            with self.subTest(label=label):
                source = two_column_page(
                    *(paragraph(box, "text", False, False, True) for box in boxes),
                    layouts=[{"box": list(box), "class": label} for box in boxes])
                self.assert_planned_page(source, expected)

    def test_round6_text_layouts_preserve_paragraph_labels_and_marginals(self):
        # 包住非正文标题或页眉的 text 框不得替换段落标签，也不能阻挡页眉带。
        source = two_column_page(
            paragraph((40, 90, 180, 105), "section_header"),
            paragraph((420, 91, 540, 106), "section_header"),
            paragraph((40, 30, 560, 40), "text", False, True),
            layouts=[{"box": [40, 90, 180, 105], "class": "text"},
                     {"box": [420, 91, 540, 106], "class": "plain text"},
                     {"box": [40, 30, 560, 40], "class": "paragraph_hybrid"}])
        self.assert_planned_page(source, PagePlan(7, 2, (
            Region("header_footer", (0, 30, 600, 40)),
            Region("column", (40, 90, 280, 650), column=0),
            Region("column", (320, 91, 560, 650), column=1))))

    def test_round6_empty_text_layout_is_an_ordinary_element(self):
        # 没有段落的文本框仍能补出缺少正文的一栏或作为跨栏几何证据。
        for label in ("text", "plain text", "paragraph_hybrid", "title",
                      "author_info_hybrid", "author", "fallback_line"):
            with self.subTest(label=label):
                source = page([paragraph((320, 150, 560, 650))], layouts=[
                    {"box": [40, 150, 280, 650], "class": label}])
                self.assert_planned_page(source, PagePlan(7, 2, (
                    Region("column", (40, 150, 280, 650), column=0),
                    Region("column", (320, 150, 560, 650), column=1))))
                source = two_column_page(layouts=[
                    {"box": [40, 80, 560, 100], "class": label}])
                self.assert_planned_page(source, PagePlan(7, 2, (
                    Region("spanning", (0, 80, 600, 100), "top"),
                    Region("column", (40, 150, 280, 650), column=0),
                    Region("column", (320, 150, 560, 650), column=1))))

    def test_round6_unknown_layout_does_not_suppress_author_paragraph(self):
        source = two_column_page(
            paragraph((60, 88, 180, 108), "text", False, False, True),
            layouts=[{"box": [60, 88, 180, 108], "class": "unknown"}])
        self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 88, 600, 108), "top"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1))))

    def test_round4_first_page_corner_is_wholly_in_title_band(self):
        source = p05_page(P05_FIRST_PAGE_BLOCKS, 0)
        source["layouts"] = [copy.deepcopy(P05_FIRST_PAGE_CORNER)]
        result = self.plan(source)
        self.assertEqual(result.columns, 2)
        self.assertIn(Region("spanning", (0, 72, 612, 99.985), "top"), result.regions)
        self.assert_unique_ownership(source, result,
                                     [("spanning", -1)] * 7 + [("column", 0)] * 9
                                     + [("column", 1)] * 4 + [("header_footer", -1)]
                                     + [("spanning", -1)])
        self.assert_content_covered(source, result)

    def test_round4_header_band_must_not_cut_another_marginal_element(self):
        source = two_column_page(
            paragraph((40, 70, 260, 90), "header", False, True),
            paragraph((40, 85, 260, 160), "copyright", False, True))
        # 后一框与正文重叠，必须随栏；前一框也不能扩成全宽带切开它。
        result = self.plan(source)
        self.assertEqual(result, PagePlan(7, 2, (
            Region("column", (40, 70, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1))))

    def test_round4_non_body_elements_supply_either_column_for_any_label(self):
        for collection in ("paragraphs", "layouts"):
            for label in ("table_body", "table", "figure", "formula", "unknown"):
                for side in (0, 1):
                    with self.subTest(collection=collection, label=label, side=side):
                        boxes = [(40, 150, 280, 650), (320, 150, 560, 650)]
                        source = page([paragraph(boxes[1 - side])])
                        source[collection].append(
                            paragraph(boxes[side], label, False) if collection == "paragraphs"
                            else {"box": list(boxes[side]), "class": label})
                        result = self.plan(source)
                        self.assertEqual(result.columns, 2)
                        self.assert_unique_ownership(source, result,
                                                     [("column", 1 - side), ("column", side)])

    def test_round4_spanning_band_shrinks_at_half_or_less_and_expands_above_half(self):
        # 初始 author 带 y=90..110；table/figure 容器由半高规则决定。
        # 收缩会切掉种子的输入必须整页回退，不能继续裁切。
        cases = (
            ((0, 70, 26, 100), (100, 110), None),  # 1/3，收上缘会切种子
            ((0, 70, 26, 110), (90, 110), None),  # 正好一半，无法保留种子
            ((0, 100, 26, 130), (90, 100), None),  # 1/3，收下缘会切种子
            ((0, 80, 26, 105), (80, 110), ("spanning", -1)),  # 3/5，扩上缘
            ((0, 95, 26, 120), (90, 120), ("spanning", -1)),  # 3/5，扩下缘
        )
        for box, band, owner in cases:
            for collection in ("paragraphs", "layouts"):
                with self.subTest(box=box, collection=collection):
                    source = two_column_page(
                        paragraph((60, 90, 180, 110), "author_info_hybrid", False, False, True))
                    source[collection].append(
                        paragraph(box, "table") if collection == "paragraphs" else
                        {"box": list(box), "class": "figure"})
                    if owner is None:
                        with self.assertLogs("ztf_split_plan", level="WARNING"):
                            result = self.plan(source)
                        self.assertEqual(result.columns, 0)
                        self.assert_content_covered(source, result)
                    else:
                        result = self.plan(source)
                        self.assertEqual([r for r in result.regions if r.kind == "spanning"],
                                         [Region("spanning", (0, band[0], 600, band[1]), "top")])
                        owners = [r for r in result.regions
                                  if r.box[0] <= box[0] and r.box[1] <= box[1]
                                  and box[2] <= r.box[2] and box[3] <= r.box[3]]
                        self.assertEqual([(r.kind, r.column) for r in owners], [owner])

    def test_preserved_author_row_is_spanning_for_explicit_non_marginals(self):
        for label in ("author_info_hybrid", "title"):
            source = two_column_page(*[
                paragraph(box, label, False, False) for box in (
                    (60, 88, 180, 108), (282, 95, 298, 101), (420, 89, 540, 109))])
            result = self.plan(source)
            self.assertEqual(result.regions[0],
                             Region("spanning", (0, 88, 600, 109), position="top"))
            self.assertFalse(any(r.kind == "header_footer" for r in result.regions))
            self.assert_unique_ownership(source, result,
                                         [("column", 0)] * 2 + [("column", 1)] * 2
                                         + [("spanning", -1)] * 3)
            self.assert_content_covered(source, result)

    def test_author_row_spans_without_any_fragment_touching_gutter(self):
        # 三块均在 x=280..320 间隙之外；中块也位于左栏范围。
        for label in ("author_info_hybrid", "title", "text"):
            boxes = [(60, 88, 150, 108), (190, 90, 270, 110), (420, 89, 540, 109)]
            source = two_column_page(*[paragraph(b, label, False, False, True) for b in boxes])
            with self.subTest(label=label):
                result = self.plan(source)
                self.assertEqual(result.regions[0],
                                 Region("spanning", (0, 88, 600, 110), position="top"))
                self.assertEqual(sum(r.kind == "spanning" for r in result.regions), 1)
                self.assert_unique_ownership(source, result,
                                             [("column", 0)] * 2 + [("column", 1)] * 2
                                             + [("spanning", -1)] * 3)
                self.assert_content_covered(source, result)

    def test_author_missing_or_false_never_seeds_a_row_by_label_or_geometry(self):
        for author in (None, False):
            for collection in ("paragraphs", "layouts"):
                with self.subTest(author=author, collection=collection):
                    boxes = [(60, 88, 150, 108), (190, 90, 270, 110), (420, 89, 540, 109)]
                    source = two_column_page()
                    source[collection] += (
                        [paragraph(b, "author_info_hybrid", False, False, author) for b in boxes]
                        if collection == "paragraphs" else
                        [{"box": list(b), "class": "author"} for b in boxes])
                    result = self.plan(source)
                    self.assertFalse(any(r.kind == "spanning" for r in result.regions))
                    self.assert_unique_ownership(source, result,
                                                 [("column", 0)] * 2 + [("column", 1)] * 2
                                                 + [("column", 0)] * 2 + [("column", 1)])
                    self.assert_content_covered(source, result)

    def test_one_author_seed_extends_unmarked_paragraph_and_layout_row_members(self):
        source = two_column_page(
            paragraph((60, 88, 150, 108), "author_info_hybrid", False, False, True),
            paragraph((190, 90, 270, 110), "author_info_hybrid", False, False),
            layouts=[{"box": [420, 89, 540, 109], "class": "author"}])
        self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 88, 600, 110), "top"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1))))

    def test_marginals_of_any_label_do_not_affect_columns(self):
        for label in ("text", "plain text", "paragraph_hybrid", "title", "header", "unknown"):
            for translated in (True, False):
                with self.subTest(label=label, translated=translated):
                    source = two_column_page(
                        paragraph((40, 20, 560, 35), label, translated, True),
                        paragraph((290, 760, 310, 775), label, translated, True))
                    result = self.plan(source)
                    self.assertEqual(result, PagePlan(7, 2, (
                        Region("header_footer", (0, 20, 600, 35)),
                        Region("header_footer", (0, 760, 600, 775)),
                        Region("column", (40, 150, 280, 650), column=0),
                        Region("column", (320, 150, 560, 650), column=1))))
                    self.assert_unique_ownership(source, result,
                                                 [("column", 0)] * 2 + [("column", 1)] * 2
                                                 + [("header_footer", -1)] * 2)
                    self.assert_content_covered(source, result)

    def test_column_boxes_include_headings_and_in_column_layouts(self):
        sources = [
            page([paragraph((40, 150, 560, 600)),
                  paragraph((40, 100, 560, 120), "title", True, False)],
                 [{"box": [35, 610, 565, 700], "class": "figure"}]),
            two_column_page(paragraph((40, 100, 130, 120), "section_header", True, False),
                            paragraph((320, 125, 440, 145), "section_header", True, False),
                            layouts=[{"box": [35, 660, 275, 710], "class": "figure"},
                                     {"box": [325, 670, 565, 720], "class": "table"}]),
        ]
        for source in sources:
            with self.subTest(source=source):
                result = self.plan(source)
                self.assert_content_covered(source, result)
                columns = [r for r in result.regions if r.kind == "column"]
                self.assertEqual(columns[0].box[0], 35)
                self.assertEqual(columns[-1].box[2], 565)
                self.assertFalse(any(r.kind == "spanning" for r in result.regions))
                self.assert_unique_ownership(
                    source, result, [("column", 0)] * 3 if result.columns == 1 else
                    [("column", 0)] * 2 + [("column", 1)] * 2
                    + [("column", 0), ("column", 1)] * 2)

    def test_explicit_non_marginal_is_not_legacy_header(self):
        source = page([paragraph((40, 150, 560, 650)),
                       paragraph((40, 100, 560, 120), "title", False, False)])
        result = self.plan(source)
        self.assertEqual(result, PagePlan(7, 1, (
            Region("column", (40, 100, 560, 650), column=0),)))
        self.assert_content_covered(source, result)
        self.assert_unique_ownership(source, result, [("column", 0)] * 2)

    def test_missing_marginal_does_not_make_preserved_content_a_header(self):
        source = two_column_page(paragraph((40, 80, 560, 120), "title", False))
        result = self.plan(source)
        self.assert_unique_ownership(source, result,
                                     [("column", 0), ("column", 0),
                                      ("column", 1), ("column", 1), ("spanning", -1)])
        self.assert_content_covered(source, result)

    def test_marginal_band_is_blocked_by_any_non_marginal_element(self):
        for collection, labels in (("paragraphs", ("text", "unknown")),
                                   ("layouts", ("figure", "table", "unknown", "header"))):
            for label in labels:
                with self.subTest(collection=collection, label=label):
                    source = two_column_page(paragraph((50, 680, 260, 700), "text", False, True))
                    # 仅在另一栏有 1pt 的纵向重叠，同样不能扩成全宽页脚。
                    box = (320, 699, 560, 720)
                    source[collection].append(
                        paragraph(box, label, False) if collection == "paragraphs" else
                        {"box": list(box), "class": label})
                    self.assert_planned_page(source, PagePlan(7, 2, (
                        Region("column", (40, 150, 280, 700), column=0),
                        Region("column", (320, 150, 560, 720), column=1))))

    def test_adjacent_marginal_bands_do_not_merge_over_non_marginal_content(self):
        source = two_column_page(
            paragraph((40, 80, 260, 90), "header", False, True),
            paragraph((40, 91, 260, 100), "header", False, True),
            paragraph((320, 90, 560, 91), "section_header"))
        self.assert_planned_page(source, PagePlan(7, 2, (
            Region("header_footer", (0, 80, 600, 90)),
            Region("header_footer", (0, 91, 600, 100)),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 90, 560, 91), column=1),
            Region("column", (320, 150, 560, 650), column=1))))

    def test_marginals_in_empty_vertical_bands_are_headers(self):
        # Lead 第 3 轮以实际纵向重叠判定；非 marginal 的全页包络不构成阻挡。
        source = two_column_page(
            paragraph((40, 70, 180, 90), "section_header", True, False),
            paragraph((50, 100, 260, 120), "text", False, True))
        result = self.plan(source)
        self.assertEqual([r for r in result.regions if r.kind == "header_footer"],
                         [Region("header_footer", (0, 100, 600, 120))])
        self.assert_unique_ownership(source, result,
                                     [("column", 0), ("column", 0),
                                      ("column", 1), ("column", 1),
                                      ("column", 0), ("header_footer", -1)])
        self.assert_content_covered(source, result)

    def test_independent_column_headings_do_not_form_spanning_rows(self):
        for label in ("section_header", "title", "section_title", "paragraph_title"):
            for y in (100, 370):
                with self.subTest(label=label, y=y):
                    source = two_column_page(
                        paragraph((40, y, 180, y + 15), label, True, False),
                        paragraph((320, y + 3, 480, y + 18), label, True, False))
                    result = self.plan(source)
                    self.assertFalse(any(r.kind == "spanning" for r in result.regions))
                    self.assert_unique_ownership(source, result,
                                                 [("column", 0), ("column", 0),
                                                  ("column", 1), ("column", 1),
                                                  ("column", 0), ("column", 1)])
                    self.assert_content_covered(source, result)

    def test_round3_marginal_overlapping_column_charts_stays_in_column(self):
        source = two_column_page(
            paragraph((50, 680, 260, 700), "text", False, True),
            layouts=[{"box": [40, 660, 280, 720], "class": "table"},
                     {"box": [320, 660, 560, 720], "class": "figure"}])
        result = self.plan(source)
        self.assertEqual(result, PagePlan(7, 2, (
            Region("column", (40, 150, 280, 720), column=0),
            Region("column", (320, 150, 560, 720), column=1))))
        self.assert_unique_ownership(source, result,
                                     [("column", 0)] * 2 + [("column", 1)] * 2
                                     + [("column", 0)] * 2 + [("column", 1)])
        self.assert_content_covered(source, result)

    def test_round3_independent_headings_outside_body_envelope_stay_in_columns(self):
        source = two_column_page(
            paragraph((35, 100, 275, 115), "section_header"),
            paragraph((325, 103, 565, 118), "section_header"))
        result = self.plan(source)
        self.assertEqual(result, PagePlan(7, 2, (
            Region("column", (35, 100, 280, 650), column=0),
            Region("column", (320, 103, 565, 650), column=1))))
        self.assert_unique_ownership(source, result,
                                     [("column", 0)] * 2 + [("column", 1)] * 2
                                     + [("column", 0), ("column", 1)])
        self.assert_content_covered(source, result)

    def test_page11_caption_grazing_gutter_by_0027pt_stays_in_right_column(self):
        # 第 11 页问题的最小几何反例，并非真实 BabelDOC layout.json。
        source = two_column_page(paragraph((319.973, 200, 550, 218), "table_caption"))
        result = self.plan(source)
        self.assertEqual(result, PagePlan(7, 2, (
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (319.973, 150, 560, 650), column=1))))
        self.assert_unique_ownership(source, result,
                                     [("column", 0)] * 2 + [("column", 1)] * 3)
        self.assert_content_covered(source, result)

    def test_gutter_overlap_must_exceed_absolute_and_relative_tolerance(self):
        for right_start, tolerance in ((320, 4), (288, 2)):
            for overlap in (0, 0.027, tolerance, tolerance + 0.001):
                for side in (0, 1):
                    for collection in ("paragraphs", "layouts"):
                        with self.subTest(right_start=right_start, overlap=overlap,
                                          side=side, collection=collection):
                            box = ((40, 370, 280 + overlap, 390) if side == 0 else
                                   (right_start - overlap, 370, 560, 390))
                            source = page([paragraph((40, 150, 280, 350)),
                                           paragraph((40, 450, 280, 650)),
                                           paragraph((right_start, 150, 560, 350)),
                                           paragraph((right_start, 450, 560, 650))])
                            source[collection].append(
                                paragraph(box, "table_caption") if collection == "paragraphs"
                                else {"box": list(box), "class": "unknown"})
                            result = self.plan(source)
                            spanning = overlap > tolerance
                            self.assertEqual([r for r in result.regions if r.kind == "spanning"],
                                             [Region("spanning", (0, 370, 600, 390), "middle")]
                                             if spanning else [])
                            self.assert_unique_ownership(source, result,
                                                         [("column", 0)] * 2 + [("column", 1)] * 2 + [
                                                          ("spanning", -1) if spanning else
                                                          ("column", side)])
                            self.assert_content_covered(source, result)

    def test_coverage_invariant_for_unknown_and_preserved_column_content(self):
        source = two_column_page(
            paragraph((25, 100, 130, 120), "unknown", False, False),
            paragraph((330, 670, 575, 710), "author_info_hybrid", False, False),
            layouts=[{"box": [35, 360, 270, 440], "class": "unknown"},
                     {"box": [325, 100, 565, 130], "class": "figure"}])
        result = self.plan(source)
        self.assertEqual(result, PagePlan(7, 2, (
            Region("column", (25, 100, 280, 650), column=0),
            Region("column", (320, 100, 575, 710), column=1))))
        self.assert_unique_ownership(source, result,
                                     [("column", 0)] * 2 + [("column", 1)] * 2
                                     + [("column", 0), ("column", 1)] * 2)
        self.assert_content_covered(source, result)

    def test_output_box_does_not_change_source_geometry_plan(self):
        source = two_column_page(paragraph((40, 80, 560, 120), "title", True, False))
        expected = self.plan(source)
        for item in source["paragraphs"]:
            item["output_box"] = [0, 0, 600, 800]
        self.assertEqual(self.plan(source), expected)
        self.assert_unique_ownership(source, expected,
                                     [("column", 0)] * 2 + [("column", 1)] * 2
                                     + [("spanning", -1)])
        self.assert_content_covered(source, expected)

    def test_p05_first_page_authors_are_unique_and_fully_covered(self):
        source = p05_page(P05_FIRST_PAGE_BLOCKS, 0)
        result = self.plan(source)
        self.assertEqual(result.columns, 2)
        authors = [r for r in result.regions if r.box[1] < 157.793 and r.box[3] > 110.469]
        self.assertEqual(authors, [Region("spanning", (0, 110.469, 612, 157.793), "top")])
        self.assertIn(Region("header_footer", (0, 754.180, 612, 763.180)), result.regions)
        columns = [r for r in result.regions if r.kind == "column"]
        self.assertEqual(columns, [
            Region("column", (53.317, 166.030, 295.563, 709.087), column=0),
            Region("column", (317.623, 167.505, 559.716, 702.513), column=1)])
        self.assertEqual([r for r in result.regions if r.kind == "header_footer"],
                         [Region("header_footer", (0, 754.180, 612, 763.180))])
        self.assert_content_covered(source, result)

        self.assert_unique_ownership(source, result,
                                     [("spanning", -1)] * 7 + [("column", 0)] * 9
                                     + [("column", 1)] * 4 + [("header_footer", -1)])

    def test_p05_twelve_author_lines_are_one_complete_top_spanning_band(self):
        blocks = (P05_FIRST_PAGE_BLOCKS[:1]
                  + tuple((box, "author_info_hybrid", False, False, True) for box in P05_AUTHOR_LINES)
                  + P05_FIRST_PAGE_BLOCKS[7:])
        source = p05_page(blocks, 0)
        result = self.plan(source)
        self.assertEqual([r for r in result.regions
                          if r.box[1] < 157.793 and r.box[3] > 110.469],
                         [Region("spanning", (0, 110.469, 612, 157.793), "top")])
        self.assert_content_covered(source, result)
        self.assert_unique_ownership(source, result,
                                     [("spanning", -1)] * 13 + [("column", 0)] * 9
                                     + [("column", 1)] * 4 + [("header_footer", -1)])

    def test_p05_fourth_page_independent_headings_stay_in_their_columns(self):
        source = p05_page(P05_FOURTH_PAGE_BLOCKS, 3)
        result = self.plan(source)
        self.assertEqual(result, PagePlan(3, 2, (
            Region("header_footer", (0, 62.164, 612, 69.138)),
            Region("header_footer", (0, 754.180, 612, 763.180)),
            Region("column", (53.467, 86.852, 295.564, 709.086), column=0),
            Region("column", (317.623, 86.869, 559.721, 709.606), column=1))))
        self.assert_content_covered(source, result)
        self.assert_unique_ownership(source, result,
                                     [("header_footer", -1)] + [("column", 0)] * 7
                                     + [("column", 1)] * 6 + [("header_footer", -1)])

    def test_p05_second_page_marginals_do_not_pollute_body_columns(self):
        source = p05_page(P05_SECOND_PAGE_BLOCKS, 1)
        result = self.plan(source)
        self.assertEqual(result, PagePlan(1, 2, (
            Region("header_footer", (0, 62.164, 612, 69.138)),
            Region("header_footer", (0, 754.180, 612, 763.180)),
            Region("column", (53.368, 86.852, 295.564, 709.087), column=0),
            Region("column", (317.641, 86.919, 559.720, 709.087), column=1))))
        self.assert_content_covered(source, result)
        self.assert_unique_ownership(source, result,
                                     [("header_footer", -1)] + [("column", 0)] * 6
                                     + [("column", 1)] * 5 + [("header_footer", -1)])

    def test_full_width_top_text_excluded_without_envelope_cap(self):
        """超出栏边距或整页宽的顶部通栏文本都不得充当栏锚点。"""
        expected = PagePlan(7, 2, (
            Region('spanning', (0, 50, 600, 120), position='top'),
            Region('column', (40, 150, 280, 650), column=0),
            Region('column', (320, 150, 560, 650), column=1)))
        for box in ([15, 50, 585, 120], [0, 50, 600, 120]):
            with self.subTest(box=box):
                self.assert_planned_page(two_column_page(paragraph(box)), expected)

    def test_middle_figure_conflicting_with_column_containers_falls_back_whole(self):
        # 第 4 轮禁止切框：容器包住跨栏种子且重叠不到容器高度一半，
        # 无法收缩带同时保留种子，必须整页回退，不能再接受裁切容器。
        source = two_column_page(layouts=[
            {"box": [40, 370, 560, 430], "class": "figure"},
            {"box": [40, 150, 280, 650], "class": "plain text"},
            {"box": [320, 150, 560, 650], "class": "figure"},
            {"box": [40, 300, 280, 500], "class": "table"},
        ])
        with self.assertLogs("ztf_split_plan", level="WARNING") as captured:
            result = self.assert_planned_page(source, PagePlan(7, 0, (
                Region("fallback", (0, 0, 600, 800)),)))
        self.assertEqual(json.loads(captured.records[0].getMessage())["reason"],
                         "region_boundary_conflict")
        self.assert_content_covered(source, result)

    def test_author_row_preserves_taller_fragments(self):
        source = two_column_page(
            paragraph((60, 88, 180, 108), "author_info_hybrid"),
            paragraph((282, 95, 298, 101), "author_info_hybrid"),
            paragraph((420, 89, 540, 109), "author_info_hybrid"),
        )
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 88, 600, 109), position="top"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1),
        )))

    def test_inset_full_width_body_does_not_become_a_third_column(self):
        source = two_column_page(paragraph((70, 50, 530, 120)))
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 50, 600, 120), position="top"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1),
        )))

    def test_single_column_has_no_spanning_bands(self):
        source = page([
            paragraph((40, 150, 560, 650)),
            paragraph((40, 20, 560, 35), "header", False, True),
            paragraph((290, 760, 310, 775), "page_footer", False, True),
        ], [{"box": [40, 370, 560, 430], "class": "figure"}])
        result = self.assert_planned_page(source, PagePlan(7, 1, (
            Region("header_footer", (0, 20, 600, 35)),
            Region("header_footer", (0, 760, 600, 775)),
            Region("column", (40, 150, 560, 650), column=0),
        )))

    def test_middle_band_conflicting_with_containing_body_falls_back_whole(self):
        source = page([
            paragraph((40, 150, 280, 650)),
            paragraph((320, 150, 560, 650)),
        ], [{"box": [40, 370, 560, 430], "class": "figure"}])
        with self.assertLogs("ztf_split_plan", level="WARNING"):
            self.assert_planned_page(source, PagePlan(7, 0, (
                Region("fallback", (0, 0, 600, 800)),)))

    def test_overlapping_indented_body_is_still_one_column(self):
        source = page([
            paragraph((40, 150, 560, 350)),
            paragraph((100, 450, 500, 650)),
        ])
        result = self.assert_planned_page(source, PagePlan(7, 1, (
            Region("column", (40, 150, 560, 650), column=0),)))

    def test_staggered_indents_in_each_column_keep_two_columns(self):
        # 每栏两段左缘相差超过容差，共 4 个左缘分组，但水平连通后只有两栏
        source = page([
            paragraph((40, 150, 280, 350)),
            paragraph((100, 450, 260, 650)),
            paragraph((320, 150, 560, 350)),
            paragraph((380, 450, 540, 650)),
        ])
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1),
        )))

    def test_staggered_indents_in_three_columns_still_fallback(self):
        # 合并只连接水平重叠的分组，三个互不重叠的栏仍应回退
        source = page([
            paragraph((30, 100, 180, 300)),
            paragraph((70, 350, 170, 600)),
            paragraph((225, 100, 375, 300)),
            paragraph((265, 350, 365, 600)),
            paragraph((420, 100, 570, 300)),
            paragraph((460, 350, 560, 600)),
        ])
        with self.assertLogs("ztf_split_plan", level="WARNING") as captured:
            result = self.plan(source)
        self.assertEqual(result, PagePlan(7, 0, (
            Region("fallback", (0, 0, 600, 800)),)))
        self.assertEqual(json.loads(captured.records[0].getMessage())["reason"],
                         "too_many_columns")

    def test_invalid_sizes_and_boxes_have_finite_fallback_regions(self):
        cases = [
            (two_column_page() | {"width": math.nan}, (0, 0, 0, 800)),
            (two_column_page() | {"height": math.inf}, (0, 0, 600, 0)),
            *((two_column_page() | {"rotation": rotation}, (0, 0, 600, 800))
              for rotation in (math.nan, math.inf, -math.inf, None, True, "0", [])),
            (page([paragraph((40, 100, 40, 200))]), (0, 0, 600, 800)),
            (page([paragraph((280, 100, 40, 200))]), (0, 0, 600, 800)),
            (page([paragraph((40, 100, 280))]), (0, 0, 600, 800)),
        ]
        for source, box in cases:
            with self.subTest(source=source):
                with self.assertLogs("ztf_split_plan", level="WARNING"):
                    result = self.plan(source)
                self.assertEqual(result, PagePlan(7, 0, (Region("fallback", box),)))
                self.assertTrue(all(math.isfinite(value) for value in result.regions[0].box))

    def test_preserved_paragraph_below_full_width_body_is_not_a_header(self):
        source = two_column_page(
            paragraph((40, 50, 560, 100)),
            paragraph((285, 120, 315, 135), "copyright", False),
        )
        result = self.plan(source)
        self.assertEqual([region for region in result.regions if region.kind == "header_footer"], [])
        self.assertEqual([region for region in result.regions if region.kind == "spanning"], [
            Region("spanning", (0, 50, 600, 100), position="top"),
            Region("spanning", (0, 120, 600, 135), position="top"),
        ])
        self.assert_unique_ownership(source, result,
                                     [("column", 0)] * 2 + [("column", 1)] * 2
                                     + [("spanning", -1)] * 2)
        self.assert_content_covered(source, result)

    def test_full_width_body_above_and_below_columns_is_spanning(self):
        source = two_column_page(
            paragraph((40, 50, 560, 120)),
            paragraph((40, 690, 560, 730), "paragraph_hybrid"),
        )
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 50, 600, 120), position="top"),
            Region("spanning", (0, 690, 600, 730), position="bottom"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1),
        )))

    def test_nonfinite_coordinates_fallback_without_partial_planning(self):
        for collection in ("paragraphs", "layouts"):
            for coordinate in range(4):
                for invalid in (math.nan, math.inf, -math.inf):
                    with self.subTest(collection=collection, coordinate=coordinate, invalid=invalid):
                        source = two_column_page(layouts=[
                            {"box": [40, 370, 560, 430], "class": "figure"}])
                        source[collection][0]["box"][coordinate] = invalid
                        with self.assertLogs("ztf_split_plan", level="WARNING") as captured:
                            result = self.plan(source)
                        self.assertEqual(result, PagePlan(7, 0, (
                            Region("fallback", (0, 0, 600, 800)),)))
                        record = json.loads(captured.records[0].getMessage())
                        self.assertEqual(record["reason"], "invalid_geometry")
                        self.assertNotIn("box", record)

    def test_unsupported_pages_fallback_with_a_structured_reason(self):
        cases = [
            (page([paragraph((30, 100, 180, 600)),
                   paragraph((225, 100, 375, 600)),
                   paragraph((420, 100, 570, 600))]), "too_many_columns"),
            (two_column_page() | {"rotation": 90}, "rotation"),
            (two_column_page() | {"rotation": 360}, "rotation"),
            (page([], [{"box": [40, 100, 560, 600], "class": "figure"}]), "no_body"),
            (page([paragraph((40, 100, 560, 600), "title")]), "no_body"),
        ]
        for source, reason in cases:
            with self.subTest(reason=reason, source=source):
                with self.assertLogs("ztf_split_plan", level="WARNING") as captured:
                    result = self.plan(source)
                self.assertEqual(result, PagePlan(7, 0, (
                    Region("fallback", (0, 0, 600, 800)),)))
                record = json.loads(captured.records[0].getMessage())
                self.assertEqual(record["event"], "split_plan_fallback")
                self.assertEqual(record["reason"], reason)
                self.assertEqual(record["count"], 1)

    def test_preserved_headers_and_footers_take_priority_over_spanning(self):
        source = two_column_page(
            paragraph((40, 20, 560, 35), "header", False, True),
            paragraph((290, 760, 310, 775), "page_footer", False, True),
            paragraph((50, 200, 260, 220), "copyright", False),
        )
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("header_footer", (0, 20, 600, 35)),
            Region("header_footer", (0, 760, 600, 775)),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1),
        )))

    def test_middle_figure_splits_both_columns_in_y_order(self):
        source = two_column_page(layouts=[
            {"box": [40, 370, 560, 420], "class": "figure"},
            {"box": [40, 420, 560, 430], "class": "figure_caption"},
            {"box": [80, 431, 520, 440], "class": "unknown"},
        ])
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 370, 600, 440), position="middle"),
            Region("column", (40, 150, 280, 350), column=0),
            Region("column", (40, 450, 280, 650), column=0),
            Region("column", (320, 150, 560, 350), column=1),
            Region("column", (320, 450, 560, 650), column=1),
        )))

    def test_author_fragments_make_one_spanning_row_without_crossing_midline(self):
        source = two_column_page(
            paragraph((60, 88, 180, 103), "author_info_hybrid"),
            paragraph((282, 90, 298, 101), "author_info_hybrid"),
            paragraph((420, 89, 540, 104), "author_info_hybrid"),
        )
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 88, 600, 104), position="top"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1),
        )))

    def test_two_columns_follow_body_x_clusters(self):
        source = page([
            paragraph((40, 100, 280, 260)),
            paragraph((55, 300, 265, 600)),
            paragraph((320, 110, 560, 270), "plain text"),
            paragraph((335, 310, 550, 620), "paragraph_hybrid"),
        ])
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("column", (40, 100, 280, 600), column=0),
            Region("column", (320, 110, 560, 620), column=1),
        )))

    def test_single_column_and_frozen_interface(self):
        source = page([paragraph((40, 100, 560, 200)),
                       paragraph((55, 220, 550, 600))])
        original = copy.deepcopy(source)
        result = self.assert_planned_page(source, PagePlan(7, 1, (
            Region("column", (40, 100, 560, 600), column=0),)))
        self.assertEqual(source, original)
        self.assertEqual([field.name for field in fields(Region)],
                         ["kind", "box", "position", "column"])
        self.assertEqual([field.name for field in fields(PagePlan)],
                         ["source_index", "columns", "regions"])
        self.assertEqual(Region("fallback", (0, 0, 600, 800)).position, "none")
        self.assertEqual(Region("fallback", (0, 0, 600, 800)).column, -1)
        with self.assertRaises(FrozenInstanceError):
            result.columns = 2
        with self.assertRaises(FrozenInstanceError):
            result.regions[0].box = (0, 0, 1, 1)

    def test_nested_x_ranges_without_gutter_are_one_column(self):
        source = page([
            paragraph((40, 100, 560, 200)),
            paragraph((100, 220, 500, 300)),
            paragraph((160, 320, 440, 400)),
        ])
        result = self.assert_planned_page(source, PagePlan(7, 1, (
            Region("column", (40, 100, 560, 400), column=0),)))

    def test_single_column_keeps_wide_body_excluded_by_gutter_prefilter(self):
        # 两个窄正文之间的空白不能让远宽于它们的正文退出锚点预筛，单栏须覆盖全部正文
        source = page([
            paragraph((40, 100, 45, 200)),
            paragraph((60, 100, 80, 200)),
            paragraph((40, 250, 560, 650)),
        ])
        result = self.assert_planned_page(source, PagePlan(7, 1, (
            Region("column", (40, 100, 560, 650), column=0),)))

    def test_tiny_blocks_inside_left_column_do_not_create_false_gutter(self):
        # 左栏内两个窄块间的空白不得排除远宽于它们的左栏正文；第二块左缘超出容差时
        # 虽有 3 个左缘分组，仍应水平连通为两栏，且不得生成中部跨栏带截断右栏
        for second in ((60, 100, 80, 200), (100, 100, 120, 200)):
            with self.subTest(second=second):
                source = page([
                    paragraph((40, 100, 45, 200)),
                    paragraph(second),
                    paragraph((40, 250, 280, 650)),
                    paragraph((320, 100, 560, 650)),
                ])
                result = self.assert_planned_page(source, PagePlan(7, 2, (
                    Region("column", (40, 100, 280, 650), column=0),
                    Region("column", (320, 100, 560, 650), column=1),
                )))

    def test_author_layouts_extend_spanning_row_like_paragraphs(self):
        source = two_column_page(layouts=[
            {"box": [60, 88, 180, 108], "class": "author"},
            {"box": [282, 95, 298, 101], "class": "author"},
            {"box": [420, 89, 540, 109], "class": "author"},
        ])
        result = self.assert_planned_page(source, PagePlan(7, 2, (
            Region("spanning", (0, 88, 600, 109), position="top"),
            Region("column", (40, 150, 280, 650), column=0),
            Region("column", (320, 150, 560, 650), column=1),
        )))


if __name__ == "__main__":
    unittest.main()
