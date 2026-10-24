"""用合成 PDF 验证分栏导航；离线运行，不依赖论文或服务。"""

import json
import re
import unittest
from unittest.mock import patch

import pymupdf as fitz

from tests.test_split_compose import SplitComposeFixture
from ztf_split_plan import PagePlan, Region
import ztf_split_compose as compose


SPECIAL_LABEL_PREFIX_CASES = [("(i)", r"(\(i\))"), ("A\\1", r"(A\\1)"),
                              ("第一页", "<FEFF7B2C4E009875>"), ("é", "<e9>"),
                              ("(i)", r"(\050i\051)"), ("", "()")]
LABEL_STYLE_CASES = [("D", 40, ["40", "41", "42"]),
                     ("R", 9, ["IX", "X", "XI"]), ("r", 4, ["iv", "v", "vi"]),
                     ("A", 26, ["Z", "AA", "BB"]), ("a", 27, ["aa", "bb", "cc"]),
                     (None, 8, ["", "", ""])]


class SplitLinksFixture(SplitComposeFixture):
    def make_navigation(self, labels=True, rotation=0):
        plans = [
            PagePlan(0, 1, (Region("column", (20, 90, 380, 400), column=0),)),
            PagePlan(1, 2, (
                Region("header_footer", (0, 0, 400, 30)),
                Region("spanning", (0, 40, 400, 80), "top"),
                Region("column", (20, 100, 180, 400), column=0),
                Region("column", (220, 100, 380, 400), column=1))),
            PagePlan(2, 0, (Region("fallback", (0, 0, 400, 500)),))]
        for path, translated in ((self.source, False), (self.mono, True)):
            doc = fitz.open()
            try:
                for index in range(3):
                    page = doc.new_page(width=400, height=500)
                    page.insert_text((30, 150), "MONO" if translated else "SOURCE")
                    if index == 1:
                        page.insert_text((230, 150), "RIGHT")
                        page.insert_text((100, 65), "TITLE")
                        page.insert_text((100, 20), "HEADER")
                if rotation:
                    doc[2].set_rotation(rotation)
                # 两侧的目标页坐标不同，中文链接必须从 mono 读取。
                target = (250, 210) if translated else (240, 200)
                destinations = [target, (120, 60), (250, 15), (250, 450)]
                for i, point in enumerate(destinations):
                    doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(30, 110 + i * 30, 55, 125 + i * 30),
                                        "page": 1, "to": fitz.Point(point)})
                doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(5, 250, 50, 270),
                                    "uri": "https://example.org/mono" if translated else "https://example.org/source"})
                # 跨栏链接只取 mono；页眉链接只取 source，复制到 A、B。
                doc[1].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(100, 50, 130, 70),
                                    "uri": "https://example.org/title"})
                doc[1].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(100, 5, 130, 25),
                                    "page": 1, "to": fitz.Point(240, 200)})
                doc[1].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(230, 120, 250, 140),
                                    "page": 0, "to": fitz.Point(40, 180)})
                doc[2].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(30, 120, 55, 140),
                                    "page": 2, "to": fitz.Point(40, 180)})
                if labels:
                    doc.set_page_labels([{"startpage": 0, "style": "D", "firstpagenum": 40}])
                if not translated:
                    doc.set_toc([[1, "正文", 2, {"kind": fitz.LINK_GOTO, "to": fitz.Point(240, 200), "bold": True}],
                                 [2, "跨栏标题", 2, {"kind": fitz.LINK_GOTO, "to": fitz.Point(120, 60)}],
                                 [1, "回退", 3, {"kind": fitz.LINK_GOTO, "to": fitz.Point(40, 180)}]])
                doc.save(path)
            finally:
                doc.close()
        return plans

    def run_navigation(self, plans):
        with self.assertLogs(compose.__name__, level="WARNING") as capture:
            stats = self.run_compose(plans)
        return stats, [json.loads(record.getMessage()) for record in capture.records]

    def make_special_label_case(self, encoded):
        plans = self.make_navigation()
        with fitz.open(self.source) as doc:
            # 独立生成合法源规则，不使用被测编码器或读取接口桩。
            nums = f"0 <</P <FEFF7B2C4E009875>>> 1 <</P {encoded}>> 2 <<>>"
            doc.xref_set_key(doc.pdf_catalog(), "PageLabels", f"<</Nums [{nums}]>>")
            doc.saveIncr()
        return plans

    def make_nested_label_case(self, style, start):
        plans = self.make_navigation()
        with fitz.open(self.source) as doc:
            def obj(value):
                xref = doc.get_new_xref()
                doc.update_object(xref, value)
                return f"{xref} 0 R"

            rule = obj(f"<</P (S-) /St {start} "
                       + (f"/S /{style}" if style else "") + ">>")
            # 间接数组、规则和多层 Kids；最后一页重置规则及默认 /St。
            nums = obj(f"[0 {rule}]")
            left = obj(f"<</Limits [0 1] /Nums {nums}>>")
            right = obj("<</Limits [2 2] /Nums [2 <</S /D /P (end-)>>]>>")
            branch = obj(f"<</Kids [{left} {right}] /Limits [0 2]>>")
            root = obj(f"<</Kids [{branch}]>>")
            doc.xref_set_key(doc.pdf_catalog(), "PageLabels", root)
            doc.saveIncr()
        return plans


class SplitLinksTests(SplitLinksFixture, unittest.TestCase):
    def regions(self, doc, index, kind):
        return [fitz.Rect(r["r"]) * doc[index].transformation_matrix
                for r in self.layout(doc)["pages"][index]["regions"] if r["k"] == kind]

    def test_internal_destinations_keep_input_side_and_column(self):
        self.run_navigation(self.make_navigation())
        with fitz.open(self.output) as doc:
            links = sorted([l for l in doc[0].get_links() if l["kind"] == fitz.LINK_GOTO],
                           key=lambda l: (l["from"].x0, l["from"].y0))
            self.assertEqual(len(links), 8)
            for link, kind, original in ((links[0], "en", (240, 200)), (links[4], "zh", (250, 210))):
                self.assertEqual(link["page"], 2)
                box = self.regions(doc, 2, kind)[0]
                self.assertAlmostEqual(link["to"].x, box.x0 + original[0] - 220, places=3)
                self.assertAlmostEqual(link["to"].y, original[1], places=3)

    def test_span_links_only_mono_and_headers_only_source(self):
        self.run_navigation(self.make_navigation())
        with fitz.open(self.output) as doc:
            a, b = doc[1].get_links(), doc[2].get_links()
            self.assertEqual(sum(l.get("uri", "").endswith("/title") for l in a + b), 1)
            self.assertEqual(sum(self.regions(doc, n, "hf")[0].contains(l["from"])
                                 for n in (1, 2) for l in doc[n].get_links()), 2)
            inbound = [l for l in doc[0].get_links() if l.get("page") == 1 and l["to"].y == 60]
            self.assertEqual(len(inbound), 1)
            self.assertTrue(self.regions(doc, 1, "span")[0].contains(inbound[0]["to"]))
            for kind in ("en", "zh"):
                box = self.regions(doc, 2, kind)[0]
                link = next(l for l in b if box.contains(l["from"]))
                self.assertEqual(link["page"], 0)
                self.assertTrue(self.regions(doc, 0, kind)[0].contains(link["to"]))

    def test_external_uris_are_unchanged_and_rects_are_clipped(self):
        self.run_navigation(self.make_navigation())
        with fitz.open(self.output) as doc:
            links = [l for l in doc[0].get_links() if l["kind"] == fitz.LINK_URI]
            self.assertEqual({l["uri"] for l in links}, {"https://example.org/source", "https://example.org/mono"})
            for link in links:
                kind = "zh" if link["uri"].endswith("/mono") else "en"
                box = self.regions(doc, 0, kind)[0]
                self.assertAlmostEqual(link["from"].x0, box.x0, places=3)
                self.assertAlmostEqual(link["from"].width, 30, places=3)

    def test_unmapped_and_header_targets_land_at_page_top_and_log(self):
        _, events = self.run_navigation(self.make_navigation())
        reasons = {e["reason"] for e in events if e["event"] == "split_navigation_fallback"}
        self.assertTrue({"target_header_footer", "target_unmapped"} <= reasons)
        with fitz.open(self.output) as doc:
            links = [l for l in doc[0].get_links() if l["kind"] == fitz.LINK_GOTO]
            self.assertEqual(sum(l["to"].y == 0 for l in links), 5)
            # 页脚下方空白位于右栏，对应 B 页；中文侧仍在右半页顶。
            missing = [l for l in links if l["from"].y0 == 200]
            self.assertEqual([l["page"] for l in missing], [2, 2])
            self.assertGreater(max(l["to"].x for l in missing), doc[2].rect.width / 2)

    def test_toc_uses_english_mapping_and_keeps_hierarchy(self):
        self.run_navigation(self.make_navigation())
        with fitz.open(self.output) as doc:
            toc = doc.get_toc(simple=False)
            self.assertEqual([(e[0], e[1], e[2]) for e in toc], [(1, "正文", 3), (2, "跨栏标题", 2), (1, "回退", 4)])
            self.assertTrue(self.regions(doc, 2, "en")[0].contains(toc[0][3]["to"]))
            self.assertTrue(toc[0][3]["bold"])
            self.assertEqual(toc[1][3]["to"].y, 0)

    def make_external_toc(self):
        plans = self.make_navigation()
        with fitz.open(self.source) as doc:
            doc.set_toc([[1, "外部页", -1], [2, "外部命名目标", -1],
                         [2, "启动文件", -1], [1, "外部 URI", -1], [1, "正文", 2]], collapse=0)
            doc.set_toc_item(0, dest_dict={"kind": fitz.LINK_GOTOR, "file": "remote.pdf",
                                          "page": 1, "to": fitz.Point(17, 29), "zoom": 1.5})
            doc.set_toc_item(1, dest_dict={"kind": fitz.LINK_GOTOR, "file": "remote.pdf",
                                          "page": -1, "to": "chapter"})
            doc.set_toc_item(2, dest_dict={"kind": fitz.LINK_LAUNCH, "file": "launch.exe"})
            doc.set_toc_item(3, dest_dict={"kind": fitz.LINK_URI, "uri": "https://example.org/(toc)"})
            doc.saveIncr()
        return plans

    def test_legal_gotor_toc_is_disabled_and_remaining_entries_survive(self):
        stats, events = self.run_navigation(self.make_external_toc())
        with fitz.open(self.source) as source, fitz.open(self.output) as output:
            before, after = source.get_toc(False), output.get_toc(False)
            self.assertEqual([e[:2] for e in after], [e[:2] for e in before])
            self.assertEqual(before[0][3]["kind"], fitz.LINK_GOTOR)
            self.assertEqual(before[0][3]["page"], 1)
            self.assertTrue(all(e[3]["kind"] == fitz.LINK_NONE for e in after[:3]))
            self.assertEqual(after[3][3]["uri"], before[3][3]["uri"])
            self.assertEqual(after[4][3]["kind"], fitz.LINK_GOTO)
        self.assertEqual(stats["toc_entries"], 5)
        self.assertEqual(stats["toc_unresolved"], 3)
        self.assertEqual([e["toc_index"] for e in events if e["reason"] == "unsupported_toc_action"],
                         [0, 1, 2])

    def test_one_external_toc_failure_keeps_hierarchy_and_other_actions(self):
        plans = self.make_external_toc()
        set_item = fitz.Document.set_toc_item

        def fail_uri(document, idx, **kwargs):
            if kwargs.get("dest_dict", {}).get("kind") == fitz.LINK_URI:
                raise ValueError("private-path-and-token")
            return set_item(document, idx, **kwargs)

        with patch.object(fitz.Document, "set_toc_item", new=fail_uri):
            stats, events = self.run_navigation(plans)
        with fitz.open(self.output) as doc:
            toc = doc.get_toc(False)
            self.assertEqual([e[:2] for e in toc],
                             [[1, "外部页"], [2, "外部命名目标"], [2, "启动文件"],
                              [1, "外部 URI"], [1, "正文"]])
            self.assertEqual([e[3]["kind"] for e in toc],
                             [fitz.LINK_NONE, fitz.LINK_NONE, fitz.LINK_NONE, fitz.LINK_NONE, fitz.LINK_GOTO])
        self.assertEqual(stats["toc_unresolved"], 4)
        self.assertIn({"event": "split_navigation_unresolved", "reason": "toc_action_failed",
                       "count": 1, "toc_index": 3, "side": "en", "error_type": "ValueError"}, events)
        self.assertNotIn("private-path-and-token", json.dumps(events))

    def test_invalid_zoom_returns_fixed_reason_and_counts_links_and_toc(self):
        plans = self.make_navigation()
        get_links, get_toc = fitz.Page.get_links, fitz.Document.get_toc
        invalid = [float("nan"), float("inf"), -float("inf"), "bad", None]

        def links(page):
            result = get_links(page)
            if page.number == 0:
                for link, zoom in zip(result, invalid):
                    if link["kind"] == fitz.LINK_GOTO:
                        link["zoom"] = zoom
            return result

        def toc(document, simple=True):
            result = get_toc(document, simple)
            if not simple:
                for entry, zoom in zip(result, invalid):
                    entry[3]["zoom"] = zoom
            return result

        with fitz.open(self.source) as doc:
            for zoom in invalid:
                with self.subTest(zoom=zoom):
                    destination, reason, zoom_reason = compose._navigation_destination(
                        doc, {"kind": fitz.LINK_GOTO, "page": 0, "to": fitz.Point(30, 150),
                              "zoom": zoom}, compose._navigation_index([
                            compose.RegionPlacement(0, 0, "a", "source", "en", (0, 0, 400, 500),
                                                    (0, 0, 400, 500), (1, 0, 0, 1, 0, 0))]), "en")
                    self.assertEqual((destination["zoom"], reason, zoom_reason), (0, "", "invalid_zoom"))
        with patch.object(fitz.Page, "get_links", new=links), patch.object(fitz.Document, "get_toc", new=toc):
            stats, events = self.run_navigation(plans)
        self.assertEqual(stats["links_zoom_fallback"], 8)
        self.assertEqual(stats["toc_zoom_fallback"], 3)
        self.assertEqual(sum(e["count"] for e in events if e["reason"] == "invalid_zoom"), 11)
        with fitz.open(self.output) as doc:
            self.assertTrue(all(l.get("zoom", 0) == 0 for p in doc for l in p.get_links()))

    def test_unmapped_target_nearest_span_uses_language_boundary_not_span_edge(self):
        plans = self.make_navigation()
        for path in (self.source, self.mono):
            with fitz.open(path) as doc:
                # 模拟 Figure 6：落点在跨栏带下方的空白，离 span 比正文更近。
                doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(80, 300, 100, 320),
                                    "page": 1, "to": fitz.Point(200, 85)})
                doc.saveIncr()
        self.run_navigation(plans)
        with fitz.open(self.output) as doc:
            for side in ("en", "zh"):
                anchor = self.regions(doc, 0, side)[0]
                link = next(l for l in doc[0].get_links() if anchor.contains(l["from"])
                            and abs(l["from"].y0 - 300) < .01)
                self.assertEqual(link["page"], 1)
                self.assertEqual(link["to"].y, 0)
                boundary = min(r.x0 for r in self.regions(doc, 1, side))
                self.assertAlmostEqual(link["to"].x, boundary, places=3)
                self.assertEqual(link["to"].x > doc[1].rect.width / 2, side == "zh")

    def test_invalid_target_nearest_span_still_uses_chinese_page_top(self):
        placements = [
            compose.RegionPlacement(0, 0, "a", "mono", "span", (0, 40, 400, 80),
                                    (0, 40, 400, 80), (1, 0, 0, 1, 0, 0)),
            compose.RegionPlacement(0, 0, "a", "mono", "zh", (20, 100, 180, 400),
                                    (206, 100, 366, 400), (1, 0, 0, 1, 186, 0))]
        for point in (None, (float("nan"), 10)):
            self.assertEqual(compose._navigation_target(compose._navigation_index(placements), 0, point, "zh"),
                             (0, fitz.Point(206, 0), "target_unmapped"))

    def test_labels_cover_single_double_and_fallback_pages(self):
        stats, _ = self.run_navigation(self.make_navigation())
        with fitz.open(self.output) as doc:
            self.assertEqual([p.get_label() for p in doc], ["40", "41a", "41b", "42"])
            self.assertEqual(stats["labels"], 4)
            self.assertEqual(stats["toc_entries"], 3)
            self.assertEqual(stats["links_source"], 18)
            self.assertEqual(stats["links_written"], sum(len(p.get_links()) for p in doc))
            self.assertEqual(stats["links_unresolved"], 2)  # 英文跨栏、mono 页眉被有意省略。

    def decoded_label_prefixes(self, doc):
        # 直接读取输出数字树，交给 MuPDF 的 PDF 字符串解码器解析 /P。
        # 不经过 PyMuPDF 1.28.2 的 rule_dict / get_label：它们不解析转义，
        # 且 get_label 会移除标签中的括号，不能判定特殊字符写入是否正确。
        kind, nums = doc.xref_get_key(doc.pdf_catalog(), "PageLabels/Nums")
        self.assertEqual(kind, "array")
        entries = list(re.finditer(r"(\d+)\s*(?:(\d+)\s+0\s+R|<<\s*>>)", nums))
        self.assertEqual([int(m[1]) for m in entries], list(range(len(doc))))
        labels = []
        for entry in entries:
            if entry[2] is None:
                labels.append("")  # 失败页的空规则阻止继承上一页标签。
            else:
                kind, prefix = doc.xref_get_key(int(entry[2]), "P")
                self.assertEqual(kind, "string")
                labels.append(prefix)
        return labels

    def test_special_label_prefixes_roundtrip_through_source_and_composition(self):
        for prefix, encoded in SPECIAL_LABEL_PREFIX_CASES:
            with self.subTest(prefix=prefix, encoded=encoded):
                plans = self.make_special_label_case(encoded)
                stats, _ = self.run_navigation(plans)
                base = prefix or "2"  # 保留原有空标签回退到源页序号的行为。
                expected = ["第一页", base + "a", base + "b", "3"]
                with fitz.open(self.output) as doc:
                    self.assertEqual(self.decoded_label_prefixes(doc), expected)
                self.assertEqual(stats["labels"], 4)
                self.assertEqual(stats["labels_unresolved"], 0)

    def test_nested_label_tree_styles_and_start_offsets_roundtrip(self):
        for style, start, numbers in LABEL_STYLE_CASES:
            with self.subTest(style=style):
                plans = self.make_nested_label_case(style, start)
                stats, _ = self.run_navigation(plans)
                expected = ["S-" + numbers[0], "S-" + numbers[1] + "a",
                            "S-" + numbers[1] + "b", "end-1"]
                with fitz.open(self.output) as doc:
                    self.assertEqual(self.decoded_label_prefixes(doc), expected)
                self.assertEqual(stats["labels_unresolved"], 0)

    def test_invalid_source_label_rule_falls_back_only_affected_pages_and_logs(self):
        for rule in ("<</S /unknown /P (secret-label-and-path)>>",
                     "<</S /D /St 0>>", "<</P 7>>", "42"):
            with self.subTest(rule=rule):
                plans = self.make_navigation()
                with fitz.open(self.source) as doc:
                    doc.xref_set_key(doc.pdf_catalog(), "PageLabels",
                                     f"<</Nums [0 <</S /D /St 40>> 1 {rule} 2 <</P (last)>>]>>")
                    doc.saveIncr()
                stats, logs = self.run_navigation(plans)
                expected = ["40", "2a", "2b", "last"]
                with fitz.open(self.output) as doc:
                    self.assertEqual(self.decoded_label_prefixes(doc), expected)
                self.assertEqual(stats["labels"], 4)
                self.assertEqual(stats["labels_unresolved"], 2)
                self.assertEqual(stats["labels_read_fallback"], 2)
                failures = [e for e in logs if e["reason"] == "label_read_failed"]
                self.assertEqual([e["output_index"] for e in failures], [1, 2])
                self.assertTrue(all(e["source_index"] == 1 and e["count"] == 1
                                    and e["error_type"] == "ValueError" for e in failures))
                self.assertNotIn("secret-label-and-path", json.dumps(logs))

    def test_broken_source_label_tree_falls_back_with_reason_and_count(self):
        for tree in ("<</Nums [0]>>", "<</Nums [1 <</S /D>>]>>", "<</Kids []>>", "cycle"):
            with self.subTest(tree=tree):
                plans = self.make_navigation()
                with fitz.open(self.source) as doc:
                    if tree == "cycle":
                        xref = doc.get_new_xref()
                        doc.update_object(xref, f"<</Kids [{xref} 0 R]>>")
                        tree = f"{xref} 0 R"
                    doc.xref_set_key(doc.pdf_catalog(), "PageLabels", tree)
                    doc.saveIncr()
                stats, logs = self.run_navigation(plans)
                expected = ["1", "2a", "2b", "3"]
                with fitz.open(self.output) as doc:
                    self.assertEqual(self.decoded_label_prefixes(doc), expected)
                self.assertEqual(stats["labels_unresolved"], 4)
                self.assertEqual(stats["labels_read_fallback"], 4)
                failures = [e for e in logs if e["reason"] == "label_read_failed"]
                self.assertEqual([e["output_index"] for e in failures], list(range(4)))
                self.assertEqual(sum(e["count"] for e in failures), 4)

    def test_label_write_failure_only_blanks_that_page_and_counts_reason(self):
        plans = self.make_navigation()
        write = fitz.Document.xref_set_key
        for failed_page in range(4):
            with self.subTest(failed_page=failed_page):
                seen = []

                def fail_one(doc, xref, key, value):
                    result = write(doc, xref, key, value)
                    if key == "P":
                        seen.append(xref)
                        if len(seen) - 1 == failed_page:
                            # 包含部分写入后的失败；异常正文不得进入日志。
                            raise ValueError("secret-label-and-path")
                    return result

                with patch.object(fitz.Document, "xref_set_key", fail_one):
                    stats, logs = self.run_navigation(plans)
                expected = ["40", "41a", "41b", "42"]
                expected[failed_page] = ""
                with fitz.open(self.output) as doc:
                    self.assertEqual(self.decoded_label_prefixes(doc), expected)
                self.assertEqual(stats["labels"], 3)
                self.assertEqual(stats["labels_unresolved"], 1)
                failures = [log for log in logs if log["event"] == "split_navigation_unresolved"
                            and log["reason"] == "label_write_failed"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["count"], 1)
                self.assertEqual(failures[0]["output_index"], failed_page)
                self.assertEqual(failures[0]["source_index"], [0, 1, 1, 2][failed_page])
                self.assertEqual(failures[0]["error_type"], "ValueError")
                self.assertNotIn("secret-label-and-path", json.dumps(logs))

    def test_pdf_label_string_uses_literal_escapes_and_unicode_hex(self):
        cases = [("(i)", r"(\(i\))"), ("A\\1", r"(A\\1)"), ("", "()"),
                 ("\n\r\t\b\f\x00\x7f", r"(\012\015\011\010\014\000\177)"),
                 ("第一页", "<feff7b2c4e009875>"), ("é", "<feff00e9>"),
                 ("😀", "<feffd83dde00>")]
        for text, encoded in cases:
            with self.subTest(text=text):
                self.assertEqual(compose._pdf_label_string(text), encoded)

    def test_no_source_labels_use_source_sequence_and_no_toc_stays_empty(self):
        plans = self.make_navigation(labels=False)
        with fitz.open(self.source) as doc:
            doc.set_toc([])
            doc.saveIncr()
        stats, _ = self.run_navigation(plans)
        with fitz.open(self.output) as doc:
            self.assertEqual(self.decoded_label_prefixes(doc), ["1", "2a", "2b", "3"])
            self.assertEqual(doc.get_toc(), [])
        self.assertEqual(stats["labels_unresolved"], 0)

    def test_rotated_fallback_links_use_unrotated_input_then_placement(self):
        self.run_navigation(self.make_navigation(rotation=90))
        with fitz.open(self.output) as doc:
            links = doc[3].get_links()
            self.assertEqual(len(links), 2)
            self.assertEqual([l["page"] for l in links], [3, 3])
            self.assertAlmostEqual(links[0]["from"].x0, 360, places=3)
            self.assertAlmostEqual(links[0]["from"].y0, 30, places=3)
            self.assertAlmostEqual(links[0]["to"].x, 320, places=3)
            self.assertAlmostEqual(links[1]["to"].x, 820, places=3)

    def test_named_destination_resolves_coordinates_instead_of_pdf_named_dict_point(self):
        plans = self.make_navigation()
        with fitz.open(self.source) as doc:
            # PDF 的命名目标使用左下原点；1.28.2 get_links 的 named.to 未翻转 y。
            doc.xref_set_key(doc.pdf_catalog(), "Names",
                             f"<</Dests <</Names [(reference) [{doc.page_xref(1)} 0 R /XYZ 240 300 0]]>>>>")
            doc[0].insert_link({"kind": fitz.LINK_GOTO, "page": -1, "to": "reference",
                                "from": fitz.Rect(80, 300, 100, 320)})
            doc.saveIncr()
        self.run_navigation(plans)
        with fitz.open(self.output) as doc:
            box = self.regions(doc, 0, "en")[0]
            link = next(l for l in doc[0].get_links() if abs(l["from"].x0 - (box.x0 + 60)) < .01)
            self.assertEqual(link["page"], 2)
            self.assertAlmostEqual(link["to"].y, 200, places=3)

    def test_uri_with_parentheses_backslash_and_unicode_roundtrips(self):
        plans = self.make_navigation()
        uri = "https://example.org/路径/(a)\\tail?q=1"
        with fitz.open(self.source) as doc:
            page = doc[0]
            before = {x for x, _, _ in page.annot_xrefs()}
            page.insert_link({"kind": fitz.LINK_URI, "uri": "https://example.org/",
                              "from": fitz.Rect(80, 300, 100, 320)})
            added = next(x for x, _, _ in page.annot_xrefs() if x not in before)
            doc.xref_set_key(added, "A/URI", fitz.get_pdf_str(uri))
            doc.saveIncr()
        self.run_navigation(plans)
        with fitz.open(self.output) as doc:
            self.assertIn(uri, [l.get("uri") for l in doc[0].get_links()])

    def test_middle_span_compaction_applies_to_link_boxes_and_targets(self):
        for path in (self.source, self.mono):
            self.make_pdf(path, [(30, 150, "LEFT"), (230, 150, "RIGHT"), (230, 400, "LOWER"),
                                 (100, 270, "FIGURE")])
            with fitz.open(path) as doc:
                doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(230, 380, 250, 400),
                                    "page": 0, "to": fitz.Point(240, 400)})
                doc.saveIncr()
        plans = [PagePlan(0, 2, (Region("spanning", (0, 240, 400, 300), "middle"),
                                 Region("column", (20, 100, 180, 450), column=0),
                                 Region("column", (220, 100, 380, 450), column=1)))]
        self.run_compose(plans)
        with fitz.open(self.output) as doc:
            links = doc[1].get_links()
            self.assertEqual(len(links), 2)
            for link in links:
                self.assertEqual(link["page"], 1)
                self.assertAlmostEqual(link["from"].y0, 320, places=3)
                self.assertAlmostEqual(link["to"].y, 340, places=3)

    def test_labels_preserve_roman_prefix_and_source_order_when_plans_reorder(self):
        plans = self.make_navigation()
        with fitz.open(self.source) as doc:
            doc.set_page_labels([{"startpage": 0, "prefix": "S-", "style": "r", "firstpagenum": 4}])
            doc.saveIncr()
        self.run_navigation([plans[2], plans[1], plans[0]])
        with fitz.open(self.output) as doc:
            self.assertEqual([p.get_label() for p in doc], ["S-vi", "S-va", "S-vb", "S-iv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
