"""0.8.1 figure/table background selection tests (red first)."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
import copy
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon/content/python"))

from ztf_split_types import SPANNING
import ztf_figure_background as background


@dataclass
class Box:
    x: float
    y: float
    x2: float
    y2: float


def box(*values):
    return Box(*values)


def char(marker, bounds):
    return NS(marker=marker, box=box(*bounds), char_unicode=marker,
              render_order=marker, sub_render_order=0, xobj_id=marker)


def paragraph(*chars):
    return NS(
        unicode="".join(c.char_unicode for c in chars),
        box=box(0, 0, 600, 800),
        pdf_paragraph_composition=[
            NS(pdf_character=None, pdf_line=NS(pdf_character=list(chars)),
               pdf_same_style_characters=None, pdf_formula=None)
        ],
        xobj_id=0,
    )


def page():
    return NS(
        page_number=0,
        mediabox=NS(box=box(0, 0, 600, 800)),
        page_layout=[NS(class_name="figure", box=box(100, 100, 300, 300)),
                     NS(class_name="table", box=box(350, 100, 500, 300)),
                     NS(class_name="text", box=box(0, 0, 600, 800))],
        pdf_curve=[], pdf_character=[], pdf_paragraph=[], pdf_figure=[],
        pdf_form=[], pdf_xobject=[], pdf_rectangle=[],
    )


class FigureBackgroundTests(unittest.TestCase):
    def test_removing_chart_form_keeps_parent_fonts_used_by_outside_body(self):
        p = page()
        body = char('body', (20, 20, 60, 40))
        body.xobj_id = 7
        p.pdf_paragraph = [paragraph(body)]
        p.pdf_paragraph[0].xobj_id = 7
        chart_form = NS(box=box(120, 120, 280, 280), xobj_id=7,
                        pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=74)))
        carrier = NS(box=chart_form.box, xobj_id=7, xref_id=73,
                     pdf_font=[NS(font_id='F49')])
        p.pdf_form = [chart_form]
        p.pdf_xobject = [carrier]
        background.remove_selected_objects(p, background.build_plan(p))
        self.assertEqual(p.pdf_form, [])
        self.assertEqual(p.pdf_xobject, [carrier])
        fonts = {f.font_id: f for x in p.pdf_xobject if x.xobj_id == body.xobj_id
                 for f in x.pdf_font}
        self.assertIn('F49', fonts)

    def test_curve_overlap_includes_inside_crossing_and_edge_but_not_outside(self):
        p = page()
        inside = NS(marker="inside", box=box(120, 120, 140, 140))
        crossing = NS(marker="cross", box=box(80, 180, 160, 180))
        touching = NS(marker="edge", box=box(300, 160, 330, 160))
        outside = NS(marker="outside", box=box(20, 20, 80, 80))
        formula_curve = NS(marker="formula", box=box(120, 120, 140, 140))
        p.pdf_curve = [inside, crossing, touching, outside]
        p.pdf_paragraph = [NS(pdf_paragraph_composition=[
            NS(pdf_formula=NS(pdf_curve=[formula_curve]), pdf_character=None,
               pdf_line=None, pdf_same_style_characters=None)
        ])]
        plan = background.build_plan(p)
        self.assertEqual([item.marker for item in plan.selected_curves],
                         ["inside", "cross", "edge"])
        self.assertNotIn(formula_curve, plan.selected_curves)

    def test_clip_regions_are_figure_union_crossing_curve_boxes(self):
        p = page()
        crossing = NS(marker="cross", box=box(80, 180, 330, 190))
        inside = NS(marker="inside", box=box(120, 120, 140, 140))
        p.pdf_curve = [crossing, inside]
        plan = background.build_plan(p)
        self.assertEqual(plan.clip_regions, (
            (100.0, 100.0, 300.0, 300.0),
            (350.0, 100.0, 500.0, 300.0),
            (80.0, 180.0, 330.0, 190.0),
        ))

    def test_other_curve_in_extra_region_is_removed_and_inherits_region_owner(self):
        p = page()
        p.page_layout = [NS(class_name="figure", box=box(100, 100, 280, 300))]
        crossing = NS(marker="cross", box=box(70, 180, 330, 200))
        extra = NS(marker="extra", box=box(290, 185, 320, 195))
        p.pdf_curve = [crossing, extra]
        plan = background.build_plan(p, gutters=((300, 300),))
        self.assertEqual([x.marker for x in plan.selected_curves], ["cross", "extra"])
        self.assertEqual(plan.owner(extra), SPANNING)
        background.remove_selected_objects(p, plan)
        self.assertEqual(p.pdf_curve, [])
        self.assertEqual(plan.clip_regions[-1], (70., 180., 330., 200.))

    def test_extra_area_text_is_kept_in_il_while_figure_text_is_selected(self):
        p = page()
        inside = char("A", (120, 120, 130, 130))
        extra = char("B", (80, 180, 330, 190))
        outside = char("C", (20, 20, 30, 30))
        p.pdf_character = [inside, extra, outside]
        p.pdf_paragraph = [paragraph(inside, extra, outside)]
        plan = background.build_plan(p)
        self.assertEqual([c.marker for c in plan.selected_characters], ["A"])
        background.remove_selected_objects(p, plan)
        self.assertEqual([c.marker for c in p.pdf_character], ["B", "C"])
        remaining = [c.marker for para in p.pdf_paragraph
                     for comp in para.pdf_paragraph_composition
                     for c in (comp.pdf_line.pdf_character if comp.pdf_line else [])]
        self.assertEqual(remaining, ["B", "C"])

    def test_selected_objects_are_removed_but_other_objects_are_unchanged(self):
        p = page()
        selected_curve = NS(marker="curve", box=box(120, 120, 140, 140))
        other_curve = NS(marker="other", box=box(20, 20, 40, 40))
        # Form 的透明留白可略超出 layout 框，不能因此留在 IL 再覆盖底图。
        selected_form = NS(marker="form", box=box(95, 150, 180, 180), xobj_id=7)
        other_form = NS(marker="other-form", box=box(20, 20, 40, 40), xobj_id=8)
        carrier = NS(marker="page-carrier", box=box(0, 0, 600, 800), xobj_id=9)
        enclosing_chart = NS(marker="chart-form", box=box(95, 95, 305, 305), xobj_id=10)
        p.pdf_curve = [selected_curve, other_curve]
        p.pdf_form = [selected_form, other_form, carrier, enclosing_chart]
        p.pdf_xobject = [NS(marker="x7", xobj_id=7, box=selected_form.box),
                         NS(marker="x8", xobj_id=8, box=other_form.box)]
        before = copy.deepcopy(p)
        plan = background.build_plan(p)
        background.remove_selected_objects(p, plan)
        self.assertEqual([x.marker for x in p.pdf_curve], ["other"])
        self.assertEqual([x.marker for x in p.pdf_form], ["other-form", "page-carrier"])
        self.assertEqual([x.marker for x in p.pdf_xobject], ["x8"])
        self.assertEqual(other_curve.marker, before.pdf_curve[1].marker)

    def test_owner_left_right_and_spanning(self):
        p = page()
        p.page_layout = [NS(class_name="figure", box=box(40, 100, 260, 300)),
                         NS(class_name="figure", box=box(340, 100, 560, 300))]
        left = NS(marker="left", box=box(50, 120, 100, 140))
        spanning = NS(marker="span", box=box(250, 180, 350, 190))
        right = NS(marker="right", box=box(400, 120, 450, 140))
        p.pdf_curve = [left, spanning, right]
        plan = background.build_plan(p, gutters=((300, 300),))
        self.assertEqual(plan.owner(left), 0)
        self.assertEqual(plan.owner(right), 1)
        self.assertEqual(plan.owner(spanning), SPANNING)
        self.assertEqual([x.marker for x in plan.for_owner(0).selected_curves], ["left"])
        self.assertEqual([x.marker for x in plan.for_owner(SPANNING).selected_curves], ["span"])

    def test_imported_background_is_vector_and_does_not_restore_outside_text(self):
        import pymupdf

        source = pymupdf.open()
        output = pymupdf.open()
        try:
            source_page = source.new_page(width=200, height=200)
            source_page.draw_rect(pymupdf.Rect(40, 20, 160, 80), fill=(1, 0, 0))
            source_page.insert_text((60, 70), "FIGURE_TEXT")
            source_page.insert_text((10, 20), "OUTSIDE_TEXT")
            output_page = output.new_page(width=200, height=200)
            output_page.insert_text((10, 190), "TRANSLATED")
            source_il = page()
            # 非上下对称的上半部框：把顶左 y 直接写成 PDF y 会整块消失。
            source_il.page_layout = [NS(class_name="figure", box=box(40, 20, 160, 80))]
            plan = background.build_plan(source_il)
            self.assertTrue(background.prepend_background_form(output_page, source, 0, plan))
            text = output_page.get_text()
            self.assertIn("FIGURE_TEXT", text)
            self.assertIn("TRANSLATED", text)
            self.assertNotIn("OUTSIDE_TEXT", text)
            self.assertGreater(len(output_page.get_contents()), 1)
            pixmap = output_page.get_pixmap(alpha=False)
            self.assertEqual(pixmap.pixel(50, 50), (255, 0, 0))
            self.assertEqual(pixmap.pixel(50, 150), (255, 255, 255))
        finally:
            source.close()
            output.close()

    def test_owner_uses_converted_boxes_and_keeps_text_only_chart(self):
        p = page()
        p.page_layout = [NS(class_name='figure', box=box(40, 600, 560, 750))]
        c = char('label', (60, 650, 100, 660))
        p.pdf_character = [c]
        edge_curve = NS(marker='edge-label', box=box(30, 650, 50, 660))
        p.pdf_curve = [edge_curve]
        plan = background.build_plan(
            p, gutters=((300,300),),
            convert_box=lambda b:(b.x,800-b.y2,b.x2,800-b.y))
        self.assertEqual(plan.owner(c), SPANNING)
        self.assertEqual(plan.owner(edge_curve), SPANNING)
        self.assertEqual(plan.for_owner(SPANNING).clip_regions,
                         ((40.,50.,560.,200.), (30.,140.,50.,150.)))
        self.assertEqual(plan.for_owner(0).clip_regions, ())
        self.assertEqual(plan.for_owner(1).clip_regions, ())
        self.assertEqual(plan.for_owner(0).excluded_figure_boxes, ((40.,50.,560.,200.),))
        self.assertEqual(plan.for_owner(SPANNING).excluded_figure_boxes, ())

    def test_shared_nested_form_is_preserved_when_other_instance_is_outside(self):
        import pymupdf

        with pymupdf.open() as symbol, pymupdf.open() as source, pymupdf.open() as output:
            stamp = symbol.new_page(width=80, height=80)
            stamp.draw_rect((10, 10, 70, 70), fill=(1, .5, .5), fill_opacity=.5)
            original = source.new_page(width=240, height=120)
            original.show_pdf_page(pymupdf.Rect(0, 0, 80, 80), symbol, 0)
            original.show_pdf_page(pymupdf.Rect(140, 0, 220, 80), symbol, 0)
            target = output.new_page(width=240, height=120)
            plan = background.BackgroundPlan(((0., 0., 80., 80.),),
                                              clip_regions=((0., 0., 80., 80.),))
            background.prepend_background_form(target, source, 0, plan)
            before = original.get_pixmap(clip=(0, 0, 80, 80), alpha=False)
            after = target.get_pixmap(clip=(0, 0, 80, 80), alpha=False)
            self.assertEqual(before.samples, after.samples)
            self.assertEqual(target.get_pixmap(alpha=False).pixel(170, 30), (255, 255, 255))

    def test_removing_chart_chars_keeps_paragraph_indices_and_no_chart_is_noop(self):
        p = page()
        chart = paragraph(char('label', (120,120,140,140)))
        body = paragraph(char('body', (20,20,40,40)))
        empty = NS(unicode='metadata',pdf_paragraph_composition=[])
        p.pdf_paragraph = [chart,body,empty]
        background.remove_selected_objects(p, background.build_plan(p))
        self.assertEqual(len(p.pdf_paragraph),3)
        self.assertIs(p.pdf_paragraph[1],body)
        self.assertEqual(chart.unicode,'')
        self.assertEqual(empty.unicode,'metadata')
        p.page_layout = []
        p.pdf_paragraph = [empty,body]
        background.remove_selected_objects(p,background.build_plan(p))
        self.assertEqual(p.pdf_paragraph,[empty,body])

    def test_no_chart_leaves_mono_variants_and_dual_pdf_bytes_unchanged(self):
        import pymupdf

        directory = Path(__file__).resolve().parents[1] / 'build/t081/b' / f'no-chart-{time.time_ns()}'
        directory.mkdir()
        source = directory / 'source.pdf'
        with pymupdf.open() as doc:
            p = doc.new_page(width=240, height=160)
            p.insert_text((20, 30), 'BODY text')
            p.draw_rect((20, 50, 180, 100), fill=(.5, .8, 1))
            doc.save(source)
        plan = background.BackgroundPlan(())
        original = source.read_bytes()
        for kind in ('mono', 'variants', 'dual'):
            target = directory / (kind + '.pdf')
            target.write_bytes(original)
            self.assertEqual(background.inject_backgrounds(str(target), str(source), {0: plan}), 0)
            self.assertEqual(background.inject_background_sequence(str(target), str(source), [plan], [0]), 0)
            self.assertEqual(target.read_bytes(), original)

    def test_background_injection_keeps_embedded_cjk_font_subset_small(self):
        import pymupdf

        directory = Path(__file__).resolve().parents[1] / 'build/t081/b' / f'font-subset-{time.time_ns()}'
        directory.mkdir()
        source, target = directory/'source.pdf', directory/'mono.pdf'
        with pymupdf.open() as doc:
            p = doc.new_page(width=200, height=200)
            p.draw_rect((20, 20, 180, 80), fill=(1, 0, 0))
            p.insert_text((30, 60), 'SOURCE FIGURE')
            doc.save(source)
        with pymupdf.open() as doc:
            p = doc.new_page(width=200, height=200)
            font = pymupdf.Font('cjk')
            p.insert_font(fontname='ZTFEmbeddedCJK', fontbuffer=font.buffer)
            p.insert_text((20, 150), '中文字体子集', fontname='ZTFEmbeddedCJK')
            full_font_bytes = len(doc.extract_font(p.get_fonts()[0][0])[3])
            self.assertGreater(full_font_bytes, 1_000_000)
            # 与 PDFCreater.write 相同：先 subset，再保存交给底图注入。
            doc.subset_fonts(fallback=False)
            doc.save(target, garbage=3, deflate=True)

        def snapshot(path):
            with pymupdf.open(path) as doc:
                fonts = {f[0] for p in doc for f in p.get_fonts(full=True)}
                font_bytes = sum(len(doc.extract_font(xref)[3]) for xref in fonts)
                body = doc[0].get_pixmap(clip=pymupdf.Rect(0, 100, 200, 200), alpha=False).samples
                return font_bytes, doc[0].get_text(), body

        before_bytes = target.stat().st_size
        before_fonts, before_text, before_pixels = snapshot(target)
        self.assertLess(before_fonts, full_font_bytes/5)
        plan = background.BackgroundPlan(((20.,20.,180.,80.),),
                                         clip_regions=((20.,20.,180.,80.),))
        self.assertEqual(background.inject_backgrounds(str(target), str(source), {0:plan}), 1)
        after_fonts, after_text, after_pixels = snapshot(target)
        self.assertLessEqual(after_fonts, before_fonts+1024)
        self.assertLess(target.stat().st_size, before_bytes+20_000)
        self.assertIn(before_text.strip(), after_text)
        self.assertIn('SOURCE FIGURE', after_text)
        self.assertEqual(before_pixels, after_pixels)

    def test_source_overlay_cannot_cover_spanning_chart_or_remove_outside_body(self):
        import pymupdf

        with pymupdf.open() as overlay, pymupdf.open() as target:
            p = overlay.new_page(width=200, height=200)
            p.draw_rect((0, 0, 200, 130), fill=(1, 1, 1))
            p.insert_text((20, 180), 'BODY')
            out = target.new_page(width=200, height=200)
            out.draw_rect((40, 20, 160, 80), fill=(1, 0, 0))
            background.clip_source_layer_outside_figures(p, ((40., 20., 160., 80.),))
            out.show_pdf_page(out.rect, overlay, 0)
            pix = out.get_pixmap(alpha=False)
            self.assertEqual(pix.pixel(50, 50), (255, 0, 0))
            self.assertIn('BODY', out.get_text())

    def test_compose_guard_installs_once_and_restores_layer_after_failure(self):
        original_layer = lambda *_: None
        helper = NS(_layer=original_layer)

        def fail(*args, **kwargs):
            self.assertIsNot(helper._layer, original_layer)
            raise RuntimeError('synthetic failure')

        helper.compose_split_document = fail
        background.install_source_layer_guard(helper)
        installed = helper.compose_split_document
        background.install_source_layer_guard(helper)
        self.assertIs(helper.compose_split_document, installed)
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            installed('source.pdf', 'nonexistent-figure-background-test-mono.pdf',
                      {'pages': []}, 'output.pdf')
        self.assertIs(helper._layer, original_layer)


if __name__ == "__main__":
    unittest.main()
