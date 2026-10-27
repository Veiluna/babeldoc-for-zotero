"""T3 公开接口回归；直接构造 Line，不依赖并行开发的 build_lines。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon/content/python"))
from ztf_split_types import Line
from ztf_split_margins import classify_margins, read_pdf_evidence


def line(page, index, text, baseline=20.0, box=None):
    """构造没有字体字段的冻结 Line。"""
    x0, y0, x1, y1 = box or (10.0, baseline - 8, 200.0, baseline + 2)
    return Line(page, index, (), index, index, x0, x1, y0, y1,
                baseline, text, ())


class ClassificationTests(unittest.TestCase):
    def test_repeated_normalizes_numbers_and_whitespace_at_one_third(self):
        pages = [[line(0, 7, " Journal\t2025   page 9 ")],
                 [line(1, 4, "Journal 2026 page 10")], [], [], [], []]
        self.assertEqual(classify_margins(pages, [800] * 6),
                         {0: {7: "repeated"}, 1: {4: "repeated"},
                          2: {}, 3: {}, 4: {}, 5: {}})

    def test_tagged_precedes_repeated_and_requires_full_containment(self):
        pages = [[line(p, 9, "Header")] for p in range(6)]
        pages[0].append(line(0, 12, "Body", 100))
        result = classify_margins(pages, [800] * 6,
                                  tagged={0: [(10, 12, 200, 22), (10, 92, 100, 102)]})
        self.assertEqual(result[0], {9: "tagged"})
        self.assertEqual(result[1], {9: "repeated"})

    def test_below_one_third_and_appended_only_do_not_classify(self):
        pages = [[line(0, 0, "Rare footer")], [line(1, 0, "Rare footer")],
                 [line(2, 0, "Only appended")], [], [], [], []]
        with self.assertLogs("ztf_split_margins", level="INFO") as captured:
            result = classify_margins(pages, [800] * 7,
                                      appended={0: [(0, 0, 300, 30)],
                                                2: [(0, 0, 300, 30)]})
        self.assertTrue(all(not found for found in result.values()))
        events = [json.loads(record.getMessage()) for record in captured.records]
        self.assertTrue(any(e["reason"] == "appended_only" for e in events))
        self.assertNotIn("Rare footer", str(events))

    def test_baselines_are_exact_and_occurrences_count_distinct_pages(self):
        pages = [[line(0, n, "Same") for n in range(5)],
                 [line(1, 1, "Same", 20.00001)], [], [], [], []]
        self.assertTrue(all(not found for found in
                            classify_margins(pages, [800] * 6).values()))

    def test_blank_and_single_page_are_not_repeated(self):
        self.assertEqual(classify_margins([], []), {})
        self.assertEqual(classify_margins([[line(0, 0, "Once"),
                                           line(0, 1, " \t")]], [800]), {0: {}})

    def test_invalid_coordinates_fail_without_logging_text(self):
        with self.assertRaises(ValueError):
            classify_margins([[line(0, 0, "secret", float("nan"))]], [800])
        with self.assertRaises(ValueError):
            classify_margins([[]], [])

    def test_different_fonts_with_same_text_and_baseline_repeat(self):
        document = pymupdf.open()
        try:
            pages = []
            for page_number, font in enumerate(("helv", "tiro", "cour")):
                page = document.new_page(width=600, height=800)
                page.insert_text((40, 30), "Same header", fontname=font, fontsize=10)
                item = page.get_text("dict")["blocks"][0]["lines"][0]
                span = item["spans"][0]
                pages.append([line(page_number, 0, span["text"],
                                   span["origin"][1], item["bbox"])])
            self.assertEqual(classify_margins(pages, [800] * 3),
                             {0: {0: "repeated"}, 1: {0: "repeated"}, 2: {0: "repeated"}})
        finally:
            document.close()


def fixture(name):
    """正文长度还原成每行唯一占位；候选原文与全部坐标保持原样。"""
    data = json.loads((Path(__file__).parent / "fixtures/split080_margins" /
                       (name + ".json")).read_text(encoding="utf-8"))
    pages = []
    for page, rows in enumerate(data["pages"]):
        lines = []
        for index, (x0, y0, x1, y1, baseline, payload) in enumerate(rows):
            text = payload
            # 占位文本的非数字编码避免数字归一造成原文并不存在的重复。
            if isinstance(payload, int):
                text = "<body:" + chr(0xE000 + page) + chr(0xE100 + index) + f":len={payload}>"
            lines.append(line(page, index, text, baseline, (x0, y0, x1, y1)))
        pages.append(lines)
    tagged = {int(p): boxes for p, boxes in data["tagged"].items()}
    appended = {int(p): boxes for p, boxes in data["appended"].items()}
    return data, pages, tagged, appended


class SampleFixtureTests(unittest.TestCase):
    def test_foba_and_chen_headers_page_numbers_and_watermarks(self):
        for name, page_count in (("foba", 19), ("chen2026", 18)):
            with self.subTest(sample=name):
                data, pages, tagged, appended = fixture(name)
                found = classify_margins(pages, data["heights"], tagged, appended)
                self.assertEqual([len(found[p]) for p in range(page_count)], [3] * page_count)
                for page, lines in enumerate(pages):
                    for item in lines:
                        if isinstance(data["pages"][page][item.index][-1], str):
                            self.assertIn(found[page].get(item.index), ("tagged", "repeated"))

    def test_foba_page_17_watermark_and_all_body_line_boxes(self):
        data, pages, tagged, appended = fixture("foba")
        found = classify_margins(pages, data["heights"], tagged, appended)
        watermark = [item for item in pages[16] if item.text.startswith("Authorized licensed use")]
        self.assertEqual(len(watermark), 1)
        self.assertEqual(found[16][watermark[0].index], "repeated")
        self.assertEqual(len(appended[16]), 1)
        body = [i for i, row in enumerate(data["pages"][16]) if isinstance(row[-1], int)]
        self.assertGreater(len(body), 20)
        self.assertFalse(set(body).intersection(found[16]))

    def test_p01_and_p05_remain_offline_coordinate_regressions(self):
        for name in ("P01", "P05"):
            with self.subTest(sample=name):
                data, pages, tagged, appended = fixture(name)
                result = classify_margins(pages, data["heights"], tagged, appended)
                self.assertEqual(len(result), len(pages))
                self.assertTrue(any(result.values()))


class PdfEvidenceTests(unittest.TestCase):
    def evidence(self, streams, properties=None):
        """合成独立内容流；临时文件 finally 清理，源文件须保持字节不变。"""
        # Windows 受限令牌不能进入 Python 3.12 以 mode=700 创建的临时目录；
        # 用临时文件仍有唯一名称与 finally 清理，不需要扩大沙箱权限。
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False,
                                         dir=Path(__file__).parent / "fixtures/split080_margins") as handle:
            path = Path(handle.name)
        try:
            document = pymupdf.open()
            try:
                page = document.new_page(width=600, height=800)
                page.insert_font(fontname="helv")
                refs = []
                for content in streams:
                    xref = document.get_new_xref()
                    document.update_object(xref, "<<>>")
                    document.update_stream(xref, content)
                    refs.append(f"{xref} 0 R")
                document.xref_set_key(page.xref, "Contents", "[" + " ".join(refs) + "]")
                if properties:
                    resource = int(document.xref_get_key(page.xref, "Resources")[1].split()[0])
                    document.xref_set_key(resource, "Properties", properties)
                document.save(path)
            finally:
                document.close()
            before = path.read_bytes()
            evidence = read_pdf_evidence(str(path))
            self.assertEqual(path.read_bytes(), before)
            return evidence
        finally:
            path.unlink()

    def test_tagged_nested_pagination_does_not_include_body(self):
        tagged, _ = self.evidence([
            b'BT /helv 10 Tf 1 0 0 1 40 700 Tm (Body) Tj ET\n'
            b'/Artifact <</Type /Pagination>> BDC\n'
            b'/Span BMC BT /helv 10 Tf 1 0 0 1 40 780 Tm (Header) Tj ET EMC EMC\n'
            b'BT /helv 10 Tf 1 0 0 1 40 600 Tm (After) Tj ET'])
        self.assertEqual(len(tagged[0]), 1)
        self.assertLess(tagged[0][0][3], 30)

    def test_named_property_and_subtypes_but_not_generic_artifact(self):
        tagged, _ = self.evidence([
            b'/Artifact /PaginationProperties BDC '
            b'BT /helv 10 Tf 40 780 Td (Header) Tj ET EMC\n'
            b'/Artifact <</Subtype /Footer>> BDC '
            b'BT /helv 10 Tf 40 20 Td (Footer) Tj ET EMC\n'
            b'/Artifact <</Subtype /Watermark>> BDC '
            b'BT /helv 10 Tf 200 400 Td (Watermark) Tj ET EMC\n'
            b'/Artifact <</Type /Layout>> BDC '
            b'BT /helv 10 Tf 40 600 Td (Layout artifact) Tj ET EMC\n'
            b'BT /helv 10 Tf 40 500 Td (Literal /Artifact BDC EMC) Tj ET'],
            '<</PaginationProperties <</Subtype /Header>>>>')
        self.assertEqual(len(tagged[0]), 3)

    def test_small_independent_tail_streams_are_only_auxiliary(self):
        tagged, appended = self.evidence([
            b'BT /helv 10 Tf 40 700 Td (Long body text, considerably longer than tails.) Tj '
            b'0 -20 Td (Second body line with more text.) Tj ET',
            b'BT /helv 7 Tf 40 20 Td (Stamp) Tj ET',
            b'BT /helv 7 Tf 40 10 Td (License) Tj ET'])
        self.assertFalse(any(tagged.values()))
        self.assertEqual(len(appended[0]), 2)
        self.assertTrue(all(box[1] > 770 for box in appended[0]))

    def test_continuation_and_single_stream_are_not_appended(self):
        for streams in ([b'BT /helv 10 Tf 40 700 Td (Body text) Tj ET'],
                        [b'BT /helv 10 Tf 40 700 Td (Body text) Tj',
                         b'0 -20 Td (Tail) Tj ET']):
            with self.subTest(streams=len(streams)):
                _, appended = self.evidence(streams)
                self.assertFalse(any(appended.values()))

    def test_cross_stream_tag_and_escaped_name_preserve_text_advance(self):
        tagged, _ = self.evidence([
            b'BT /helv 10 Tf 40 780 Td (abc) Tj % BDC EMC in comment\n'
            b'/Art#69fact <</Type /Pagination>> BDC',
            b'(Header) Tj EMC (After) Tj ET'])
        self.assertEqual(len(tagged[0]), 1)
        self.assertGreater(tagged[0][0][0], 50)
        self.assertLess(tagged[0][0][0], 60)

    def test_inline_image_skip_has_structured_reason(self):
        with self.assertLogs("ztf_split_margins", level="INFO") as captured:
            tagged, appended = self.evidence([
                b'q BI /W 1 /H 1 /CS /G /BPC 8 ID \xff EI Q'])
        self.assertFalse(tagged)
        self.assertFalse(appended)
        self.assertTrue(any(json.loads(record.getMessage())["reason"] ==
                            "inline_image_evidence_unsupported" for record in captured.records))


if __name__ == "__main__":
    unittest.main()
