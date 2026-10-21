"""Unit regressions for link anchor extraction and annotation coordinates."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

import pymupdf


HELPER_PATH = Path(__file__).parents[1] / "addon" / "content" / "python" / "ztf_links.py"
SPEC = importlib.util.spec_from_file_location("ztf_links", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
links = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(links)


class LinkAnchorTest(unittest.TestCase):
    def test_anchor_kinds_cover_references_figures_tables_and_equations(self) -> None:
        self.assertEqual(
            links._anchor_from_text("[40]", "Earlier work [40].")["type"], "bracket"
        )
        self.assertEqual(links._anchor_from_text("[40]", "")["text"], "[40]")

        figure = links._anchor_from_text("6", "The detector in Fig. 6 reaches accuracy.")
        self.assertEqual(figure["type"], "number")
        self.assertEqual(figure["text"], "6")
        self.assertEqual(figure["label"], "figure")
        self.assertEqual(
            links._anchor_from_text("6", "We compare in Table 6 with baselines.")["label"],
            "table",
        )
        equation = links._anchor_from_text("(11)", "Therefore, (11) can be rewritten.")
        self.assertEqual(equation["type"], "paren")
        self.assertEqual(equation["text"], "(11)")
        self.assertEqual(
            links._anchor_from_text("1(d).", "shown in Fig. 1(d).")["text"], "1(d)"
        )

    def test_uri_anchor_keeps_bracketed_citation_text(self) -> None:
        anchor = links._anchor_from_uri("[48]", "https://example.org/code")
        self.assertEqual(anchor["type"], "bracket")
        self.assertEqual(anchor["text"], "[48]")

    def test_uri_anchor_uses_url_text_but_treats_orcid_icon_as_graphics(self) -> None:
        anchor = links._anchor_from_uri(
            "https://example.org/code", "https://example.org/code"
        )
        self.assertEqual(anchor["type"], "text")
        self.assertEqual(anchor["text"], "https://example.org/code")
        icon = links._anchor_from_uri(
            "n ,", "https://orcid.org/0000-0001-0000-0000"
        )
        self.assertEqual(icon["type"], "graphics")

    def test_duplicate_numbers_use_position_and_refuse_ambiguous_guesses(self) -> None:
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=500, height=700)
            page.insert_text(
                (60, 100), "See Fig. 6 for the first detector.",
                fontname="tiro", fontsize=11,
            )
            page.insert_text(
                (60, 300), "See Table 6 for the second result.",
                fontname="tiro", fontsize=11,
            )
            first, second = page.search_for("6")[:2]
            anchor = {"type": "number", "text": "6", "number": "6", "label": "figure"}
            region = pymupdf.Rect(0, 0, 500, 700)

            rect, reason = links._find_anchor_rect(
                page, anchor, region, pymupdf.Point(60, 95), None, None
            )
            self.assertEqual(reason, "")
            self.assertAlmostEqual(rect.x0, first.x0, delta=1)
            self.assertAlmostEqual(rect.y0, first.y0, delta=1)

            rect, reason = links._find_anchor_rect(
                page, anchor, region, pymupdf.Point(60, 295), None, None
            )
            self.assertEqual(reason, "")
            self.assertAlmostEqual(rect.x0, second.x0, delta=1)

            midpoint = pymupdf.Point(
                (first.x0 + first.x1 + second.x0 + second.x1) / 4,
                (first.y0 + first.y1 + second.y0 + second.y1) / 4,
            )
            rect, reason = links._find_anchor_rect(
                page, anchor, region, midpoint, None, None
            )
            self.assertIsNone(rect)
            self.assertEqual(reason, "anchor_ambiguous")
        finally:
            doc.close()

    def test_rotated_cropped_page_annotation_round_trips_to_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=300, height=400)
                page.insert_text((50, 80), "hello world", fontname="tiro", fontsize=10)
                page.set_cropbox(pymupdf.Rect(20, 30, 280, 380))
                page.set_rotation(90)
                page = doc[0]
                text_rect = page.search_for("hello")[0]
                xref = links._insert_link(
                    doc, page, text_rect,
                    {"type": "uri", "uri": "https://example.org/code"}, None, None,
                )
                self.assertGreater(xref, 0)
                saved = Path(directory) / "rotated.pdf"
                doc.save(saved)
            finally:
                doc.close()

            with pymupdf.open(saved) as reopened:
                page = reopened[0]
                found = [link for link in page.get_links()
                         if link.get("xref") == xref]
                self.assertEqual(len(found), 1)
                visible = pymupdf.Rect(found[0]["from"])
                unrotated = visible * page.derotation_matrix
                for actual, expected in zip(
                    (unrotated.x0, unrotated.y0, unrotated.x1, unrotated.y1),
                    (text_rect.x0, text_rect.y0, text_rect.x1, text_rect.y1),
                ):
                    self.assertAlmostEqual(actual, expected, delta=1.0)
                self.assertEqual(found[0]["uri"], "https://example.org/code")

    def test_formula_box_fallback_prefers_rendered_citation_area(self) -> None:
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=400, height=600)
            page.insert_text((50, 200), "translated sentence", fontname="tiro", fontsize=10)
            paragraph = {
                "output_text_bbox": [50, 190, 300, 210],
                "output_formula_boxes": [[200, 195, 208, 207]],
            }
            rect, mapped_by = links._fallback_anchor_rect(
                paragraph,
                pymupdf.Rect(200, 195, 208, 207),
                pymupdf.Point(202, 200),
                {"type": "number", "text": "6"},
                page,
            )
            self.assertEqual(mapped_by, "formula_box")
            self.assertEqual(tuple(rect), (200.0, 195.0, 208.0, 207.0))
        finally:
            doc.close()

    def test_geometry_fallback_records_that_it_is_not_text_based(self) -> None:
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=400, height=600)
            rect, mapped_by = links._fallback_anchor_rect(
                {"output_text_bbox": [50, 190, 300, 210], "output_formula_boxes": []},
                pymupdf.Rect(100, 195, 110, 207),
                pymupdf.Point(200, 200),
                {"type": "roman", "text": "II"},
                page,
            )
            self.assertEqual(mapped_by, "geometry")
            self.assertLessEqual(abs(rect.x0 - 195), 1)
        finally:
            doc.close()

    def test_source_link_rect_is_read_back_in_text_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rotated.pdf"
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=300, height=400)
                page.insert_text((50, 80), "hello world", fontname="tiro", fontsize=10)
                page.set_cropbox(pymupdf.Rect(20, 30, 280, 380))
                page.set_rotation(90)
                page = doc[0]
                text_rect = page.search_for("hello")[0]
                links._insert_link(
                    doc, page, text_rect,
                    {"type": "uri", "uri": "https://example.org/code"}, None, None,
                )
                doc.save(path)
            finally:
                doc.close()
            with pymupdf.open(path) as reopened:
                records = links._source_links(reopened, [])
            self.assertEqual(len(records), 1)
            actual = pymupdf.Rect(records[0]["source_from_unrotated"])
            for got, expected in zip(
                (actual.x0, actual.y0, actual.x1, actual.y1),
                (text_rect.x0, text_rect.y0, text_rect.x1, text_rect.y1),
            ):
                self.assertAlmostEqual(got, expected, delta=1.0)
            self.assertEqual(records[0]["anchor"]["text"], "hello")


class LinkDestinationTest(unittest.TestCase):
    def _named_fixture(self, directory: Path, bad: bool = False) -> Path:
        path = directory / "source.pdf"
        doc = pymupdf.open()
        try:
            doc.new_page(width=400, height=600)
            doc.new_page(width=400, height=600)
            first = doc[0]
            first.insert_text(
                (50, 100), "See [40] for the reference.",
                fontname="tiro", fontsize=12,
            )
            token = first.search_for("[40]")[0]
            page_xrefs = [doc.page_xref(0), doc.page_xref(1)]
            if bad:
                # No /Dests entry: the destination name cannot be resolved.
                first.insert_link({
                    "kind": pymupdf.LINK_NAMED, "from": token,
                    "nameddest": "missing.dest",
                })
            else:
                doc.xref_set_key(
                    doc.pdf_catalog(), "Dests",
                    "<< /bibr:ref40 [%d 0 R /XYZ 50 300 0] >>" % page_xrefs[1],
                )
                first.insert_link({
                    "kind": pymupdf.LINK_NAMED, "from": token,
                    "nameddest": "bibr:ref40",
                })
            doc.save(path)
        finally:
            doc.close()
        return path

    def test_named_destination_uses_resolved_top_left_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._named_fixture(Path(directory))
            doc = pymupdf.open(source)
            try:
                records = links._source_links(doc, [])
            finally:
                doc.close()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["destination"]["type"], "goto")
        self.assertEqual(records[0]["destination"]["page"], 1)
        self.assertEqual(records[0]["destination"]["point"], [50.0, 300.0])

    def test_missing_named_destination_is_reported_and_never_fabricated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = self._named_fixture(directory, bad=True)
            output = directory / "output.pdf"
            doc = pymupdf.open(source)
            try:
                doc.xref_set_key(doc[0].xref, "Annots", "null")
                doc.save(output)
            finally:
                doc.close()

            stats = links.restore_links(
                str(source), str(output), [], str(directory / "map.json")
            )
            self.assertEqual(stats["source"], 1)
            self.assertEqual(stats["restored"], 0)
            self.assertEqual(stats["unresolved"], 1)
            self.assertEqual(
                stats["unresolved_records"][0]["reason"],
                "named_destination_unresolved",
            )
            with pymupdf.open(output) as doc:
                self.assertEqual(doc[0].get_links(), [])


class TargetPointSearchTest(unittest.TestCase):
    def test_section_number_prefers_heading_near_approximate_point(self) -> None:
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=400, height=600)
            page.insert_text((50, 100), "3.4 Method", fontname="tiro", fontsize=12)
            page.insert_text((50, 320), "see 3.4 and 3.41", fontname="tiro", fontsize=10)
            page.insert_text((50, 520), "3.4 again", fontname="tiro", fontsize=10)
            destination = {"type": "goto", "page": 0, "point": [50.0, 100.0]}
            target_page, target_point, reason = links._target_point_for_link(
                destination, [], doc, {0: 0}, doc, anchor_text="3.4"
            )
            self.assertEqual(target_page, 0)
            self.assertEqual(reason, "target_anchor_search")
            self.assertGreaterEqual(target_point[1], 70.0)
            self.assertLessEqual(target_point[1], 130.0)
        finally:
            doc.close()

    def test_approximate_point_is_used_when_no_anchor_is_found(self) -> None:
        doc = pymupdf.open()
        try:
            doc.new_page(width=400, height=600)
            destination = {"type": "goto", "page": 0, "point": [100.0, 300.0]}
            target_page, target_point, reason = links._target_point_for_link(
                destination, [], doc, {0: 0}, doc, anchor_text="9.9"
            )
            self.assertEqual(target_page, 0)
            self.assertEqual(reason, "target_point_approximate")
            self.assertAlmostEqual(target_point[0], 100.0, delta=1.0)
            self.assertAlmostEqual(target_point[1], 300.0, delta=1.0)
        finally:
            doc.close()


class CompoundAnchorAndClusterTest(unittest.TestCase):
    def test_clipped_compound_number_is_recovered_from_line(self) -> None:
        for under in (".1", "3", "3.1"):
            with self.subTest(under=under):
                anchor = links._anchor_from_text(
                    under, "See Section 3.1 for details."
                )
                self.assertEqual(anchor["text"], "3.1", anchor)
                self.assertEqual(anchor["label"], "section")

    def test_superscript_and_equation_numbers_keep_full_text(self) -> None:
        self.assertEqual(
            links._anchor_from_text("10", "策略 10 展示了结果。")["text"], "10"
        )
        self.assertEqual(
            links._anchor_from_text("1", "见公式 1。")["text"], "1"
        )
        self.assertEqual(
            links._anchor_from_text("1(d)", "见 Fig. 1(d)。")["text"], "1(d)"
        )

    def test_citation_cluster_assigns_source_order(self) -> None:
        records = []
        for number in (4, 7, 18):
            records.append(
                {
                    "source_paragraph": {"id": 1},
                    "anchor": {"type": "number", "text": str(number)},
                    "source_line": "Earlier work [4, 7, 18] showed",
                    "source_from_unrotated": [number, 100, number + 3, 110],
                }
            )
        ordinals = links._assign_group_ordinals(records)
        self.assertEqual(
            [ordinals[index] for index in range(3)],
            [(0, 3), (1, 3), (2, 3)],
        )
        self.assertEqual(
            [record["_cluster"]["index"] for record in records], [0, 1, 2]
        )

    def test_cluster_rect_targets_the_nth_number(self) -> None:
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=400, height=200)
            page.insert_text((50, 100), "[4, 7, 18]", fontname="tiro", fontsize=12)
            region = pymupdf.Rect(0, 0, 400, 200)
            for index, number in enumerate((4, 7, 18)):
                cluster = {"numbers": [4, 7, 18], "index": index}
                rect = links._find_cluster_rect(page, region, cluster)
                self.assertIsNotNone(rect, cluster)
                self.assertEqual(page.get_textbox(rect).strip(), str(number))
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()