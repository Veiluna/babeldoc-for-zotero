"""T4：按源段落归属生成独立 IL 页，保留字形位置与原单语页。"""

import copy
import json
import os
import subprocess
import sys
import threading
import time
import unicodedata
import unittest
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon/content/python"))

from ztf_split_types import MARGIN, SPANNING, PageColumns
from ztf_split_variants import collect_source_chars, create_variants
import ztf_split_variants as split_variants


def box(x, y, x2, y2):
    return NS(x=x, y=y, x2=x2, y2=y2)


def paragraph(text, bounds):
    return NS(unicode=text, box=box(*bounds), pdf_paragraph_composition=[
        NS(pdf_character=NS(char_unicode=text, box=box(*bounds), xobj_id=0),
           pdf_formula=None)], xobj_id=0)


def page():
    return NS(page_number=0, mediabox=NS(box=box(0, 0, 600, 800)),
              cropbox=NS(box=box(0, 0, 600, 800)),
              pdf_paragraph=[paragraph("左栏译文", (40, 600, 280, 700)),
                             paragraph("右栏译文", (320, 600, 560, 700)),
                             paragraph("跨栏标题", (40, 730, 560, 760))],
              pdf_character=[], pdf_curve=[], pdf_form=[], pdf_rectangle=[],
              pdf_figure=[], pdf_xobject=[], page_layout=[])


def columns():
    return PageColumns(0, 2, "jump", ((300, 300),), (),
                       ((0, 0), (1, 1), (2, SPANNING)))


class VariantTests(unittest.TestCase):
    def test_foba_page17_watermark_is_margin_and_absent_from_chinese_variants(self):
        import gzip
        from ztf_split_lines import build_lines
        from ztf_split_margins import classify_margins
        from ztf_split_columns import detect_columns
        from ztf_split_types import DocumentColumns

        fixture = Path(__file__).parent / 'fixtures/foba-watermark.json.gz'
        records = json.loads(gzip.decompress(fixture.read_bytes()))['pages']
        saved = {}

        class Finder:
            def process_page(self, original):
                # Replay measured paragraph membership after BabelDOC spacing:
                # the real spaces with paragraph=-1 disappeared from the IL.
                glyphs = original.pdf_character
                original.pdf_character = []
                for c in glyphs:
                    if c.expected_paragraph >= 0:
                        original.pdf_paragraph[c.expected_paragraph].pdf_paragraph_composition.append(
                            NS(pdf_character=c, pdf_formula=None))

        split_variants._install_paragraph_capture(Finder, saved)
        pages, lines = [], []
        for record in records:
            original = page()
            original.page_number = record['page']
            original.pdf_paragraph = [NS(pdf_paragraph_composition=[]) for _ in range(
                1 + max(c[8] for c in record['chars']))]
            original.pdf_character = [NS(render_order=c[1], sub_render_order=c[2],
                box=box(*c[3:7]), char_unicode=c[7], xobj_id=c[9], expected_paragraph=c[8])
                for c in record['chars']]
            Finder().process_page(original)
            convert = lambda b:(b.x,b.y,b.x2,b.y2)
            chars = collect_source_chars(original, convert, saved.pop(id(original))[1])
            lines.append(build_lines(record['page'], chars))
            pages.append(original)
        margins = classify_margins(lines, [792,792])
        self.assertEqual(len(lines[1]), 1)
        self.assertIn('UTC from IEEE', lines[1][0].text)
        self.assertIn(lines[1][0].index, margins[1])
        assigned = detect_columns(16,612,792,lines[1],margins[1],DocumentColumns(2,'jump'))
        self.assertEqual(assigned.line_column, (MARGIN,))
        self.assertTrue(all(not v.paragraphs for v in create_variants(pages[1],assigned)))
        self.assertFalse(any(c.char_unicode.isspace() and c.expected_paragraph == -1
            for p in pages[1].pdf_paragraph for c in split_variants._paragraph_chars(p)))

        # BabelDOC may share a protected paragraph between a unique copyright
        # line and the repeated watermark. Keep its non-margin source line.
        original = page()
        footer = NS(render_order=8,sub_render_order=0,xobj_id=0,
                    box=box(10,20,100,30),char_unicode='watermark')
        copyright = NS(render_order=7,sub_render_order=0,xobj_id=0,
                       box=box(10,40,100,50),char_unicode='copyright')
        original.pdf_paragraph[2].pdf_paragraph_composition = [
            NS(pdf_character=None,pdf_formula=None,pdf_line=NS(pdf_character=[copyright,footer]))]
        variants = create_variants(original,columns(),{(8,0,0):MARGIN})
        self.assertEqual([c.char_unicode for c in split_variants._paragraph_chars(
            variants[-1].page.pdf_paragraph[0])],['copyright'])
        self.assertEqual(list(split_variants._paragraph_chars(original.pdf_paragraph[2])),[copyright,footer])

    def test_column_count_is_read_from_structural_attributes(self):
        import pymupdf
        for count in (1, 2):
            with self.subTest(count=count), pymupdf.open() as pdf:
                pdf.new_page()
                child = pdf.get_new_xref()
                pdf.update_object(child, f"<< /Type /StructElem /S /Document "
                                        f"/A << /O /Layout /ColumnCount {count} >> >>")
                pdf.xref_set_key(pdf.pdf_catalog(), "StructTreeRoot", f"<< /K [{child} 0 R] >>")
                self.assertEqual(split_variants._column_count_tag(pdf), count)
        with pymupdf.open() as pdf:
            pdf.new_page()
            self.assertIsNone(split_variants._column_count_tag(pdf))

    def test_lazy_graphics_history_is_flattened_without_touching_source_caches(self):
        from babeldoc.format.pdf.document_il.frontend.il_creater_active_support import (
            LazyPassthroughInstruction, PassthroughSnapshot,
            replace_first_passthrough_operator,
        )
        snapshot = PassthroughSnapshot()
        for i in range(600):
            snapshot = replace_first_passthrough_operator(snapshot, "w", ("w", i + 1))
        lazy = LazyPassthroughInstruction(snapshot, suffix_parts=("0 0 1 rg",))
        original = page()
        original.pdf_curve = [NS(box=box(40, 300, 280, 300), xobj_id=0,
                                 graphic_state=NS(passthrough_per_char_instruction=lazy))]
        limit = sys.getrecursionlimit()
        with self.assertRaises(RecursionError):
            copy.deepcopy(original)
        variants = create_variants(original, columns())
        copied = variants[0].page.pdf_curve[0].graphic_state.passthrough_per_char_instruction
        self.assertIs(type(copied), str)
        self.assertEqual(copied, "600 w 0 0 1 rg")
        self.assertIs(original.pdf_curve[0].graphic_state.passthrough_per_char_instruction, lazy)
        self.assertIsNone(lazy._value)
        self.assertEqual(snapshot._render_cache, {})
        self.assertEqual(sys.getrecursionlimit(), limit)

    def test_all_margin_paragraph_is_absent_from_every_variant(self):
        original = page()
        assigned = PageColumns(0, 2, "tag", ((300, 300),), (), ((1, 1), (2, SPANNING)))
        variants = create_variants(original, assigned)
        self.assertFalse(any(0 in variant.paragraphs for variant in variants))

    def test_copy_boundary_preserves_clipping_suffix_shared_lazy_and_mutable_isolation(self):
        from babeldoc.format.pdf.document_il.frontend.il_creater_active_support import (
            LazyPassthroughInstruction, PassthroughSnapshot,
        )
        snapshot = PassthroughSnapshot()
        snapshot._render_cache[True] = "0 0 50 50 re W n"
        lazy = LazyPassthroughInstruction(snapshot, include_clipping=True, suffix_parts=("1 g",))
        original = NS(first=lazy, second=lazy, mutable=[1])
        cloned = split_variants._copy_il(original)
        self.assertEqual(cloned.first, "0 0 50 50 re W n 1 g")
        self.assertIs(cloned.first, cloned.second)
        cloned.mutable.append(2)
        self.assertEqual(original.mutable, [1])
        self.assertIsNone(lazy._value)
        self.assertEqual(snapshot._render_cache, {True: "0 0 50 50 re W n"})

    def test_each_variant_contains_only_its_paragraphs_without_moving_text(self):
        original = page()
        before = copy.deepcopy(original)
        variants = create_variants(original, columns())
        self.assertEqual([v.column for v in variants], [0, 1, SPANNING])
        self.assertEqual([[p.unicode for p in v.page.pdf_paragraph] for v in variants],
                         [["左栏译文"], ["右栏译文"], ["跨栏标题"]])
        self.assertEqual([v.paragraphs for v in variants], [(0,), (1,), (2,)])
        for variant, index in zip(variants, (0, 1, 2)):
            self.assertEqual(variant.page.pdf_paragraph[0], before.pdf_paragraph[index])
            self.assertEqual(variant.page.cropbox, original.cropbox)
        variants[0].page.pdf_paragraph[0].box.y2 = 0
        self.assertEqual(original, before)

    def test_nontext_and_free_characters_follow_regions_and_margin_ownership(self):
        original = page()
        original.page_layout = [NS(class_name="figure", box=box(40, 100, 560, 200))]
        original.pdf_curve = [NS(box=box(40, 300, 280, 300), xobj_id=0),
                              NS(box=box(320, 300, 560, 300), xobj_id=0),
                              NS(box=box(40, 100, 80, 120), xobj_id=0)]
        original.pdf_form = [NS(box=box(320, 400, 560, 500), xobj_id=0,
                                pdf_form_subtype=NS(pdf_xobj_form=None))]
        original.pdf_character = [NS(box=box(40, 740, 50, 750), xobj_id=0,
                                     render_order=99, sub_render_order=0),
                                  NS(box=box(40, 150, 50, 160), xobj_id=0,
                                     render_order=100, sub_render_order=0)]
        variants = create_variants(original, columns(), {(99, 0, 0): MARGIN})
        self.assertEqual([len(v.page.pdf_curve) for v in variants], [1, 1, 1])
        self.assertEqual([len(v.page.pdf_form) for v in variants], [0, 1, 0])
        self.assertEqual([len(v.page.pdf_character) for v in variants], [0, 0, 1])

    def test_single_column_has_no_variants(self):
        original = page()
        single = PageColumns(0, 1, "single", (), (), ((0, 0),))
        self.assertEqual(create_variants(original, single), ())

    def test_carrier_form_is_kept_in_each_variant_with_selected_descendants(self):
        original = page()
        for item in original.pdf_paragraph:
            item.xobj_id = 7
            item.pdf_paragraph_composition[0].pdf_character.xobj_id = 7
        original.pdf_xobject = [NS(xobj_id=7, xref_id=42, box=box(0, 0, 600, 800))]
        original.pdf_form = [NS(box=box(0, 0, 600, 800), xobj_id=0, render_order=1,
                                pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=42)))]
        variants = create_variants(original, columns())
        self.assertEqual([len(v.page.pdf_form) for v in variants], [1, 1, 1])
        self.assertEqual([len(v.page.pdf_paragraph) for v in variants], [1, 1, 1])

    def test_shading_without_bounds_follows_its_carrier_not_the_zero_placeholder(self):
        # 本机 BabelDOC 的 shading 用零框占位；正常图片/曲线的 box 已是页面坐标。
        original = page()
        original.pdf_xobject = [NS(xobj_id=9, xref_id=18, box=box(320, 300, 560, 470))]
        original.pdf_form = [
            NS(box=box(320, 300, 560, 470), xobj_id=0, render_order=1,
               pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=18))),
            NS(box=box(330, 320, 360, 350), xobj_id=9,
               pdf_form_subtype=NS(pdf_xobj_form=None)),
        ]
        original.pdf_curve = [NS(box=box(0, 0, 0, 0), xobj_id=9, passthrough_paint=True)]
        variants = create_variants(original, columns())
        self.assertEqual([len(v.page.pdf_form) for v in variants], [0, 2, 0])
        self.assertEqual([len(v.page.pdf_curve) for v in variants], [0, 1, 0])

    def test_repeated_form_xref_keeps_distinct_invocations_and_only_active_streams(self):
        original = page()
        original.pdf_paragraph = []
        bounds = [(40, 300, 100, 400), (330, 300, 390, 400), (430, 300, 490, 400)]
        original.pdf_xobject = [NS(xobj_id=i, xref_id=18, box=box(*b))
                                for i, b in zip((1, 3, 5), bounds)]
        original.pdf_form = [NS(box=box(*b), xobj_id=0, render_order=i,
                                pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=18)))
                             for i, b in enumerate(bounds)]
        original.pdf_curve = [NS(box=box(b[0] + 5, 310, b[2] - 5, 390), xobj_id=i)
                              for i, b in zip((1, 3, 5), bounds)]
        variants = create_variants(original, columns())
        self.assertEqual([[f.box.x for f in v.page.pdf_form] for v in variants],
                         [[40], [330, 430], []])
        self.assertEqual([[x.xobj_id for x in v.page.pdf_xobject] for v in variants],
                         [[1], [3, 5], []])

    def test_nested_and_spanning_carriers_do_not_turn_unknown_bounds_into_left_column(self):
        original = page()
        original.pdf_paragraph = []
        original.pdf_xobject = [NS(xobj_id=7, xref_id=42, box=box(0, 0, 600, 800)),
                                NS(xobj_id=9, xref_id=18, box=box(330, 300, 560, 470))]
        original.pdf_form = [
            NS(box=box(0, 0, 600, 800), xobj_id=0, render_order=1,
               pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=42))),
            NS(box=box(330, 300, 560, 470), xobj_id=7, render_order=2,
               pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=18))),
        ]
        original.pdf_curve = [NS(box=box(0, 0, 0, 0), xobj_id=i, passthrough_paint=True)
                              for i in (0, 7, 9)]
        original.pdf_curve.append(NS(box=box(40, 300, 100, 400), xobj_id=7))
        variants = create_variants(original, columns())
        self.assertEqual([[c.xobj_id for c in v.page.pdf_curve] for v in variants],
                         [[7], [9], [0, 7]])
        self.assertEqual([len(v.page.pdf_form) for v in variants], [1, 2, 1])

    def test_repeated_form_streams_render_distinct_subsets_at_original_positions(self):
        import pymupdf
        from babeldoc.format.pdf.document_il.utils.zstd_helper import zstd_decompress

        cases = [(in_formula, indirect, scale, dx, dy)
                 for in_formula in (False, True) for indirect in (False, True)
                 for scale, dx, dy in ((1, 0, 0), (1, 10, 7), (1.5, 10, 7))]
        for in_formula, indirect, scale, dx, dy in cases:
            with self.subTest(in_formula=in_formula, indirect_resources=indirect,
                              relocation=(scale, dx, dy)), pymupdf.open() as pdf:
                pdf_page = pdf.new_page(width=600, height=800)

                def object_xref(value, stream=None):
                    xref = pdf.get_new_xref()
                    pdf.update_object(xref, value)
                    if stream is not None:
                        pdf.update_stream(xref, stream)
                    return xref

                source_stream = b"0 0 5 5 re f 10 10 5 5 re f"
                source = object_xref("<< /Type /XObject /Subtype /Form /BBox [0 0 20 20] "
                                     "/Matrix [0 1 -1 0 0 0] /Resources <<>> "
                                     "/PTEX.FileName (example file.pdf) >>", source_stream)
                resources = f"<< /XObject << /Fm {source} 0 R >> >>"
                if indirect:
                    names = object_xref(f"<< /Fm {source} 0 R >>")
                    resources = f"{object_xref(f'<< /XObject {names} 0 R >>')} 0 R"
                pdf.xref_set_key(pdf_page.xref, "Resources", resources)
                original = page()
                original.pdf_paragraph = []
                bounds = [(80, 300, 100, 320), (380, 300, 400, 320)]
                original.pdf_xobject = [NS(xobj_id=i, xref_id=source, box=box(*b),
                                           base_operations=NS(value="stale shared inverse"))
                                        for i, b in zip((1, 3), bounds)]
                original.pdf_form = [NS(box=box(*b), xobj_id=0, ctm=[1, 0, 0, 1, x, 300], render_order=i,
                                        pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=source, do_args="Fm")))
                                     for i, (x, b) in enumerate(zip((100, 400), bounds))]
                forms = original.pdf_form
                relocation = pymupdf.Matrix(scale, 0, 0, scale, dx, dy)
                for form in forms:
                    # BabelDOC 公式搬移改变 box，保留原 ctm/render_order，并另写 relocation。
                    form.box = box(*(pymupdf.Rect(form.box.x, form.box.y, form.box.x2, form.box.y2)
                                     * relocation))
                    form.relocation_transform = list(relocation)
                original.pdf_form = list(reversed(forms))
                if in_formula:
                    original.pdf_paragraph = [NS(pdf_paragraph_composition=[
                        NS(pdf_formula=NS(pdf_form=[form]))]) for form in reversed(forms)]
                    original.pdf_form = []
                split_variants._isolate_form_streams(original, pdf)
                self.assertEqual(pdf.xref_stream(source), source_stream)
                self.assertEqual(pdf.xref_get_key(pdf_page.xref, "Resources/XObject/Fm"),
                                 ("xref", f"{source} 0 R"))
                self.assertEqual(len({x.xref_id for x in original.pdf_xobject}), 2)
                page_operations, expected = [], []
                for index, (xobj, form) in enumerate(zip(original.pdf_xobject, forms)):
                    self.assertEqual(pdf.xref_get_key(xobj.xref_id, "PTEX.FileName"),
                                     ("string", "example file.pdf"))
                    ref = form.pdf_form_subtype.pdf_xobj_form
                    self.assertEqual(pdf.xref_get_key(pdf_page.xref, "Resources/XObject/" + ref.do_args),
                                     ("xref", f"{xobj.xref_id} 0 R"))
                    full_ctm = pymupdf.Matrix(0, 1, -1, 0, 0, 0) * pymupdf.Matrix(form.ctm)
                    # 模拟写出器给两个调用保留不同子集；共享流时后者会覆盖前者。
                    local = index * 10
                    operation = (zstd_decompress(xobj.base_operations.value)
                                 + " ".join(str(v) for v in full_ctm)
                                 + f" cm {local} {local} 5 5 re f")
                    pdf.update_stream(xobj.xref_id, operation.encode())
                    page_operations.append("q " + " ".join(str(v) for v in relocation) + " cm "
                                           + " ".join(str(v) for v in form.ctm)
                                           + f" cm /{ref.do_args} Do Q")
                    expected.append(tuple(pymupdf.Rect(local, local, local + 5, local + 5)
                                          * full_ctm * relocation * pdf_page.transformation_matrix))
                content = object_xref("<<>>", " ".join(page_operations).encode())
                pdf.xref_set_key(pdf_page.xref, "Contents", f"{content} 0 R")
                actual = [tuple(d["rect"]) for d in pdf[0].get_drawings()]
                self.assertEqual(actual, expected)

    def test_carrier_identity_rejects_missing_source_order_instead_of_guessing(self):
        original = page()
        original.pdf_xobject = [NS(xobj_id=7, xref_id=42, box=box(0, 0, 600, 800))]
        original.pdf_form = [NS(box=box(0, 0, 600, 800), xobj_id=0, render_order=None,
                                pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=42)))]
        with self.assertRaisesRegex(ValueError, "form_carrier_order_unresolved"):
            create_variants(original, columns())

    def test_formula_carriers_keep_child_streams_and_reach_selected_descendants(self):
        for paragraph_index in (1, 2):
            with self.subTest(paragraph_index=paragraph_index):
                original = page()
                carrier = NS(box=box(330, 300, 560, 470), xobj_id=0, render_order=1,
                             pdf_form_subtype=NS(pdf_xobj_form=NS(xref_id=18)))
                formula = NS(pdf_form=[carrier], pdf_curve=[], pdf_character=[])
                original.pdf_paragraph[paragraph_index].pdf_paragraph_composition = [
                    NS(pdf_character=None, pdf_formula=formula)]
                original.pdf_xobject = [NS(xobj_id=9, xref_id=18, box=box(330, 300, 560, 470))]
                original.pdf_curve = [NS(box=box(340, 310, 550, 460), xobj_id=9)]
                variants = create_variants(original, columns())
                right = variants[1].page
                self.assertEqual([x.xobj_id for x in right.pdf_xobject], [9])
                self.assertEqual(len(right.pdf_form), 0 if paragraph_index == 1 else 1)
                selected = variants[1 if paragraph_index == 1 else 2].page
                self.assertEqual([x.xobj_id for x in selected.pdf_xobject], [9])
                self.assertEqual(len(selected.pdf_form), 0)

    def test_split_paragraph_is_reported_instead_of_silently_dropped_or_guessed(self):
        mixed = PageColumns(0, 2, "jump", ((300, 300),), (),
                            ((0, 0), (1, 1)), (2,))
        with self.assertRaisesRegex(ValueError, "split_paragraph_unresolved"):
            create_variants(page(), mixed)

    def test_source_chars_include_nested_formula_and_preserve_stream_order(self):
        original = page()
        glyph = NS(box=box(50, 700, 60, 710), char_unicode="a", render_order=8,
                   sub_render_order=2, xobj_id=7)
        equation = NS(box=box(330, 700, 340, 710), char_unicode="=", render_order=9,
                      sub_render_order=1, xobj_id=7)
        original.pdf_character = [glyph]
        original.pdf_paragraph[0].pdf_paragraph_composition = [
            NS(pdf_line=NS(pdf_character=[glyph]), pdf_character=None,
               pdf_formula=None, pdf_same_style_characters=None),
            NS(pdf_line=None, pdf_character=None,
               pdf_formula=NS(pdf_character=[equation]), pdf_same_style_characters=None)]
        original.pdf_paragraph[1].pdf_paragraph_composition = []
        original.pdf_paragraph[2].pdf_paragraph_composition = []
        chars = collect_source_chars(original)
        self.assertEqual(len(chars), 2)
        self.assertEqual([(c.order, c.sub_order, c.paragraph, c.xobj, c.text) for c in chars],
                         [(8, 2, 0, 7, "a"), (9, 1, 0, 7, "=")])
        self.assertEqual((chars[0].x0, chars[0].y0, chars[0].y1), (50, 90, 100))

    def test_region_assignment_uses_same_crop_coordinates_as_column_detection(self):
        original = page()
        original.pdf_curve = [NS(box=box(320, 300, 340, 300), xobj_id=0)]
        cropped = PageColumns(0, 2, "jump", ((200, 200),), (),
                              ((0, 0), (1, 1), (2, SPANNING)))
        variants = create_variants(original, cropped,
                                   convert_box=lambda b: (b.x - 100, 800 - b.y2,
                                                          b.x2 - 100, 800 - b.y))
        self.assertEqual([len(v.page.pdf_curve) for v in variants], [0, 1, 0])

    def test_generated_spaces_without_content_stream_order_do_not_become_source_chars(self):
        original = page()
        original.pdf_paragraph = []
        original.pdf_character = [NS(box=box(50, 700, 60, 710), char_unicode=" ",
                                     render_order=None, sub_render_order=None, xobj_id=0)]
        self.assertEqual(collect_source_chars(original), ())
        original.pdf_character[0].char_unicode = "x"
        with self.assertRaisesRegex(ValueError, "source_char_order_missing"):
            collect_source_chars(original)


class MockRunnerTests(unittest.TestCase):
    def test_chen_two_pages_with_local_synthetic_translation(self):
        # 第七页含严格回跳证据；前两页在新规则下本身不足以判定全文双栏。
        self.run_sample("chen2026", (0, 6))

    def test_foba_page10_with_local_synthetic_translation(self):
        # 首页提供全文双栏依据；第十页保留原图形历史，真正进入两次整页复制。
        self.run_sample("foba", (0, 9))

    def run_sample(self, name, numbers):
        """真实 IL/写出器、环回 mock；关闭开关和开启开关各跑一次。"""
        sample_name = name
        import pymupdf
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from mock_openai_server import MockOpenAIHandler, MockOpenAIServer
        from run_split_mock_e2e import isolated_environment

        root = Path(__file__).resolve().parents[1]
        sample = Path(os.environ.get(
            "ZTF_T4_CHEN_SAMPLE" if name == "chen2026" else "ZTF_T62_FOBA_SAMPLE",
            f"G:/ProgramData/zotero-translate-files/ztest/samples/0.8.0/{name}.pdf"))
        if not sample.is_file():
            self.skipTest(f"缺少 {name} 本地原文样本")
        run = root / "build/t62/t4" / (name + '-' + time.strftime("%Y%m%d-%H%M%S"))
        run.mkdir(parents=True)
        source = run / (name + "-two-pages.pdf")
        with pymupdf.open(sample) as full, pymupdf.open() as two:
            for number in numbers:
                two.insert_pdf(full, from_page=number, to_page=number)
            two.save(source)
        environment = isolated_environment(root / "build/t62/resources")
        server = MockOpenAIServer(("127.0.0.1", 0), MockOpenAIHandler, synthetic=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # 仅测试驱动采集预写出 IL 的字符，正文证据只放 build/t62/t4，不进入生产日志。
        harness = run / "run.py"
        harness.write_text('''import copy, dataclasses, hashlib, json, os, sys, traceback
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.argv = sys.argv[2:]
import ztf_split_variants as variants
import ztf_babeldoc_runner as runner
runner._HELPER_MODULES["ztf_split_variants"] = variants
original = variants.create_variants
collect = variants.collect_source_chars
render = variants.render_variants
records = []
reference_variants = []

def lazy_objects(root):
    from babeldoc.format.pdf.document_il.frontend.il_creater_active_support import LazyPassthroughInstruction
    pending, seen, result = [root], set(), []
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, LazyPassthroughInstruction):
            result.append(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif dataclasses.is_dataclass(item):
            pending.extend(getattr(item, f.name) for f in dataclasses.fields(item))
    return result

def reference_copy(root):
    # 对照保留 lazy 渲染语义，memo 仅隔离包装及缓存，不展开历史链。
    memo = {}
    for item in lazy_objects(root):
        detached = copy.copy(item)
        detached.snapshot = copy.copy(item.snapshot)
        detached.snapshot._render_cache = dict(item.snapshot._render_cache)
        memo[id(item)] = detached
    return copy.deepcopy(root, memo)

def render_checked(*args, **kwargs):
    try:
        result = render(*args, **kwargs)
        reference_path = args[2] + '.reference.pdf'
        with patch.object(variants, '_copy_il', reference_copy):
            render(args[0], tuple(reference_variants), reference_path)
        import pymupdf
        hashes = []
        with pymupdf.open(args[2]) as actual, pymupdf.open(reference_path) as reference:
            assert len(actual) == len(reference)
            for a, b in zip(actual, reference):
                ap, bp = a.get_pixmap(), b.get_pixmap()
                assert (ap.width, ap.height, ap.samples) == (bp.width, bp.height, bp.samples)
                hashes.append(hashlib.sha256(ap.samples).hexdigest())
        Path(os.environ['T4_EXPECTED']).with_suffix('.render-equivalence.json').write_text(
            json.dumps({'pixel_equal': True, 'page_hashes': hashes}), encoding='utf-8')
        return result
    except Exception:
        Path(os.environ["T4_EXPECTED"]).with_suffix(".render-diagnostic.txt").write_text(
            traceback.format_exc(), encoding="utf-8")
        raise
variants.render_variants = render_checked
def collect_checked(*args, **kwargs):
    try:
        return collect(*args, **kwargs)
    except Exception:
        Path(os.environ["T4_EXPECTED"]).with_suffix(".diagnostic.txt").write_text(
            traceback.format_exc(), encoding="utf-8")
        raise
variants.collect_source_chars = collect_checked
def glyphs(paragraph, char_columns):
    text = []
    for c in paragraph.pdf_paragraph_composition:
        if c.pdf_character is not None:
            text.append(c.pdf_character)
        elif c.pdf_formula is not None:
            text.extend(c.pdf_formula.pdf_character)
    return "".join(c.char_unicode for c in text
        if char_columns.get((getattr(c, "render_order", None),
            getattr(c, "sub_render_order", None), getattr(c, "xobj_id", None))) != variants.MARGIN)
def checked(page, columns, *args, **kwargs):
    geometry = {"forms": [{"xobj_id": f.xobj_id,
                           "box": [f.box.x, f.box.y, f.box.x2, f.box.y2],
                           "ctm": f.ctm,
                           "inline": f.pdf_form_subtype.pdf_inline_form is not None,
                           "xref": (f.pdf_form_subtype.pdf_xobj_form.xref_id
                                    if f.pdf_form_subtype.pdf_xobj_form else None)}
                          for f in page.pdf_form],
                "xobjects": [{"id": x.xobj_id, "xref": x.xref_id,
                              "box": [x.box.x, x.box.y, x.box.x2, x.box.y2]}
                             for x in page.pdf_xobject]}
    Path(os.environ["T4_EXPECTED"]).with_name(f"geometry-{page.page_number}.json").write_text(
        json.dumps(geometry, indent=2), encoding="utf-8")
    lazy = lazy_objects(page)
    before = [(item._value, dict(item.snapshot._render_cache)) for item in lazy]
    result = original(page, columns, *args, **kwargs)
    assert before == [(item._value, dict(item.snapshot._render_cache)) for item in lazy]
    with patch.object(variants, '_copy_il', reference_copy):
        reference_variants.extend(original(page, columns, *args, **kwargs))
    assert before == [(item._value, dict(item.snapshot._render_cache)) for item in lazy]
    Path(os.environ['T4_EXPECTED']).with_name(f'source-lazy-{page.page_number}.json').write_text(
        json.dumps({'lazy_count': len(lazy), 'source_caches_unchanged': True}), encoding='utf-8')
    assignment = dict(columns.paragraph_column)
    char_columns = args[0] if args else kwargs.get("char_columns", {})
    for variant in result:
        expected = "".join(glyphs(p, char_columns) for i, p in enumerate(page.pdf_paragraph)
                           if assignment.get(i) == variant.column)
        expected += "".join(c.char_unicode for c in variant.page.pdf_character)
        records.append({"source_page": page.page_number, "column": variant.column,
                        "expected": expected})
    Path(os.environ["T4_EXPECTED"]).write_text(json.dumps(records), encoding="utf-8")
    return result
variants.create_variants = checked
sys.exit(runner.main())
''', encoding="utf-8")
        results = {}
        try:
            for enabled in (False, True):
                name = "enabled" if enabled else "disabled"
                directory = run / name
                directory.mkdir()
                expected = directory / "expected.json"
                env = dict(environment, ZTF_SPLIT_DUAL=str(int(enabled)),
                           ZTF_JOB_LOG_PATH=str(directory / "runner.jsonl"),
                           T4_EXPECTED=str(expected))
                command = [sys.executable, "-B", "-X", "utf8", str(harness),
                           str(root / "addon/content/python"),
                           str(root / "addon/content/python/ztf_babeldoc_runner.py"),
                           "--files", str(source), "--output", str(directory / "out"),
                           "--working-dir", str(directory / "work"), "--openai",
                           "--openai-model", "mock-model", "--openai-base-url",
                           f"http://127.0.0.1:{server.server_port}/v1", "--lang-in", "en",
                           "--lang-out", "zh", "--no-dual", "--no-auto-extract-glossary",
                           "--disable-same-text-fallback", "--ignore-cache",
                           "--watermark-output-mode", "no_watermark"]
                started = time.monotonic()
                requests_before = server.request_count
                completed = subprocess.run(command, cwd=directory, env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=600, check=False)
                self.assertEqual(completed.returncode, 0, str(directory / "runner.jsonl"))
                mono, = (directory / "out").glob("*.mono.pdf")
                with pymupdf.open(mono) as pdf:
                    results[name] = {"texts": [p.get_text() for p in pdf],
                                     "nontext_counts": [{"images": len(p.get_image_info()),
                                                         "drawings": len(p.get_drawings())}
                                                        for p in pdf],
                                     "nontext_boxes": [{"images": [list(i["bbox"]) for i in p.get_image_info()],
                                                        "drawings": [list(d["rect"]) for d in p.get_drawings()]}
                                                       for p in pdf],
                                     "requests": server.request_count - requests_before,
                                     "elapsed_seconds": round(time.monotonic() - started, 3)}
                if not enabled:
                    self.assertFalse(list((directory / "out").glob("*.variants.*")))
                    continue
                metadata = json.loads(Path(str(mono) + ".variants.json").read_text(encoding="utf-8"))
                results[name]["variants"] = metadata
                self.assertFalse([p for p in metadata["pages"] if p.get("error")])
                self.assertTrue(metadata["pdf_path"])
                expected_records = json.loads(expected.read_text(encoding="utf-8"))

                def characters(text):
                    return Counter(c for c in unicodedata.normalize("NFKC", text) if not c.isspace())

                with pymupdf.open(metadata["pdf_path"]) as pdf:
                    results[name]["nontext"] = []
                    self.assertEqual(len(pdf), len(expected_records))
                    for item in metadata["pages"]:
                        if item["columns"] > 1:
                            self.assertEqual([v["column"] for v in item["variants"]],
                                             [*range(item["columns"]), SPANNING])
                        for variant in item["variants"]:
                            variant_page = pdf[variant["pdf_page"]]
                            images = [list(image["bbox"]) for image in variant_page.get_image_info()]
                            drawings = [list(drawing["rect"]) for drawing in variant_page.get_drawings()]
                            results[name]["nontext"].append({
                                "source_page": item["source_page"], **variant,
                                "images": images, "drawings": drawings})
                            if variant["column"] != SPANNING:
                                column = variant["column"]
                                left = item["gutters"][column - 1][1] if column else 0
                                right = (item["gutters"][column][0]
                                         if column < item["columns"] - 1 else variant_page.rect.width)
                                for bounds in images + drawings:
                                    self.assertGreaterEqual(bounds[0], left,
                                                            (item["source_page"], column, bounds))
                                    self.assertLessEqual(bounds[2], right,
                                                         (item["source_page"], column, bounds))
                                    self.assertGreaterEqual(bounds[1], 0)
                                    self.assertLessEqual(bounds[3], variant_page.rect.height)
                            record = expected_records[variant["pdf_page"]]
                            self.assertEqual((record["source_page"], record["column"]),
                                             (item["source_page"], variant["column"]))
                            if sample_name != "foba":
                                self.assertEqual(characters(variant_page.get_text()),
                                                 characters(record["expected"]),
                                                 (item["source_page"], variant["column"]))
                        actual = [p for p in results[name]["nontext"]
                                  if p["source_page"] == item["source_page"]]
                        for kind in ("images", "drawings"):
                            self.assertEqual(sum(len(p[kind]) for p in actual),
                                             results[name]["nontext_counts"][item["source_page"]][kind],
                                             (item["source_page"], kind))
                            source_boxes = sorted(results[name]["nontext_boxes"][item["source_page"]][kind])
                            variant_boxes = sorted(b for p in actual for b in p[kind])
                            for original_box, variant_box in zip(source_boxes, variant_boxes):
                                for original_coordinate, variant_coordinate in zip(original_box, variant_box):
                                    # 内容流序列化保留 6 位小数；仅测试浮点写回误差，不放宽认栏。
                                    self.assertAlmostEqual(original_coordinate, variant_coordinate, places=2,
                                                           msg=(item["source_page"], kind,
                                                                original_box, variant_box))
                self.assertTrue(any(p["columns"] == 2 for p in metadata["pages"]))
                equivalence = json.loads(expected.with_suffix(".render-equivalence.json").read_text())
                self.assertTrue(equivalence["pixel_equal"])
                if sample_name == "foba":
                    lazy = json.loads(expected.with_name("source-lazy-1.json").read_text())
                    self.assertGreater(lazy["lazy_count"], 0)
                    self.assertTrue(lazy["source_caches_unchanged"])
            self.assertEqual(results["enabled"]["texts"], results["disabled"]["texts"])
            self.assertGreater(results["disabled"]["requests"], 0)
            self.assertEqual(results["enabled"]["requests"], results["disabled"]["requests"])
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()
            # 不把正文复制进汇总；正文证据保留在本地 expected.json/PDF。
            summary = {name: {key: value for key, value in data.items() if key != "texts"}
                       for name, data in results.items()}
            summary["mock_requests"] = server.request_count
            summary["server_thread_alive"] = thread.is_alive()
            summary["server_closed"] = server.socket.fileno() == -1
            summary["subprocesses_reaped"] = True
            (run / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print("T4 artifacts: " + str(run), flush=True)


class HookFailureTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build/t62/t4"
        root.mkdir(parents=True, exist_ok=True)
        # 当前 Windows 沙箱不允许访问 tempfile 创建的私有 ACL 目录。
        self.directory = root / f"unit-{time.time_ns()}"
        self.directory.mkdir()
        self.temporary = self.directory / "split-variants.pdf"
        self.mono = self.directory / "mono.pdf"
        self.result = NS(mono_pdf_path=self.mono)
        self.original_write = Mock(return_value=self.result)
        self.writer = type("Writer", (), {"write": self.original_write})
        self.translator = type("Translator", (), {"translate": lambda *_: None})
        modules = {
            "babeldoc.format.pdf.document_il.midend.il_translator": NS(ILTranslator=self.translator),
            "babeldoc.format.pdf.document_il.midend.il_translator_llm_only": NS(ILTranslatorLLMOnly=NS()),
            "babeldoc.format.pdf.document_il.backend.pdf_creater": NS(PDFCreater=self.writer),
            "pymupdf": NS(open=lambda _: nullcontext({
                17: NS(transformation_matrix=None, cropbox=NS(width=600, height=800))})),
        }
        patches = [patch.dict(sys.modules, modules),
                   patch("ztf_split_margins.read_pdf_evidence", return_value=({}, {})),
                   patch("ztf_split_columns.detect_columns", return_value=columns()),
                   patch.object(split_variants, "_column_count_tag", return_value=None)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.events = []
        self.config = NS(input_file="unused.pdf",
                         get_working_file_path=lambda _: self.temporary)
        self.page = page()
        self.page.page_number = 17
        self.document = NS(page=[self.page])
        self.creator = NS(docs=self.document)
        split_variants.install(self.events.append)

    def capture(self):
        self.translator().translate(self.document)

    def test_source_error_keeps_reason_and_page_without_interrupting_mono(self):
        self.page.pdf_character = [NS(box=box(40, 700, 50, 710), char_unicode="x",
                                      render_order=None, sub_render_order=None, xobj_id=0)]
        # 设置 translator 的配置，源字符采集在读取 box 前即报告缺少内容流顺序。
        self.translator.translation_config = self.config
        self.capture()
        self.assertIs(self.writer.write(self.creator, self.config), self.result)
        metadata = json.loads(Path(str(self.mono) + ".variants.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["pages"], [{"source_page": 17, "variants": [],
                                             "error": "source_char_order_missing"}])
        self.assertTrue(any(e.get("page") == 17 and e.get("reason") == "source_char_order_missing"
                            for e in self.events))
        self.original_write.assert_called_once()

    def test_render_move_and_mono_failures_remove_partial_temporary_pdf(self):
        for failure in ("render", "move", "mono", "no_mono"):
            with self.subTest(failure=failure):
                self.original_write.reset_mock()
                self.original_write.side_effect = OSError("private") if failure == "mono" else None
                self.result.mono_pdf_path = None if failure == "no_mono" else self.mono
                self.translator.translation_config = self.config
                with patch.object(split_variants, "collect_source_chars", return_value=()):
                    self.capture()

                def render(*_):
                    self.temporary.write_bytes(b"partial PDF")
                    if failure == "render":
                        raise OSError("private")

                replacement = patch.object(Path, "replace", side_effect=OSError("private"))
                with patch.object(split_variants, "render_variants", side_effect=render) as rendering, \
                        replacement if failure == "move" else nullcontext():
                    if failure == "mono":
                        with self.assertRaises(OSError):
                            self.writer.write(self.creator, self.config)
                    else:
                        self.assertIs(self.writer.write(self.creator, self.config), self.result)
                rendering.assert_called_once()
                self.assertFalse(self.temporary.exists())
                self.original_write.assert_called_once()
                self.assertNotIn("private", json.dumps(self.events))


if __name__ == "__main__":
    unittest.main()
