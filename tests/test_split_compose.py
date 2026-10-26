"""通过公开合成入口验证 PDF 版式、文本层隔离与原子写入。"""

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon/content/python"))
from ztf_split_plan import PagePlan, Region, plan_page
import ztf_split_compose as compose


class SplitComposeFixture:
    def setUp(self):
        # Windows 沙箱令牌不兼容 Python 3.12 的 0700 专用 ACL，夹具沿用父目录 ACL。
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode: mkdir(path, 0o777)):
            self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.mono = self.root / "mono.pdf"
        self.output = self.root / "split.pdf"

    def make_pdf(self, path, items, width=400, height=500, embed_cjk=False):
        doc = fitz.open()
        try:
            page = doc.new_page(width=width, height=height)
            if embed_cjk and any(re.search(r"[\u4e00-\u9fff]", text) for _, _, text in items):
                # 完整嵌入字体及 ToUnicode，pdf.js 离线验收不依赖外部 CMap。
                page.insert_font(fontname="fixture-cjk", fontbuffer=fitz.Font("cjk").buffer)
            for x, y, text in items:
                page.insert_text((x, y), text, fontsize=10,
                                 fontname=("fixture-cjk" if embed_cjk else "china-s")
                                 if re.search(r"[\u4e00-\u9fff]", text) else "helv")
            doc.save(path)
        finally:
            doc.close()

    def run_compose(self, plans):
        return compose.compose_split_document(str(self.source), str(self.mono), plans, str(self.output))

    def layout(self, doc):
        kind, info = doc.xref_get_key(-1, "Info")
        self.assertEqual(kind, "xref")
        kind, value = doc.xref_get_key(int(info.split()[0]), "ZTFSplitLayout")
        self.assertEqual(kind, "string")
        self.assertNotIn(": ", value)
        return json.loads(value)

    def assert_region_invariants(self, doc):
        for page, entry in zip(doc, self.layout(doc)["pages"]):
            regions = [(r["k"], fitz.Rect(r["r"]) * page.transformation_matrix)
                       for r in entry["regions"]]
            for i, (kind, box) in enumerate(regions):
                for other, peer in regions[i + 1:]:
                    if kind != other:
                        self.assertFalse(box.intersects(peer), (kind, other, box, peer))
            for block in page.get_text("rawdict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        for char in span["chars"]:
                            if char["c"].isspace():
                                continue
                            bbox = fitz.Rect(char["bbox"])
                            self.assertEqual(len({k for k, b in regions
                                                  if (b + (-0.01, -0.01, 0.01, 0.01)).contains(bbox)}),
                                             1, char)

    def make_tall_span_case(self):
        self.make_pdf(self.source, [(30, 140, "LEFT_EN"), (230, 140, "RIGHT_EN")])
        self.make_pdf(self.mono, [(100, 115, "TITLE_ZH"), (100, 130, "AUTHOR_ONE"),
                                 (30, 170, "左栏译文"), (230, 170, "右栏译文")])
        return PagePlan(0, 2, (
            Region("spanning", (0, 95, 400, 115), "top"),
            Region("column", (25, 115, 170, 220), column=0),
            Region("column", (225, 115, 370, 220), column=1)))

    def make_overlapping_spans_case(self):
        self.make_pdf(self.source, [(30, 210, "LEFT_EN"), (230, 210, "RIGHT_EN")])
        self.make_pdf(self.mono, [(100, 110, "TITLE_ZH"), (100, 125, "AUTHOR_ONE"),
                                 (100, 140, "INSTITUTE"),
                                 (30, 210, "左栏译文"), (230, 210, "右栏译文")])
        return PagePlan(0, 2, (
            Region("spanning", (0, 95, 400, 115), "top"),
            Region("spanning", (0, 125, 400, 145), "top"),
            Region("column", (25, 180, 170, 250), column=0),
            Region("column", (225, 180, 370, 250), column=1)))

    def make_gutter_case(self, collision=False, baseline=120, separated=False):
        self.make_pdf(self.source, [(30, 120, "LEFT_EN"), (230, 120, "RIGHT_EN")])
        # 左栏译文行延至 x=210，仍未到右栏原框 x=225。
        self.make_pdf(self.mono, [(30, baseline, "左栏译文保留全部十八字包括最后四个字"),
                                 (205 if collision or separated else 230,
                                  baseline + (40 if separated else 10 if collision else 0), "右栏译文")])
        return PagePlan(0, 2, (
            Region("column", (25, 100, 170, 200), column=0),
            Region("column", (225, 100, 370, 200), column=1)))

    def make_expanded_span_gutter_case(self):
        # 作者块扩到栏框高度，但 mono 正文在其下方，二者实际文字不重叠。
        plan = self.make_gutter_case(baseline=170)
        with fitz.open(self.mono) as doc:
            # 跨栏作者块扩入栏框高度，其 x 起点不能成为右栏正文边界。
            for y, text in ((95, "TITLE_ZH"), (110, "AUTHOR_ONE"), (125, "INSTITUTE")):
                doc[0].insert_text((180, y), text, fontsize=10)
            doc.saveIncr()
        return PagePlan(0, 2, plan.regions + (
            Region("spanning", (0, 80, 400, 100), "top"),))

    def make_header_title_case(self):
        self.make_pdf(self.source, [(100, 90, "HEADER"), (30, 170, "LEFT_EN"),
                                   (230, 170, "RIGHT_EN")])
        self.make_pdf(self.mono, [(100, 90, "HEADER"), (100, 105, "TITLE_ZH"),
                                 (30, 170, "左栏译文"), (230, 170, "右栏译文")])
        return PagePlan(0, 2, (
            Region("header_footer", (0, 75, 400, 94)),
            Region("spanning", (0, 95, 400, 115), "top"),
            Region("column", (25, 140, 170, 220), column=0),
            Region("column", (225, 140, 370, 220), column=1)))


class SplitComposeTests(SplitComposeFixture, unittest.TestCase):
    def test_taller_span_preserves_source_body_and_moves_columns_below_it(self):
        self.run_compose([self.make_tall_span_case()])
        with fitz.open(self.output) as doc:
            for page, english, chinese in zip(doc, ["LEFT_EN", "RIGHT_EN"], ["左栏译文", "右栏译文"]):
                self.assertIn(english, page.get_text())
                self.assertIn(chinese, page.get_text())
            self.assertEqual(doc[0].get_text().count("AUTHOR_ONE"), 1)
            span = next(r for r in self.layout(doc)["pages"][0]["regions"] if r["k"] == "span")
            span_box = fitz.Rect(span["r"]) * doc[0].transformation_matrix
            for region in self.layout(doc)["pages"][0]["regions"]:
                if region["k"] in {"en", "zh"}:
                    box = fitz.Rect(region["r"]) * doc[0].transformation_matrix
                    self.assertGreaterEqual(box.y0, span_box.y1)

    def test_expanded_spans_merge_before_embedding_shared_text_block(self):
        self.run_compose([self.make_overlapping_spans_case()])
        with fitz.open(self.output) as doc:
            text = "".join(page.get_text() for page in doc)
            for value in ("TITLE_ZH", "AUTHOR_ONE", "INSTITUTE", "LEFT_EN", "RIGHT_EN"):
                self.assertEqual(text.count(value), 1)
            self.assertEqual([r["k"] for r in self.layout(doc)["pages"][0]["regions"]],
                             ["span", "en", "zh"])

    def test_translation_can_extend_into_gutter_without_truncation(self):
        with self.assertNoLogs(compose.__name__, level="WARNING"):
            self.run_compose([self.make_gutter_case()])
        with fitz.open(self.output) as doc:
            self.assertIn("左栏译文保留全部十八字包括最后四个字", doc[0].get_text())
            self.assertNotIn("右栏译文", doc[0].get_text())
            self.assertNotIn("左栏译文", doc[1].get_text())

    def test_true_horizontal_overlap_fails_without_losing_text_or_replacing_output(self):
        plan = self.make_gutter_case(collision=True)
        self.output.write_bytes(b"previous document")
        with self.assertLogs(compose.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "^split_compose_failed$"):
                self.run_compose([plan])
        events = [json.loads(record.message) for record in logs.records]
        self.assertIn({"event": "split_compose_collision", "reason": "column_text_overlap",
                       "count": 1, "source_index": 0}, events)
        self.assertNotIn("左栏译文", "".join(logs.output))
        self.assertEqual(self.output.read_bytes(), b"previous document")

    def test_vertically_separated_gutter_lines_keep_every_character_in_own_column(self):
        self.run_compose([self.make_gutter_case(separated=True)])
        with fitz.open(self.output) as doc:
            self.assertIn("左栏译文保留全部十八字包括最后四个字", doc[0].get_text())
            self.assertIn("右栏译文", doc[1].get_text())
            self.assertNotIn("右栏译文", doc[0].get_text())
            self.assertNotIn("字", doc[1].get_text())
            self.assert_region_invariants(doc)

    def test_spanning_expansion_does_not_absorb_header_from_same_block(self):
        plan = self.make_header_title_case()
        with fitz.open(self.mono) as doc:
            self.assertTrue(any(len(b.get("lines", [])) == 2 for b in doc[0].get_text("dict")["blocks"]))
        self.run_compose([plan])
        with fitz.open(self.output) as doc:
            self.assertEqual(doc[0].get_text().count("HEADER"), 1)
            self.assertEqual(doc[1].get_text().count("HEADER"), 1)
            self.assertEqual("".join(p.get_text() for p in doc).count("TITLE_ZH"), 1)
            self.assert_region_invariants(doc)

    def test_spanning_block_does_not_absorb_independent_column_line(self):
        self.make_pdf(self.source, [(30, 120, "LEFT_EN"), (230, 120, "RIGHT_EN")])
        self.make_pdf(self.mono, [(30, 90, "TITLE_ZH"), (30, 105, "左栏完整。"),
                                 (230, 120, "右栏完整。")])
        plan = PagePlan(0, 2, (
            Region("spanning", (0, 75, 400, 94), "top"),
            Region("column", (25, 95, 170, 160), column=0),
            Region("column", (225, 95, 370, 160), column=1)))
        self.run_compose([plan])
        with fitz.open(self.output) as doc:
            self.assertEqual(doc[0].get_text().count("左栏完整。"), 1)
            span = next(r for r in self.layout(doc)["pages"][0]["regions"] if r["k"] == "span")
            rect = fitz.Rect(span["r"]) * doc[0].transformation_matrix
            self.assertNotIn("左栏", doc[0].get_text(clip=rect))
            self.assert_region_invariants(doc)

    def test_column_block_taller_than_source_frame_keeps_continuation(self):
        self.make_pdf(self.source, [(30, 110, "EN")])
        self.make_pdf(self.mono, [(30, 110, "首行"), (30, 125, "尾行。")])
        plan = PagePlan(0, 1, (Region("column", (25, 95, 170, 115), column=0),))
        self.run_compose([plan])
        with fitz.open(self.output) as doc:
            self.assertIn("尾行。", doc[0].get_text())
            self.assert_region_invariants(doc)

    def test_redaction_cannot_return_success_with_unassigned_text(self):
        plan = self.make_gutter_case()
        with patch.object(fitz.Page, "apply_redactions", return_value=False), \
                self.assertLogs(compose.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "^split_compose_failed$"):
                self.run_compose([plan])
        self.assertEqual(json.loads(logs.records[0].message), {
            "event": "split_compose_text_error", "reason": "redaction_text_mismatch",
            "count": 1, "source_index": 0})
        self.assertFalse(self.output.exists())

    def test_downward_collision_overflow_falls_back_only_affected_source_page(self):
        self.make_pdf(self.source, [(30, 490, "LEFT_EN"), (230, 490, "RIGHT_EN")])
        self.make_pdf(self.mono, [(100, 115, "TITLE_ZH"), (100, 130, "AUTHOR_ONE"),
                                 (30, 490, "左栏末行。"), (230, 490, "右栏末行。")])
        plan = PagePlan(0, 2, (
            Region("spanning", (0, 95, 400, 115), "top"),
            Region("column", (25, 115, 170, 495), column=0),
            Region("column", (225, 115, 370, 495), column=1)))
        # 在 A 页溢出的源页前后各插一页，核对回退后的页号和导航。
        for path, text in ((self.source, "NEXT_EN"), (self.mono, "下一页")):
            doc = fitz.open(path)
            try:
                doc.new_page(pno=0, width=400, height=500).insert_text(
                    (30, 120), text, fontname="china-s")
                doc.new_page(width=400, height=500).insert_text((30, 120), text, fontname="china-s")
                doc.set_page_labels([{"startpage": 0, "style": "D", "firstpagenum": 1}])
                doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(25, 100, 170, 150),
                                    "page": 1, "to": fitz.Point(30, 490)})
                doc.saveIncr()
            finally:
                doc.close()
        def regular(index):
            return PagePlan(index, 2, (
                Region("column", (25, 100, 170, 200), column=0),
                Region("column", (225, 100, 370, 200), column=1)))

        plan = PagePlan(1, plan.columns, plan.regions)
        plans = [regular(0), plan, regular(2)]
        original = list(plans)
        with self.assertLogs(compose.__name__, level="WARNING") as logs:
            stats = self.run_compose(plans)
        self.assertEqual(plans, original)
        self.assertEqual((stats["output_pages"], stats["fallback_pages"]), (5, 1))
        self.assertIn({"event": "split_compose_fallback", "reason": "column_layout_overflow",
                       "count": 1, "source_index": 1},
                      [json.loads(record.message) for record in logs.records])
        with fitz.open(self.output) as doc:
            self.assertEqual([p.rect.width for p in doc], [400, 400, 800, 400, 400])
            layout = self.layout(doc)["pages"]
            self.assertEqual([(p["src"], p["part"], p.get("fallback", False)) for p in layout],
                             [(0, "a", False), (0, "b", False), (1, "a", True),
                              (2, "a", False), (2, "b", False)])
            self.assertEqual([p.get_label() for p in doc], ["1a", "1b", "2", "3a", "3b"])
            fallback_text = doc[2].get_text()
            for text in ("LEFT_EN", "RIGHT_EN", "TITLE_ZH", "AUTHOR_ONE", "左栏末行。", "右栏末行。"):
                self.assertEqual(fallback_text.count(text), 1, text)
            self.assertEqual([link["page"] for link in doc[0].get_links()], [2, 2])
            self.assert_region_invariants(doc)

    def test_b_overflow_discards_completed_a_and_falls_back_when_output_becomes_empty(self):
        self.make_pdf(self.source, [(30, 120, "LEFT_EN"), (230, 490, "RIGHT_EN"),
                                   (100, 460, "BOTTOM_EN")])
        self.make_pdf(self.mono, [(30, 120, "左栏"), (230, 490, "右栏"),
                                 (100, 465, "BOTTOM_ZH"), (100, 480, "CONTINUED")])
        plan = PagePlan(0, 2, (
            Region("column", (25, 100, 170, 200), column=0),
            Region("column", (225, 470, 370, 495), column=1),
            Region("spanning", (0, 450, 400, 470), "bottom")))
        with self.assertLogs(compose.__name__, level="WARNING") as logs:
            stats = self.run_compose([plan])
        self.assertEqual((stats["output_pages"], stats["fallback_pages"]), (1, 1))
        self.assertIn({"event": "split_compose_fallback", "reason": "column_layout_overflow",
                       "count": 1, "source_index": 0},
                      [json.loads(record.message) for record in logs.records])
        with fitz.open(self.output) as doc:
            self.assertEqual(len(doc), 1)
            self.assertTrue(self.layout(doc)["pages"][0]["fallback"])
            self.assertEqual(doc[0].rect.width, 800)
            for text in ("LEFT_EN", "RIGHT_EN", "BOTTOM_EN", "左栏", "右栏", "BOTTOM_ZH", "CONTINUED"):
                self.assertEqual(doc[0].get_text().count(text), 1)
            self.assert_region_invariants(doc)

    def test_page_number_wrapper_keeps_one_source_number_on_both_split_pages(self):
        for path, texts in ((self.source, ("LEFT_EN", "RIGHT_EN")),
                            (self.mono, ("左栏", "右栏"))):
            self.make_pdf(path, [(54, 200, texts[0]), (318, 200, texts[1]), (300, 762, "41")],
                          width=612, height=792)
        plan = plan_page({"index": 0, "width": 612, "height": 792, "paragraphs": [
            {"box": [51, 164, 296, 711], "label": "text", "translated": True},
            {"box": [316, 165, 560, 704], "label": "text", "translated": True},
            {"box": [300, 757, 309, 763], "label": "abandon", "translated": False, "marginal": True},
        ], "layouts": [{"box": [298, 755, 310, 765], "class": "abandon"}]})
        stats = self.run_compose([plan])
        self.assertEqual((stats["output_pages"], stats["fallback_pages"]), (2, 0))
        with fitz.open(self.output) as doc:
            for page in doc:
                self.assertEqual(page.get_text().count("41"), 1)
            self.assert_region_invariants(doc)

    def test_expanded_span_is_not_mistaken_for_neighbor_column_start(self):
        plan = self.make_expanded_span_gutter_case()
        with self.assertNoLogs(compose.__name__, level="WARNING"):
            self.run_compose([plan])
        with fitz.open(self.output) as doc:
            self.assertIn("左栏译文保留全部十八字包括最后四个字", doc[0].get_text())
            self.assertIn("LEFT_EN", doc[0].get_text())
            self.assertIn("RIGHT_EN", doc[1].get_text())
            self.assertEqual("".join(p.get_text() for p in doc).count("INSTITUTE"), 1)

    def test_all_plans_validated_before_any_plan_rect_is_constructed(self):
        self.make_pdf(self.source, [(30, 120, "EN")])
        self.make_pdf(self.mono, [(30, 120, "译文")])
        for path in (self.source, self.mono):
            with fitz.open(path) as doc:
                doc.new_page(width=400, height=500)
                doc.saveIncr()
        plans = [PagePlan(0, 1, (Region("column", (25, 100, 170, 200), column=0),)),
                 PagePlan(1, 1, (Region("column", (25, float("nan"), 170, 200), column=0),))]
        with fitz.open(self.source) as source, fitz.open(self.mono) as mono, \
                patch.object(compose.fitz, "Rect", side_effect=AssertionError("premature geometry")), \
                self.assertLogs(compose.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(ValueError, "^invalid_region_box$"):
                compose._validate_inputs(source, mono, plans)
        self.assertEqual(json.loads(logs.records[0].message), {
            "event": "split_compose_invalid_plan", "reason": "invalid_region_box",
            "count": 1, "source_index": 1})

    def test_all_region_boxes_rejected_during_validation_before_composition(self):
        self.make_pdf(self.source, [(30, 120, "EN")])
        self.make_pdf(self.mono, [(30, 120, "译文")])
        bad_boxes = [(0, float("nan"), 40, 50), (0, 0, float("inf"), 50),
                     (0, 0, 40, float("-inf")), (40, 0, 20, 50), (0, 0, 0, 50),
                     (0, 20, 40, 10), (0, 0, 40), (0, 0, "secret-token", 50),
                     (False, 0, 40, 50), (500, 0, 600, 50), (0, 0, 10 ** 1000, 50)]
        for kind in ("column", "spanning", "header_footer", "fallback"):
            for box in bad_boxes:
                regions = (Region("column", (25, 100, 170, 200), column=0),
                           Region(kind, box, "top", column=0))
                plans = [PagePlan(0, 0 if kind == "fallback" else 1, regions)]
                with self.subTest(kind=kind, box=box), \
                        patch.object(compose, "_compose_pages") as compose_pages, \
                        self.assertLogs(compose.__name__, level="WARNING") as logs:
                    with self.assertRaisesRegex(RuntimeError, "^split_compose_failed$"):
                        self.run_compose(plans)
                    compose_pages.assert_not_called()
                    events = [json.loads(record.message) for record in logs.records]
                    self.assertEqual(events[0], {"event": "split_compose_invalid_plan",
                                                "reason": "invalid_region_box", "count": 1,
                                                "source_index": 0})
                    self.assertNotIn("secret-token", "".join(logs.output))
                self.assertFalse(self.output.exists())

    def test_cleanup_unlink_failure_preserves_original_failure_and_redacts_log(self):
        self.make_pdf(self.source, [(30, 120, "EN")])
        self.make_pdf(self.mono, [(30, 120, "译文")])
        plans = [PagePlan(0, 1, (Region("column", (25, 100, 170, 200), column=0),))]
        self.output.write_bytes(b"previous document")
        with patch.object(fitz.Document, "save", side_effect=ValueError("save-secret-token")), \
                patch.object(os, "unlink", side_effect=PermissionError("cleanup-secret-token")), \
                self.assertLogs(compose.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "^split_compose_failed$") as raised:
                self.run_compose(plans)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual([json.loads(r.message) for r in logs.records], [
            {"event": "split_compose_error", "reason": "composition_failed", "count": 1,
             "error_type": "ValueError"},
            {"event": "split_compose_cleanup_error", "reason": "unlink_failed", "count": 1,
             "error_type": "PermissionError"}])
        self.assertNotIn("secret-token", "".join(logs.output))
        self.assertEqual(self.output.read_bytes(), b"previous document")

    def test_cleanup_close_failure_does_not_stop_remaining_cleanup(self):
        self.make_pdf(self.source, [(30, 120, "EN")])
        self.make_pdf(self.mono, [(30, 120, "译文")])
        plans = [PagePlan(0, 1, (Region("column", (25, 100, 170, 200), column=0),))]
        closed = []
        original = fitz.Document.close

        def close_then_fail(doc):
            original(doc)
            closed.append(doc)
            raise PermissionError("close-secret-token")

        with patch.object(compose, "_compose_pages", side_effect=ValueError("original-secret")), \
                patch.object(fitz.Document, "close", close_then_fail), \
                self.assertLogs(compose.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "^split_compose_failed$"):
                self.run_compose(plans)
        self.assertEqual(len(closed), 3)
        self.assertTrue(all(doc.is_closed for doc in closed))
        events = [json.loads(r.message) for r in logs.records]
        self.assertEqual([event["reason"] for event in events],
                         ["composition_failed", "close_failed", "close_failed", "close_failed"])
        self.assertTrue(all(event["count"] == 1 for event in events))
        self.assertNotIn("secret", "".join(logs.output))

    def test_embedding_temporary_close_error_is_redacted_on_success_and_failure(self):
        self.make_pdf(self.source, [(30, 120, "EN")])
        self.make_pdf(self.mono, [(30, 120, "译文")])
        plans = [PagePlan(0, 1, (Region("column", (25, 100, 170, 200), column=0),))]
        original_open, original_close = fitz.open, fitz.Document.close
        for fail_embedding in (False, True):
            opened = []

            def tracked_open(*args, **kwargs):
                document = original_open(*args, **kwargs)
                opened.append(document)
                return document

            def close_then_fail(document):
                original_close(document)
                if document is opened[3]:
                    raise PermissionError("temporary-close-secret-token")

            with self.subTest(fail_embedding=fail_embedding), \
                    patch.object(fitz, "open", side_effect=tracked_open), \
                    patch.object(fitz.Document, "close", close_then_fail), \
                    self.assertLogs(compose.__name__, level="WARNING") as logs:
                if fail_embedding:
                    with patch.object(fitz.Page, "show_pdf_page", side_effect=ValueError("embed-secret")):
                        with self.assertRaisesRegex(RuntimeError, "^split_compose_failed$"):
                            self.run_compose(plans)
                else:
                    self.assertEqual(self.run_compose(plans)["output_pages"], 1)
            self.assertTrue(all(document.is_closed for document in opened))
            events = [json.loads(record.message) for record in logs.records]
            self.assertEqual(events[0], {"event": "split_compose_cleanup_error",
                                        "reason": "close_failed", "count": 1,
                                        "error_type": "PermissionError"})
            self.assertEqual(len(events), 2 if fail_embedding else 1)
            if fail_embedding:
                self.assertEqual(events[1], {"event": "split_compose_error",
                                            "reason": "composition_failed", "count": 1,
                                            "error_type": "ValueError"})
            self.assertNotIn("secret", "".join(logs.output))

    def test_two_columns_keep_font_size_and_remove_other_column_text(self):
        self.make_pdf(self.source, [(30, 120, "LEFT_EN"), (230, 120, "RIGHT_EN"),
                                   (5, 450, "OUTSIDE_SECRET")])
        self.make_pdf(self.mono, [(30, 120, "左栏译文"), (230, 120, "右栏译文"),
                                 (5, 450, "区域外文字")])
        plan = PagePlan(0, 2, (Region("column", (25, 100, 170, 200), column=0),
                               Region("column", (225, 100, 370, 200), column=1)))
        stats = self.run_compose([plan])
        self.assertEqual(stats["output_pages"], 2)
        doc = fitz.open(self.output)
        try:
            for page, english, chinese, other in zip(doc, ["LEFT_EN", "RIGHT_EN"],
                                                     ["左栏译文", "右栏译文"],
                                                     ["RIGHT_EN", "LEFT_EN"]):
                self.assertEqual(tuple(page.rect), (0, 0, 400, 500))
                text = page.get_text(sort=False, clip=fitz.INFINITE_RECT())
                self.assertIn(english, text)
                self.assertIn(chinese, text)
                self.assertNotIn(other, text)
                self.assertNotIn("OUTSIDE", text)
                self.assertNotIn("区域外", text)
                self.assertLess(text.index(english), text.index(chinese))
                for block in page.get_text("dict")["blocks"]:
                    for line in block.get("lines", []):
                        for span in line["spans"]:
                            self.assertAlmostEqual(span["size"], 10, places=3)
                            if re.search(r"[\u4e00-\u9fff]", span["text"]):
                                self.assertGreaterEqual(span["bbox"][0], 206)
                            else:
                                self.assertLessEqual(span["bbox"][2], 194)
        finally:
            doc.close()

    def test_single_column_expands_page_without_scaling_and_writes_pdf_coordinates(self):
        self.make_pdf(self.source, [(30, 120, "SINGLE_EN")])
        self.make_pdf(self.mono, [(30, 120, "单栏译文")])
        self.run_compose([PagePlan(0, 1, (Region("column", (25, 100, 375, 300), column=0),))])
        doc = fitz.open(self.output)
        try:
            self.assertEqual(len(doc), 1)
            self.assertEqual(doc[0].rect.width, 736)
            data = self.layout(doc)
            self.assertEqual(data, {"v": 1, "pages": [{"src": 0, "part": "a", "regions": [
                {"k": "en", "r": [12, 200, 362, 400]},
                {"k": "zh", "r": [374, 200, 724, 400]},
            ]}]})
            for word in doc[0].get_text("words"):
                kind = "zh" if re.search(r"[\u4e00-\u9fff]", word[4]) else "en"
                region = next(r["r"] for r in data["pages"][0]["regions"] if r["k"] == kind)
                self.assertTrue(fitz.Rect(region).contains(fitz.Rect(word[:4]) * ~doc[0].transformation_matrix))
        finally:
            doc.close()

    def test_author_band_and_taller_translation_are_complete_once_in_stream_order(self):
        authors = [(25, 80, "Alice Institute"), (155, 80, "Bob Institute"), (285, 80, "Carol Institute")]
        self.make_pdf(self.source, [(10, 20, "HEADER"), (10, 480, "FOOTER"),
                                   (35, 50, "ORIGINAL_TITLE"), *authors,
                                   (30, 130, "LEFT_EN"), (230, 130, "RIGHT_EN")])
        self.make_pdf(self.mono, [(10, 20, "不应使用的页眉"), (35, 45, "完整中文标题"),
                                 *authors, (30, 130, "左栏译文"), (230, 130, "右栏译文")])
        # 标题译文比冻结的源框向上超出，必须保全整个相交文字块。
        plan = PagePlan(0, 2, (
            Region("column", (25, 110, 170, 200), column=0),
            Region("column", (225, 110, 370, 200), column=1),
            Region("spanning", (0, 45, 400, 55), "top"),
            Region("spanning", (0, 65, 400, 85), "top"),
            Region("header_footer", (0, 5, 400, 25)),
            Region("header_footer", (0, 465, 400, 490)),
        ))
        self.run_compose([plan])
        doc = fitz.open(self.output)
        try:
            a, b = [p.get_text(sort=False) for p in doc]
            self.assertIn("完整中文标题", a)
            self.assertNotIn("ORIGINAL_TITLE", a + b)
            for _, _, author in authors:
                self.assertEqual((a + b).count(author), 1)
            for text in (a, b):
                self.assertEqual(text.count("HEADER"), 1)
                self.assertEqual(text.count("FOOTER"), 1)
                self.assertNotIn("不应使用", text)
            self.assertLess(a.index("FOOTER"), a.index("完整中文标题"))
            self.assertLess(a.index("Carol Institute"), a.index("LEFT_EN"))
            self.assertLess(a.index("LEFT_EN"), a.index("左栏译文"))
            self.assertNotIn("完整中文标题", b)
            self.assertEqual([r["k"] for r in self.layout(doc)["pages"][0]["regions"]],
                             ["hf", "hf", "span", "span", "en", "zh"])
        finally:
            doc.close()

    def test_middle_figure_stays_on_a_bottom_span_on_b_and_b_compacts_blank(self):
        items = [(30, 120, "LEFT_UP"), (230, 120, "RIGHT_UP"),
                 (30, 350, "LEFT_DOWN"), (230, 350, "RIGHT_DOWN"),
                 (100, 260, "ORIGINAL_FIGURE"), (100, 440, "ORIGINAL_BOTTOM")]
        self.make_pdf(self.source, items)
        self.make_pdf(self.mono, [(30, 120, "左上"), (230, 120, "右上"),
                                 (30, 350, "左下"), (230, 350, "右下"),
                                 (100, 260, "中部跨栏图"), (100, 440, "底部跨栏")])
        # 图像与矢量横跨区域边界；删除区域外文字不能删掉它们。
        doc = fitz.open(self.mono)
        try:
            doc[0].draw_rect((90, 210, 310, 275), color=(1, 0, 0), fill=(1, 0, 0))
            image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False)
            image.clear_with(0)
            doc[0].insert_image((190, 220, 210, 240), pixmap=image)
            doc.saveIncr()
        finally:
            doc.close()
        plan = PagePlan(0, 2, tuple(
            Region("column", box, column=column) for column, box in [
                (0, (25, 100, 170, 200)), (0, (25, 300, 170, 390)),
                (1, (225, 100, 370, 200)), (1, (225, 300, 370, 390)),
            ]) + (Region("spanning", (0, 200, 400, 280), "middle"),
                  Region("spanning", (0, 420, 400, 455), "bottom")))
        self.run_compose([plan])
        doc = fitz.open(self.output)
        try:
            a, b = [p.get_text(sort=False) for p in doc]
            self.assertIn("中部跨栏图", a)
            self.assertNotIn("中部跨栏图", b)
            self.assertNotIn("底部跨栏", a)
            self.assertIn("底部跨栏", b)
            self.assertNotIn("ORIGINAL_", a + b)
            self.assertLess(a.index("LEFT_DOWN"), a.index("左上"))
            self.assertAlmostEqual(doc[0].search_for("LEFT_DOWN")[0].y0, 339.25, places=2)
            self.assertAlmostEqual(doc[1].search_for("RIGHT_DOWN")[0].y0, 259.25, places=2)
            self.assertAlmostEqual(doc[1].search_for("右下")[0].y0, 260, places=2)
            # get_image_info 还报告 clip 外的图像资源，按实际像素核对可见结果。
            pixmap = doc[0].get_pixmap(alpha=False)
            self.assertEqual(pixmap.pixel(200, 230), (0, 0, 0))
            self.assertEqual(pixmap.pixel(100, 220), (255, 0, 0))
            self.assertTrue(any(d["fill"] == (1, 0, 0) for d in doc[0].get_drawings()))
        finally:
            doc.close()

    def test_resources_closed_and_temporary_files_removed_on_success_and_failures(self):
        self.make_pdf(self.source, [(30, 120, "EN")])
        self.make_pdf(self.mono, [(30, 120, "译文")])
        plans = [PagePlan(0, 1, (Region("column", (25, 100, 170, 200), column=0),))]
        original_open = fitz.open
        for fault in (None, "embed", "save", "replace"):
            for existing in (False, True):
                with self.subTest(fault=fault, existing=existing):
                    if self.output.exists():
                        self.output.unlink()
                    if existing:
                        self.output.write_bytes(b"existing completed document")
                    opened = []

                    def tracked_open(*args, **kwargs):
                        doc = original_open(*args, **kwargs)
                        opened.append(doc)
                        return doc

                    boundary = {"embed": (fitz.Page, "show_pdf_page"),
                                "save": (fitz.Document, "save"),
                                "replace": (os, "replace")}
                    with patch.object(fitz, "open", side_effect=tracked_open):
                        if fault:
                            owner, name = boundary[fault]
                            with patch.object(owner, name, side_effect=RuntimeError("secret-token")):
                                with self.assertRaisesRegex(RuntimeError, "split_compose_failed") as raised:
                                    self.run_compose(plans)
                                self.assertNotIn("secret-token", str(raised.exception))
                        else:
                            self.run_compose(plans)
                    self.assertTrue(opened)
                    self.assertTrue(all(doc.is_closed for doc in opened))
                    self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                                     ["mono.pdf", "source.pdf", "split.pdf"] if existing or not fault
                                     else ["mono.pdf", "source.pdf"])
                    if fault and existing:
                        self.assertEqual(self.output.read_bytes(), b"existing completed document")

    def test_invalid_plans_and_mismatched_pages_fail_without_partial_output(self):
        self.make_pdf(self.source, [(30, 120, "EN")])
        self.make_pdf(self.mono, [(30, 120, "译文")])
        for plans in ([], [PagePlan(-1, 1, ())], [PagePlan(1, 1, ())],
                      [PagePlan(0, 3, ())], [PagePlan(0, 1, ())],
                      [PagePlan(0, 1, (Region("column", (0, float("nan"), 40, 50), column=0),))],
                      [PagePlan(0, 1, (Region("column", (0, 50, 40, 30), column=0),))]):
            with self.subTest(plans=plans), self.assertRaises(Exception):
                self.run_compose(plans)
            self.assertFalse(self.output.exists())
        self.mono.unlink()
        self.make_pdf(self.mono, [(30, 120, "译文")], width=410)
        with self.assertRaises(Exception):
            self.run_compose([PagePlan(0, 1, (Region("column", (25, 100, 170, 200), column=0),))])
        self.assertFalse(self.output.exists())

    def test_module_loads_with_runner_file_loader_without_sys_path_mutation(self):
        module = Path(__file__).resolve().parents[1] / "addon/content/python/ztf_split_compose.py"
        script = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('ztf_split_compose', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert callable(module.compose_split_document)
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(module)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fallback_uses_whole_pages_and_marks_metadata(self):
        self.make_pdf(self.source, [(10, 30, "WHOLE_EN")])
        self.make_pdf(self.mono, [(10, 30, "整页译文")])
        # S2 在尺寸无效时允许回退框为零，合成必须从真实页重取尺寸。
        with self.assertLogs(compose.__name__, level="WARNING") as logs:
            stats = self.run_compose([PagePlan(0, 0, (Region("fallback", (0, 0, 0, 0)),))])
        self.assertIn('"event": "split_compose_fallback"', logs.output[0])
        self.assertEqual(stats["fallback_pages"], 1)
        doc = fitz.open(self.output)
        try:
            self.assertEqual(len(doc), 1)
            self.assertEqual(doc[0].rect.width, 800)
            self.assertEqual(doc[0].get_text().strip(), "WHOLE_EN\n整页译文")
            self.assertEqual(self.layout(doc)["pages"][0], {
                "src": 0, "part": "a", "fallback": True, "regions": [
                    {"k": "en", "r": [0, 0, 400, 500]},
                    {"k": "zh", "r": [400, 0, 800, 500]}]})
        finally:
            doc.close()

    def test_rotated_fallback_keeps_display_geometry_and_font_size(self):
        for path, text in ((self.source, "ROTATED_EN"), (self.mono, "旋转译文")):
            self.make_pdf(path, [(30, 120, text)])
            doc = fitz.open(path)
            try:
                doc[0].set_rotation(90)
                doc.saveIncr()
            finally:
                doc.close()
        self.run_compose([PagePlan(0, 0, (Region("fallback", (0, 0, 500, 400)),))])
        doc = fitz.open(self.output)
        try:
            self.assertEqual(tuple(doc[0].rect), (0, 0, 1000, 400))
            self.assertIn("ROTATED_EN", doc[0].get_text())
            self.assertIn("旋转译文", doc[0].get_text())
            word = doc[0].search_for("ROTATED_EN")[0]
            self.assertAlmostEqual(word.x0, 377.01, places=2)
            self.assertAlmostEqual(word.y0, 30, places=2)
            for block in doc[0].get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        self.assertAlmostEqual(span["size"], 10, places=3)
        finally:
            doc.close()



if __name__ == "__main__":
    unittest.main()
