"""用合成 PDF 验证质量门不会误报同词，同时拒绝作者缺字、拆栏和重复。"""

import json
import sys
import unittest
from unittest.mock import patch

import pymupdf as fitz

from test_split_compose import SplitComposeFixture
from ztf_split_plan import PagePlan, Region
import split_quality_check as quality


class SplitQualityTests(SplitComposeFixture, unittest.TestCase):
    def check(self, key):
        with patch.object(quality, "pdfjs_order", return_value=(True, {})):
            report = quality.build_report(self.source, self.output, self.mono)
        return next(c for c in report["checks"] if c["id"] == key)

    def test_missing_pdfjs_dependency_is_a_failed_check_and_nonzero_exit(self):
        self.make_authors()
        for dependency in ("node", "worker"):
            with self.subTest(dependency=dependency), patch.object(
                    quality.shutil, "which", return_value=None if dependency == "node" else "node"), patch.object(
                    quality, "ROOT", self.root):
                report = quality.build_report(self.source, self.output, self.mono)
                checks = [c for c in report["checks"] if c["id"] == "pdfjs_order"]
                self.assertEqual(len(checks), 1, report)
                self.assertEqual(checks[0]["status"], "FAIL")
                self.assertNotEqual(report["status"], "PASS")
                with patch.object(quality, "build_report", return_value=report), patch.object(
                        sys, "argv", ["quality", "--source", str(self.source), "--dual", str(self.output)]), patch("builtins.print"):
                    self.assertNotEqual(quality.main(), 0)

    def make_short_english_line(self, allowed_in_mono=False):
        self.make_pdf(self.source, [(30, 250, "LEFT")])
        self.make_pdf(self.mono, [(30, 250, "左栏译文"), (230, 250, "右栏译文")])
        phrase = "various training and initialization algorithms."
        with fitz.open(self.source) as doc:
            doc[0].insert_text((230, 250), phrase, fontsize=6)
            doc.saveIncr()
        if allowed_in_mono:
            with fitz.open(self.mono) as doc:
                doc[0].insert_text((30, 270), phrase, fontsize=6)
                doc.saveIncr()
        self.run_compose([PagePlan(0, 2, (
            Region("column", (25, 220, 190, 300), column=0),
            Region("column", (225, 220, 395, 300), column=1)))])
        return phrase

    def test_short_english_line_from_other_column_is_rejected(self):
        phrase = self.make_short_english_line()
        self.edit_output(lambda doc: doc[0].insert_text((210, 280), phrase, fontsize=4))
        result = self.check("chinese_cross_column")
        self.assertEqual(result["status"], "FAIL", result)

    def test_short_english_line_already_in_corresponding_mono_column_is_allowed(self):
        self.make_short_english_line(allowed_in_mono=True)
        result = self.check("chinese_cross_column")
        self.assertEqual(result["status"], "PASS", result)

    def test_quality_report_field_names_are_english(self):
        self.make_authors()
        with patch.object(quality, "pdfjs_order", return_value=(True, {})):
            report = quality.build_report(self.source, self.output, self.mono)

        def check_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertTrue(key.isascii(), key)
                    check_keys(child)
            elif isinstance(value, list):
                for child in value:
                    check_keys(child)

        check_keys(report)
        self.edit_output(lambda doc: doc[0].insert_text(
            (15, 50), "错", fontname="china-s", fontsize=10))
        with patch.object(quality, "pdfjs_order", return_value=(True, {})):
            check_keys(quality.build_report(self.source, self.output, self.mono))

    def test_quality_error_report_omits_raw_exception_message(self):
        secret = "sk-private-example-secret正文"
        with patch.object(quality, "build_report", side_effect=RuntimeError(secret)), patch.object(
                sys, "argv", ["quality", "--source", "source.pdf", "--dual", "dual.pdf", "--json"]), patch(
                "builtins.print") as display:
            self.assertEqual(quality.main(), 1)
        rendered = display.call_args.args[0]
        self.assertNotIn(secret, rendered)
        self.assertIn("RuntimeError", rendered)

    def make_composition_fallback(self):
        self.make_pdf(self.source, [(100, 50, "HEADER"), (100, 110, "Iman Sharafaldin"),
                                   (100, 130, "Canadian Institute for Cybersecurity"),
                                   (30, 490, "LEFT"), (230, 490, "RIGHT")])
        self.make_pdf(self.mono, [(100, 50, "HEADER"), (100, 115, "作者姓名译文"),
                                 (100, 130, "完整机构译文"), (30, 490, "左栏末行"),
                                 (230, 490, "右栏末行")])
        for path in (self.source, self.mono):
            with fitz.open(path) as doc:
                for box in ((100, 38, 145, 54), (100, 98, 145, 115)):
                    doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(box),
                                        "uri": "https://example.test/"})
                doc.saveIncr()
        raw = {"index": 0, "width": 400, "height": 500, "layouts": [], "paragraphs": [
            {"box": [100, 38, 145, 54], "label": "abandon", "marginal": True,
             "translated": False},
            {"box": [0, 95, 400, 115], "label": "title"},
            {"box": [25, 115, 170, 495], "label": "text"},
            {"box": [225, 115, 370, 495], "label": "text"}]}
        self.mono.with_suffix(".pdf.layout.json").write_text(
            json.dumps({"pages": [raw]}), encoding="utf-8")
        from ztf_split_plan import plan_page
        plan = plan_page(raw)
        self.assertEqual(plan.columns, 2)
        stats = self.run_compose([plan])
        self.assertEqual(stats["fallback_pages"], 1)

    def test_composition_fallback_uses_actual_page_and_link_structure(self):
        self.make_composition_fallback()
        for key in ("pages", "link_counts"):
            result = self.check(key)
            self.assertEqual(result["status"], "PASS", result)
        detail = self.check("pages")["evidence"]["pages"][0]
        self.assertEqual(detail["fallback_stage"], "composition")
        with patch.object(quality, "pdfjs_order", return_value=(True, {})):
            report = quality.build_report(self.source, self.output, self.mono)
        self.assertEqual(report["status"], "PASS", report)

    def test_fallback_flag_does_not_excuse_incomplete_regions(self):
        self.make_composition_fallback()

        def shrink_region(doc):
            xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
            layout = json.loads(doc.xref_get_key(xref, "ZTFSplitLayout")[1])
            layout["pages"][0]["regions"][0]["r"][2] -= 20
            doc.xref_set_key(xref, "ZTFSplitLayout", fitz.get_pdf_str(json.dumps(layout)))

        self.edit_output(shrink_region)
        self.assertEqual(self.check("pages")["status"], "FAIL")

    def test_author_groups_in_planning_fallback_match_each_input_once(self):
        for preserved in (False, True):
            with self.subTest(preserved=preserved):
                self.make_authors()
                if preserved:
                    self.make_pdf(self.mono, [(100, 50, "HEADER"),
                                             (100, 100, "Iman Sharafaldin and Other Author"),
                                             (100, 125, "Canadian Institute for Cybersecurity"),
                                             (30, 250, "左文"), (230, 250, "右文")])
                self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": [{
                    "index": 0, "width": 400, "height": 500, "paragraphs": [], "layouts": []}]}), encoding="utf-8")
                self.run_compose([PagePlan(0, 0, (Region("fallback", (0, 0, 400, 500)),))])
                with patch.object(quality, "pdfjs_order", return_value=(True, {})):
                    report = quality.build_report(self.source, self.output, self.mono)
                self.assertEqual(report["status"], "PASS", report)

    def test_incomplete_or_duplicate_author_group_in_fallback_is_rejected(self):
        for side in ("en", "zh"):
            for defect in ("missing_character", "duplicate"):
                with self.subTest(side=side, defect=defect):
                    self.make_composition_fallback()

                    def change(doc):
                        page = doc[0]
                        if defect == "missing_character":
                            line = next(l for l in quality.lines(page) if
                                        (l["box"].x0 > 400) == (side == "zh") and
                                        ("Iman" in l["text"] or "作者" in l["text"]))
                            page.add_redact_annot(fitz.Rect(line["chars"][0]["bbox"]) + (.2, .2, -.2, -.2))
                            page.apply_redactions(images=0, graphics=0)
                        else:
                            if side == "zh":
                                page.insert_font(fontname="duplicate-cjk", fontbuffer=fitz.Font("cjk").buffer)
                            page.insert_text((450 if side == "zh" else 50, 100),
                                             "作者姓名译文" if side == "zh" else "Iman Sharafaldin",
                                             fontname="duplicate-cjk" if side == "zh" else "helv", fontsize=6)

                    self.edit_output(change)
                    self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_fallback_allows_mono_author_line_shorter_than_original(self):
        self.make_composition_fallback()
        self.make_pdf(self.mono, [(100, 50, "HEADER"), (100, 115, "Iman"),
                                 (100, 130, "完整机构译文"), (30, 490, "左栏末行"),
                                 (230, 490, "右栏末行")])
        self.run_compose([PagePlan(0, 0, (Region("fallback", (0, 0, 400, 500)),))])
        self.assertEqual(self.check("authors")["status"], "PASS")

    def test_planning_fallback_is_reported_separately(self):
        self.make_pdf(self.source, [(30, 100, "WHOLE SOURCE")])
        self.make_pdf(self.mono, [(30, 100, "整页译文")])
        self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": [{
            "index": 0, "width": 400, "height": 500, "paragraphs": [], "layouts": []}]}), encoding="utf-8")
        self.run_compose([PagePlan(0, 0, (Region("fallback", (0, 0, 400, 500)),))])
        result = self.check("pages")
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["evidence"]["pages"][0]["fallback_stage"], "planning")

    def make_header_and_figure(self):
        items = [(150, 50, "Intrusion Traffic Characterization"),
                 (130, 120, "Traffic"), (30, 250, "LEFT"), (230, 250, "RIGHT")]
        self.make_pdf(self.source, items)
        self.make_pdf(self.mono, items)
        self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": [{
            "index": 0, "width": 400, "height": 500, "layouts": [], "paragraphs": [
                {"box": [0, 38, 400, 54], "label": "abandon", "marginal": True},
                {"box": [0, 100, 400, 140], "label": "title"},
                {"box": [25, 220, 170, 280], "label": "text"},
                {"box": [225, 220, 370, 280], "label": "text"}]}]}), encoding="utf-8")
        self.run_compose([PagePlan(0, 2, (
            Region("header_footer", (0, 38, 400, 54)),
            Region("spanning", (0, 100, 400, 140), "top"),
            Region("column", (25, 220, 170, 280), column=0),
            Region("column", (225, 220, 370, 280), column=1)))])
    def test_header_word_inside_independent_spanning_figure_is_not_a_duplicate(self):
        self.make_header_and_figure()
        result = self.check("headers_footers")
        self.assertEqual(result["status"], "PASS", result)

    def test_marginal_text_inside_a_spanning_figure_is_not_a_header(self):
        self.make_header_and_figure()
        sidecar = self.mono.with_suffix(".pdf.layout.json")
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        raw["pages"][0]["paragraphs"].append({
            "box": [130, 110, 160, 125], "label": "abandon", "marginal": True})
        sidecar.write_text(json.dumps(raw), encoding="utf-8")
        result = self.check("headers_footers")
        self.assertEqual(result["status"], "PASS", result)

    def test_missing_header_character_is_rejected(self):
        self.make_header_and_figure()

        def remove(doc):
            page = doc[0]
            page.add_redact_annot((150, 39, 152, 51))
            page.apply_redactions(images=0, graphics=0)

        self.edit_output(remove)
        self.assertEqual(self.check("headers_footers")["status"], "FAIL")

    def test_header_copied_into_body_is_rejected(self):
        self.make_header_and_figure()
        self.edit_output(lambda doc: doc[0].insert_text(
            (50, 260), "Intrusion Traffic Characterization", fontsize=6))
        self.assertEqual(self.check("headers_footers")["status"], "FAIL")

    def test_header_band_swallowing_body_is_rejected(self):
        items = [(100, 50, "HEADER"), (100, 100, "BODY"),
                 (30, 250, "LEFT"), (230, 250, "RIGHT")]
        self.make_pdf(self.source, items)
        self.make_pdf(self.mono, items)
        self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": [{
            "index": 0, "width": 400, "height": 500, "layouts": [], "paragraphs": [
                {"box": [95, 85, 135, 105], "label": "text", "marginal": False},
                {"box": [25, 220, 170, 280], "label": "text"},
                {"box": [225, 220, 370, 280], "label": "text"}]}]}), encoding="utf-8")
        self.run_compose([PagePlan(0, 2, (
            Region("header_footer", (0, 38, 400, 110)),
            Region("column", (25, 220, 170, 280), column=0),
            Region("column", (225, 220, 370, 280), column=1)))])
        self.assertEqual(self.check("headers_footers")["status"], "FAIL")

    def make_page_number(self, evidence, number="1", body_number=False):
        self.make_authors()
        baseline = 485 if evidence == "source_edge" else 440
        band = (0, baseline - 15, 400, baseline + 5)
        for path in (self.source, self.mono):
            with fitz.open(path) as doc:
                doc[0].insert_text((195, baseline), number, fontsize=10)
                if body_number:
                    for x in (30, 230):
                        doc[0].insert_text((x, 270), "Count " + number + " and 1108", fontsize=6)
                doc.saveIncr()
        sidecar = self.mono.with_suffix(".pdf.layout.json")
        raw = json.loads(sidecar.read_text(encoding="utf-8"))["pages"][0]
        if evidence == "marginal":
            raw["paragraphs"].append({"box": [190, band[1], 210, band[3]], "label": "abandon", "marginal": True})
        elif evidence == "layout":
            raw["layouts"].append({"box": [193, baseline - 12, 203, baseline - 2], "class": "abandon"})
        sidecar.write_text(json.dumps({"pages": [raw]}), encoding="utf-8")
        self.run_compose([PagePlan(0, 2, (
            Region("header_footer", (0, 38, 400, 54)),
            Region("header_footer", band),
            Region("spanning", (0, 85, 400, 140), "top"),
            Region("column", (25, 220, 170, 280), column=0),
            Region("column", (225, 220, 370, 280), column=1)))])
        return band

    def test_short_page_numbers_allow_existing_body_digits_but_reject_extra_copies(self):
        for number in ("1", "12", "108"):
            for part in (0, 1):
                for side, x in (("en", 55), ("zh", 210)):
                    with self.subTest(number=number, part=part, side=side):
                        self.make_page_number("marginal", number=number, body_number=True)
                        result = self.check("headers_footers")
                        self.assertEqual(result["status"], "PASS", result)
                        self.edit_output(lambda doc: doc[part].insert_text((x, 260), number, fontsize=6))
                        result = self.check("headers_footers")
                        self.assertEqual(result["status"], "FAIL", result)

    def test_edge_body_is_not_expected_as_header_footer(self):
        for page_count in (1, 2):
            for explicit_body in (False, True):
                with self.subTest(page_count=page_count, explicit_body=explicit_body):
                    for path in (self.source, self.mono):
                        self.make_pdf(path, [(30, 25, "EDGE BODY"), (230, 25, "RIGHT BODY")])
                        if page_count == 2:
                            with fitz.open(path) as doc:
                                doc.fullcopy_page(0)
                                doc.saveIncr()
                    paragraphs = [{"box": [25, 10, 170, 280], "label": "text"},
                                  {"box": [225, 10, 370, 280], "label": "text"}]
                    if explicit_body:
                        for paragraph in paragraphs:
                            paragraph["marginal"] = False
                    self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": [
                        {"index": i, "width": 400, "height": 500,
                         "paragraphs": paragraphs, "layouts": []} for i in range(page_count)]}), encoding="utf-8")
                    from ztf_split_plan import plan_page
                    raw = json.loads(self.mono.with_suffix(".pdf.layout.json").read_text(encoding="utf-8"))
                    self.run_compose([plan_page(page) for page in raw["pages"]])
                    result = self.check("headers_footers")
                    self.assertEqual(result["status"], "PASS", result)

    def make_quality_navigation(self):
        from test_split_links import SplitLinksFixture
        plans = SplitLinksFixture.make_navigation(self)
        pages = []
        for plan in plans:
            paragraphs = [{"box": list(r.box), "label": "abandon" if r.kind == "header_footer" else
                           "title" if r.kind == "spanning" else "text",
                           "marginal": r.kind == "header_footer"}
                          for r in plan.regions if r.kind != "fallback"]
            pages.append({"index": plan.source_index, "width": 400, "height": 500,
                          "paragraphs": paragraphs, "layouts": []})
        self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": pages}), encoding="utf-8")
        self.run_compose(plans)
        return plans

    def test_single_page_edge_text_without_sidecar_is_not_repetition(self):
        self.make_pdf(self.source, [(30, 25, "EDGE BODY"), (230, 25, "RIGHT BODY")])
        self.make_pdf(self.mono, [(30, 25, "EDGE BODY"), (230, 25, "RIGHT BODY")])
        self.run_compose([PagePlan(0, 2, (
            Region("column", (25, 10, 170, 280), column=0),
            Region("column", (225, 10, 370, 280), column=1)))])
        result = self.check("headers_footers")
        self.assertEqual(result["status"], "PASS", result)

    def test_internal_link_to_wrong_part_is_rejected(self):
        for side in ("en", "zh"):
            with self.subTest(side=side):
                self.make_quality_navigation()
                self.assertEqual(self.check("link_sides")["status"], "PASS")

                def change(doc):
                    link = next(l for l in doc[0].get_links() if l.get("page") == 2
                                and l["to"].y > 0 and (l["from"].x0 > doc[0].rect.width / 2) == (side == "zh"))
                    link["page"] = 1
                    doc[0].update_link(link)

                self.edit_output(change)
                result = self.check("link_sides")
                self.assertEqual(result["status"], "FAIL", result)

    def test_native_page_top_targets_are_allowed_on_both_sides(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                for path in (self.source, self.mono):
                    self.make_pdf(path, [(30, 100, "BODY")])
                    with fitz.open(path) as doc:
                        doc[0].insert_link({"kind": fitz.LINK_GOTO,
                                            "from": fitz.Rect(30, 85, 65, 105),
                                            "page": 0, "to": fitz.Point(40, 0)})
                        doc.saveIncr()
                raw = {"index": 0, "width": 400, "height": 500, "layouts": [],
                       "paragraphs": [] if fallback else [{"box": [20, 0, 380, 400], "label": "text"}]}
                self.mono.with_suffix(".pdf.layout.json").write_text(
                    json.dumps({"pages": [raw]}), encoding="utf-8")
                plan = PagePlan(0, 0 if fallback else 1, (
                    Region("fallback" if fallback else "column", (0, 0, 400, 500)
                           if fallback else (20, 0, 380, 400), column=-1 if fallback else 0),))
                self.run_compose([plan])
                result = self.check("link_sides")
                self.assertEqual(result["status"], "PASS", result)

    def test_link_targets_are_bound_to_geometry_even_with_same_or_empty_text(self):
        for text in ("SAME", ""):
            for side in ("en", "zh"):
                with self.subTest(text=text, side=side):
                    plans = self.make_quality_navigation()
                    path = self.source if side == "en" else self.mono
                    with fitz.open(path) as doc:
                        for y, x in ((280, 240), (310, 40)):
                            if text:
                                doc[0].insert_text((30, y), text, fontsize=10)
                            doc[0].insert_link({"kind": fitz.LINK_GOTO,
                                                "from": fitz.Rect(30, y - 10, 70, y + 4),
                                                "page": 1, "to": fitz.Point(x, 200)})
                        doc.saveIncr()
                    self.run_compose(plans)
                    self.assertEqual(self.check("link_sides")["status"], "PASS")

                    def swap(doc):
                        links = [l for l in doc[0].get_links()
                                 if l["from"].y0 in (270, 300)
                                 and (l["from"].x0 > doc[0].rect.width / 2) == (side == "zh")]
                        self.assertEqual(len(links), 2)
                        destinations = [(l["page"], l["to"]) for l in reversed(links)]
                        for link, (page, point) in zip(links, destinations):
                            link.update(page=page, to=point)
                            doc[0].update_link(link)

                    self.edit_output(swap)
                    result = self.check("link_sides")
                    self.assertEqual(result["status"], "FAIL", result)

    def test_toc_to_wrong_part_is_rejected(self):
        self.make_quality_navigation()
        self.assertEqual(self.check("toc")["status"], "PASS")

        def change(doc):
            toc = doc.get_toc(False)
            toc[0][2] = 2
            toc[0][3]["page"] = 1
            doc.set_toc(toc)

        self.edit_output(change)
        result = self.check("toc")
        self.assertEqual(result["status"], "FAIL", result)

    def test_input_specific_targets_and_navigation_fallbacks_are_allowed(self):
        plans = self.make_quality_navigation()
        for key in ("link_counts", "link_sides", "toc"):
            result = self.check(key)
            self.assertEqual(result["status"], "PASS", result)
        # 同一输入源页的英文目标在 B，单语目标在 A；不能借用原文目标坐标。
        with fitz.open(self.mono) as doc:
            link = doc[0].get_links()[0]
            link["to"] = fitz.Point(40, 200)
            doc[0].update_link(link)
            doc.saveIncr()
        self.run_compose(plans)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "PASS", result)

    def test_page_top_fallback_to_wrong_part_or_body_is_rejected(self):
        for side in ("en", "zh"):
            for defect in ("part", "not_top"):
                with self.subTest(side=side, defect=defect):
                    self.make_quality_navigation()

                    def change(doc):
                        link = next(l for l in doc[0].get_links() if l.get("page") == 2 and l["to"].y == 0
                                    and (l["from"].x0 > doc[0].rect.width / 2) == (side == "zh"))
                        if defect == "part":
                            link["page"] = 1
                        else:
                            link["to"] = fitz.Point(link["to"].x, 200)
                        doc[0].update_link(link)

                    self.edit_output(change)
                    result = self.check("link_sides")
                    self.assertEqual(result["status"], "FAIL", result)

    def test_header_link_uses_source_side_for_header_target_fallback(self):
        plans = self.make_quality_navigation()
        with fitz.open(self.source) as doc:
            link = next(l for l in doc[1].get_links() if l["from"].y0 == 5)
            link["to"] = fitz.Point(250, 15)
            doc[1].update_link(link)
            doc.saveIncr()
        self.run_compose(plans)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "PASS", result)

    def test_toc_page_top_fallback_to_body_is_rejected(self):
        self.make_quality_navigation()

        def change(doc):
            toc = doc.get_toc(False)
            toc[1][3]["to"] = fitz.Point(40, 200)
            doc.set_toc(toc)

        self.edit_output(change)
        result = self.check("toc")
        self.assertEqual(result["status"], "FAIL", result)

    def test_bottom_spanning_target_uses_b_page(self):
        plans = self.make_quality_navigation()
        bottom = Region("spanning", (0, 420, 400, 460), "bottom")
        plans[1] = PagePlan(1, 2, plans[1].regions + (bottom,))
        for path in (self.source, self.mono):
            with fitz.open(path) as doc:
                doc[1].insert_text((100, 440), "BOTTOM SPAN")
                doc.saveIncr()
        sidecar = self.mono.with_suffix(".pdf.layout.json")
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        raw["pages"][1]["paragraphs"].append({"box": list(bottom.box), "label": "title", "marginal": False})
        sidecar.write_text(json.dumps(raw), encoding="utf-8")
        self.run_compose(plans)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "PASS", result)

        def change(doc):
            link = next(l for l in doc[0].get_links() if l.get("page") == 2 and l["to"].y > 420
                        and l["from"].x0 > doc[0].rect.width / 2)
            link["page"] = 1
            link["to"] = fitz.Point(120, 60)
            doc[0].update_link(link)

        self.edit_output(change)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "FAIL", result)

    def test_links_and_targets_in_expanded_input_text_are_allowed(self):
        plans = self.make_quality_navigation()
        for path in (self.source, self.mono):
            with fitz.open(path) as doc:
                doc[1].insert_text((360, 170), "EDGE", fontsize=10)
                doc[1].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(360, 158, 388, 174),
                                    "page": 0, "to": fitz.Point(40, 180)})
                link = doc[0].get_links()[0]
                link["to"] = fitz.Point(385, 165)
                doc[0].update_link(link)
                doc.saveIncr()
        self.run_compose(plans)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "PASS", result)

    def test_expanded_middle_span_moves_b_page_chinese_link_up(self):
        for path, translated in ((self.source, False), (self.mono, True)):
            self.make_pdf(path, [(30, 120, "LEFT"), (230, 120, "RIGHT"),
                                 (30, 250, "LEFT BELOW"), (230, 250, "RIGHT BELOW")])
            with fitz.open(path) as doc:
                doc[0].insert_text((100, 161 if translated else 153),
                                   "SPAN\nCONTINUED\nLAST" if translated else "SPAN", fontsize=10)
                doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(230, 238, 250, 254),
                                    "page": 0, "to": fitz.Point(240, 250)})
                doc.saveIncr()
        raw = {"index": 0, "width": 400, "height": 500, "layouts": [], "paragraphs": [
            {"box": [20, 100, 180, 130], "label": "text"},
            {"box": [220, 100, 380, 130], "label": "text"},
            {"box": [20, 220, 180, 400], "label": "text"},
            {"box": [220, 220, 380, 400], "label": "text"},
            {"box": [0, 140, 400, 160], "output_box": [0, 140, 400, 187.99], "label": "title"}]}
        self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": [raw]}), encoding="utf-8")
        from ztf_split_plan import plan_page
        self.run_compose([plan_page(raw)])
        with fitz.open(self.output) as doc:
            band = next(r for r in self.layout(doc)["pages"][0]["regions"] if r["k"] == "span")
            rect = fitz.Rect(band["r"]) * doc[0].transformation_matrix
            self.assertAlmostEqual(rect.y0, 140, places=2)
            self.assertAlmostEqual(rect.y1, 187.99, places=2)
            for link in doc[1].get_links():
                self.assertEqual(link["page"], 1)
                chinese = link["from"].x0 > doc[1].rect.width / 2
                self.assertAlmostEqual(link["to"].y, 202.01 if chinese else 230, places=2)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "PASS", result)

        def overlap_middle_metadata(doc):
            xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
            layout = self.layout(doc)
            bands = layout["pages"][0]["regions"]
            band = next(r for r in bands if r["k"] == "span")
            # 两个重叠区间覆盖原带，实际占高仍为 47.99 pt。
            bands.remove(band)
            bands.extend([{"k": "span", "r": [0, 325, 400, 360]},
                          {"k": "span", "r": [0, 312.01, 400, 340]}])
            doc.xref_set_key(xref, "ZTFSplitLayout", fitz.get_pdf_str(json.dumps(layout)))

        self.edit_output(overlap_middle_metadata)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "PASS", result)

    def test_swapped_link_parts_with_unchanged_target_counts_are_rejected(self):
        plans = self.make_quality_navigation()
        with fitz.open(self.source) as doc:
            for y, text, x in ((280, "ALPHA", 240), (310, "BETA", 40)):
                doc[0].insert_text((30, y), text, fontsize=10)
                doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(30, y - 10, 70, y + 4),
                                    "page": 1, "to": fitz.Point(x, 200)})
            doc.saveIncr()
        self.run_compose(plans)
        self.assertEqual(self.check("link_sides")["status"], "PASS")

        def change(doc):
            for link in doc[0].get_links():
                if link["from"].x0 < doc[0].rect.width / 2 and link["from"].y0 in (270, 300):
                    link["page"] = 3 - link["page"]
                    doc[0].update_link(link)

        self.edit_output(change)
        result = self.check("link_sides")
        self.assertEqual(result["status"], "FAIL", result)

    def test_page_number_and_its_metadata_missing_together_are_rejected(self):
        for evidence in ("marginal", "layout", "source_edge"):
            for part in (0, 1):
                with self.subTest(evidence=evidence, part=part):
                    band = self.make_page_number(evidence)
                    self.assertEqual(self.check("headers_footers")["status"], "PASS")

                    def remove(doc):
                        page = doc[part]
                        page.add_redact_annot(band)
                        page.apply_redactions(images=0, graphics=0)
                        xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
                        layout = json.loads(doc.xref_get_key(xref, "ZTFSplitLayout")[1])
                        regions = layout["pages"][part]["regions"]
                        regions[:] = [r for r in regions if not (r["k"] == "hf" and r["r"][1] < 100)]
                        doc.xref_set_key(xref, "ZTFSplitLayout", fitz.get_pdf_str(json.dumps(layout)))

                    self.edit_output(remove)
                    with patch.object(quality, "pdfjs_order", return_value=(True, {})):
                        report = quality.build_report(self.source, self.output, self.mono)
                    result = next(c for c in report["checks"] if c["id"] == "headers_footers")
                    self.assertEqual(result["status"], "FAIL", result)
                    self.assertEqual(report["status"], "FAIL", report)

    def make_authors(self):
        self.make_pdf(self.source, [(100, 50, "HEADER"),
                                   (100, 100, "Iman Sharafaldin and Other Author"),
                                   (100, 125, "Canadian Institute for Cybersecurity"),
                                   (30, 250, "LEFT"), (230, 250, "RIGHT")])
        self.make_pdf(self.mono, [(100, 50, "HEADER"), (100, 100, "作者姓名译文"),
                                 (100, 125, "完整机构译文"),
                                 (30, 250, "左文"), (230, 250, "右文")])
        raw = {"index": 0, "width": 400, "height": 500, "layouts": [], "paragraphs": [
            {"box": [0, 38, 400, 54], "label": "abandon", "marginal": True},
            {"box": [95, 85, 300, 140], "output_box": [95, 85, 300, 140],
             "label": "plain text", "translated": True, "marginal": False, "author": False},
            {"box": [25, 220, 170, 280], "label": "text"},
            {"box": [225, 220, 370, 280], "label": "text"}]}
        self.mono.with_suffix(".pdf.layout.json").write_text(
            json.dumps({"pages": [raw]}), encoding="utf-8")
        self.run_compose([PagePlan(0, 2, (
            Region("header_footer", (0, 38, 400, 54)),
            Region("spanning", (0, 85, 400, 140), "top"),
            Region("column", (25, 220, 170, 280), column=0),
            Region("column", (225, 220, 370, 280), column=1)))])

    def make_spanning_title(self, position="top"):
        self.make_authors()
        baseline = {"top": 175, "middle": 250, "bottom": 330}[position]
        band = (0, baseline - 15, 400, baseline + 5)
        for path, text in ((self.source, "ORIGINAL TITLE"), (self.mono, "完整译文标题")):
            with fitz.open(path) as doc:
                doc[0].insert_text((100, baseline), text, fontsize=10,
                                   fontname="china-s" if path == self.mono else "helv")
                doc.saveIncr()
        raw = json.loads(self.mono.with_suffix(".pdf.layout.json").read_text(encoding="utf-8"))
        raw["pages"][0]["paragraphs"].append({"box": list(band), "label": "title"})
        self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps(raw), encoding="utf-8")
        from ztf_split_plan import plan_page
        self.run_compose([plan_page(raw["pages"][0])])

    def test_all_spanning_positions_preserve_mono_content_once(self):
        for position in ("top", "middle", "bottom"):
            with self.subTest(position=position):
                self.make_spanning_title(position)
                result = self.check("spanning")
                self.assertEqual(result["status"], "PASS", result)

    def test_spanning_title_original_residue_missing_char_and_copies_are_rejected(self):
        for defect in ("original", "missing_first", "duplicate", "wrong_part"):
            with self.subTest(defect=defect):
                self.make_spanning_title()

                def change(doc):
                    if defect == "original":
                        doc[0].insert_text((55, 265), "ORIGINAL TITLE", fontsize=4)
                    elif defect == "missing_first":
                        char = next(c for l in quality.lines(doc[0]) for c in l["chars"] if c["c"] == "完"
                                    and c["bbox"][1] > 140)
                        doc[0].add_redact_annot(fitz.Rect(char["bbox"]) + (2, 2, -2, -2))
                        doc[0].apply_redactions(images=0, graphics=0)
                    else:
                        doc[1].insert_font(fontname="title-copy", fontbuffer=fitz.Font("cjk").buffer)
                        doc[1].insert_text((210, 265), "完整译文标题", fontname="title-copy", fontsize=5)
                        if defect == "wrong_part":
                            doc[0].add_redact_annot((0, 155, 400, 185))
                            doc[0].apply_redactions(images=0, graphics=0)

                self.edit_output(change)
                self.assertEqual(self.check("spanning")["status"], "FAIL")

    def test_translated_author_and_affiliation_match_mono_in_one_complete_span(self):
        self.make_authors()
        result = self.check("authors")
        self.assertEqual(result["status"], "PASS", result)

    def test_preserved_english_names_with_partly_translated_affiliation_are_allowed(self):
        self.make_authors()
        self.make_pdf(self.mono, [(100, 50, "HEADER"),
                                 (100, 100, "Iman Sharafaldin and Other Author加拿大"),
                                 (100, 125, "Canadian Institute 网络安全研究所"),
                                 (30, 250, "左文"), (230, 250, "右文")], embed_cjk=True)
        from ztf_split_plan import plan_page
        raw = json.loads(self.mono.with_suffix(".pdf.layout.json").read_text(encoding="utf-8"))
        self.run_compose([plan_page(raw["pages"][0])])
        result = self.check("authors")
        self.assertEqual(result["status"], "PASS", result)
        # 同一作者原文在正文多出一份，不能复用合法跨栏整行的豁免。
        self.edit_output(lambda doc: doc[0].insert_text(
            (55, 260), "Iman Sharafaldin and Other Author", fontsize=4))
        result = self.check("authors")
        self.assertEqual(result["status"], "FAIL", result)
        self.assertTrue(any("extra_original_author_copy" in error
                            for error in result["evidence"]["errors"]), result)

    def make_preserved_authors_with_mentions(self):
        items = [(100, 50, "Authors: Max Landauer and Colleagues"),
                 (100, 100, "Max Landauer"), (100, 125, "Austrian Institute of Technology"),
                 (30, 250, "LEFT"), (230, 250, "RIGHT"),
                 (30, 270, "Reference: Max Landauer 2024")]
        self.make_pdf(self.source, items)
        self.make_pdf(self.mono, items)
        self.mono.with_suffix(".pdf.layout.json").write_text(json.dumps({"pages": [{
            "index": 0, "width": 400, "height": 500, "layouts": [], "paragraphs": [
                {"box": [0, 38, 400, 54], "label": "abandon", "marginal": True},
                {"box": [95, 85, 310, 140], "label": "text", "author": True},
                {"box": [25, 220, 190, 290], "label": "text"},
                {"box": [225, 220, 370, 290], "label": "text"}]}]}), encoding="utf-8")
        self.run_compose([PagePlan(0, 2, (
            Region("header_footer", (0, 38, 400, 54)),
            Region("spanning", (0, 85, 400, 140), "top"),
            Region("column", (25, 220, 190, 290), column=0),
            Region("column", (225, 220, 370, 290), column=1)))])

    def test_existing_header_and_reference_mentions_are_not_author_copies(self):
        self.make_preserved_authors_with_mentions()
        result = self.check("authors")
        self.assertEqual(result["status"], "PASS", result)

    def test_extra_copy_of_an_existing_reference_is_rejected(self):
        self.make_preserved_authors_with_mentions()
        self.edit_output(lambda doc: doc[0].insert_text(
            (55, 280), "Reference: Max Landauer 2024", fontsize=4))
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_reference_copied_to_wrong_part_is_rejected(self):
        self.make_preserved_authors_with_mentions()
        self.edit_output(lambda doc: doc[1].insert_text(
            (55, 270), "Reference: Max Landauer 2024", fontsize=4))
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def edit_output(self, change):
        with fitz.open(self.output) as doc:
            change(doc)
            doc.saveIncr()

    def test_author_missing_character_is_rejected(self):
        self.make_authors()

        def remove_character(doc):
            page = doc[0]
            char = next(c for l in quality.lines(page) for c in l["chars"] if c["c"] == "构")
            page.add_redact_annot(fitz.Rect(char["bbox"]) + (2, 2, -2, -2))
            page.apply_redactions(images=0, graphics=0)

        self.edit_output(remove_character)
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_duplicate_translated_author_group_is_rejected(self):
        self.make_authors()

        def duplicate(doc):
            page = doc[1]
            page.insert_font(fontname="duplicate-cjk", fontbuffer=fitz.Font("cjk").buffer)
            page.insert_text((100, 100), "作者姓名译文", fontname="duplicate-cjk", fontsize=10)
            page.insert_text((100, 125), "完整机构译文", fontname="duplicate-cjk", fontsize=10)
            typ, info = doc.xref_get_key(-1, "Info")
            xref = int(info.split()[0])
            layout = json.loads(doc.xref_get_key(xref, "ZTFSplitLayout")[1])
            layout["pages"][1]["regions"].append({"k": "span", "r": [0, 360, 400, 415]})
            doc.xref_set_key(xref, "ZTFSplitLayout", fitz.get_pdf_str(json.dumps(layout)))

        self.edit_output(duplicate)
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_extra_original_author_copy_beside_translated_span_is_rejected(self):
        self.make_authors()
        self.edit_output(lambda doc: doc[0].insert_text(
            (55, 260), "Iman Sharafaldin and Other Author", fontsize=4))
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_original_author_copy_with_added_text_is_rejected(self):
        self.make_authors()
        self.edit_output(lambda doc: doc[0].insert_text(
            (55, 260), "Iman Sharafaldin and Other Author EXTRA", fontsize=4))
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_translated_author_copy_with_added_text_is_rejected(self):
        self.make_authors()

        def duplicate(doc):
            page = doc[0]
            page.insert_font(fontname="extra-cjk", fontbuffer=fitz.Font("cjk").buffer)
            page.insert_text((210, 260), "作者姓名译文 EXTRA", fontname="extra-cjk", fontsize=4)

        self.edit_output(duplicate)
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def append_later_source_page_with_author_copy(self, original=False):
        for path in (self.source, self.mono):
            with fitz.open(path) as doc:
                doc.new_page(width=400, height=500)
                doc.saveIncr()

        def append(doc):
            page = doc.new_page(width=400, height=500)
            if original:
                page.insert_text((210, 100), "Iman Sharafaldin and Other Author", fontsize=4)
            else:
                page.insert_font(fontname="copy-cjk", fontbuffer=fitz.Font("cjk").buffer)
                page.insert_text((210, 100), "作者姓名译文", fontname="copy-cjk", fontsize=6)
                page.insert_text((210, 125), "完整机构译文", fontname="copy-cjk", fontsize=6)
            xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
            layout = json.loads(doc.xref_get_key(xref, "ZTFSplitLayout")[1])
            layout["pages"].append({"src": 1, "part": "a", "regions": [
                {"k": "en", "r": [0, 0, 200, 500]}, {"k": "zh", "r": [200, 0, 400, 500]}]})
            doc.xref_set_key(xref, "ZTFSplitLayout", fitz.get_pdf_str(json.dumps(layout)))

        self.edit_output(append)

    def test_translated_author_copy_on_later_source_page_is_rejected(self):
        self.make_authors()
        self.append_later_source_page_with_author_copy()
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_original_author_copy_on_later_source_page_is_rejected(self):
        self.make_authors()
        self.append_later_source_page_with_author_copy(original=True)
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_translated_author_copy_with_added_text_on_later_source_page_is_rejected(self):
        self.make_authors()
        self.append_later_source_page_with_author_copy(original=True)

        def duplicate(doc):
            page = doc[-1]
            page.add_redact_annot(page.rect)
            page.apply_redactions(images=0, graphics=0)
            page.insert_font(fontname="later-cjk", fontbuffer=fitz.Font("cjk").buffer)
            page.insert_text((210, 260), "作者姓名译文 EXTRA", fontname="later-cjk", fontsize=4)

        self.edit_output(duplicate)
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_authors_split_into_sides_are_rejected_even_when_other_span_exists(self):
        self.make_authors()

        def split(doc):
            xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
            layout = json.loads(doc.xref_get_key(xref, "ZTFSplitLayout")[1])
            regions = layout["pages"][0]["regions"]
            regions[:] = [r for r in regions if r["k"] != "span"]
            regions.extend([{"k": "en", "r": [0, 360, 200, 415]},
                            {"k": "zh", "r": [200, 360, 400, 415]},
                            {"k": "span", "r": [0, 310, 400, 340]}])
            doc.xref_set_key(xref, "ZTFSplitLayout", fitz.get_pdf_str(json.dumps(layout)))

        self.edit_output(split)
        self.assertEqual(self.check("authors")["status"], "FAIL")

    def test_author_rows_in_separate_spans_are_rejected(self):
        self.make_authors()

        def separate(doc):
            xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
            layout = json.loads(doc.xref_get_key(xref, "ZTFSplitLayout")[1])
            regions = layout["pages"][0]["regions"]
            regions[:] = [r for r in regions if r["k"] != "span"]
            regions.extend([{"k": "span", "r": [0, 392, 400, 415]},
                            {"k": "span", "r": [0, 360, 400, 390]}])
            doc.xref_set_key(xref, "ZTFSplitLayout", fitz.get_pdf_str(json.dumps(layout)))

        self.edit_output(separate)
        self.assertEqual(self.check("authors")["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
