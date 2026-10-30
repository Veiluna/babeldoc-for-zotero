"""#62：全文认栏、中线与视觉行回归。"""
from dataclasses import replace
import gzip
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon/content/python'))
from ztf_split_types import Char, Line, DocumentColumns, MARGIN, SPANNING
from ztf_split_lines import build_lines
from ztf_split_columns import detect_columns, detect_document_columns


def line(i, x0, x1, y, p=None):
    return Line(0, i, (i,), i, i, x0, x1, y-10, y, y, 'x', (i if p is None else p,))


DUAL = DocumentColumns(2, 'jump', 0, ('jump',))


def document(rows, margins=None, figures=(), tag=None):
    return detect_document_columns([rows], [600], [800], {0: margins or {}}, {0: figures}, tag)


def fixture(name):
    path = Path(__file__).parent / 'fixtures/split080' / (name + '.json.gz')
    raw = json.loads(gzip.decompress(path.read_bytes()))
    chars = tuple(Char(i, i, 0, *b[:4], 'x', b[4]) for i, b in enumerate(raw['chars']))
    return raw, chars, build_lines(raw['page'], chars)


class DetectDocumentTests(unittest.TestCase):
    def test_short_columns_and_first_evidence_page(self):
        rows = (line(0, 40, 280, 620), line(1, 40, 280, 740),
                line(2, 320, 560, 618), line(3, 320, 560, 740))
        result = detect_document_columns([(), rows], [600, 600], [800, 800], {}, {})
        self.assertEqual((result.columns, result.basis, result.page), (2, 'jump', 1))

    def test_tags_precede_jump_and_only_one_or_two_are_supported(self):
        rows = (line(0, 40, 280, 700), line(1, 320, 560, 100))
        for count in (1, 2):
            result = document(rows, tag=count)
            self.assertEqual((result.columns, result.basis), (count, 'tag'))
        self.assertEqual(document((), tag=3).columns, 1)

    def test_each_jump_condition_is_required_without_tolerance(self):
        for next_line in (line(2, 320, 560, 200), line(2, 320, 560, 101),
                          line(2, 280, 560, 100), line(2, 100, 200, 100)):
            rows = (line(0, 40, 280, 100), line(1, 40, 280, 210), next_line)
            with self.subTest(next_line=next_line):
                self.assertEqual(document(rows).columns, 1)
        self.assertEqual(document((line(0, 40, 280, 100), line(1, 40, 280, 210),
                                   line(2, 320, 560, 100))).columns, 2)

    def test_margins_and_yolo_lines_do_not_supply_jump_evidence(self):
        rows = (line(0, 40, 280, 700), line(1, 320, 560, 100))
        self.assertEqual(document(rows, {1: 'tagged'}).columns, 1)
        self.assertEqual(document(rows, figures=((310, 80, 570, 110),)).columns, 1)

    def test_local_upward_move_does_not_replace_the_column_group_start(self):
        rows = (line(0, 40, 280, 100), line(1, 40, 280, 300),
                line(2, 40, 280, 200), line(3, 40, 280, 400), line(4, 320, 560, 150))
        self.assertEqual(document(rows).columns, 1)

    def test_fraction_dropcap_and_superscript_do_not_trigger_jumps(self):
        cases = [
            [Char(0, 0, 0, 40, 100, 60, 140, 'D'), Char(1, 1, 0, 60, 105, 70, 115, 'x')],
            [Char(0, 0, 0, 40, 100, 50, 110, 'x'), Char(1, 1, 0, 50, 96, 55, 105, '2'),
             Char(2, 2, 0, 55, 100, 65, 110, 'y')],
            [Char(0, 0, 0, 40, 110, 50, 120, 'd'), Char(1, 1, 0, 55, 90, 65, 100, 'n')]]
        for chars in cases:
            with self.subTest(chars=chars):
                self.assertEqual(document(build_lines(0, chars)).columns, 1)

    def test_alternating_rows_no_longer_use_gap_evidence(self):
        rows = tuple(line(i, x, x+240, y) for i, (x, y) in enumerate(
            ((40, 100), (320, 100), (40, 200), (320, 200))))
        self.assertEqual((document(rows).columns, document(rows).basis), (1, 'single'))


class DetectPageTests(unittest.TestCase):
    def test_gap_across_midline_is_not_a_character_crossing(self):
        chars = (Char(0, 0, 0, 40, 90, 280, 100, 'a', 0),
                 Char(1, 1, 0, 320, 90, 560, 100, 'b', 1))
        rows = build_lines(0, chars)
        self.assertEqual(len(rows), 1)
        result = detect_columns(0, 600, 800, rows, {}, DUAL)
        self.assertEqual(result.gutters, ((300, 300),))
        self.assertEqual(result.paragraph_column, ((0, 0), (1, 1)))
        self.assertEqual(result.split_paragraphs, ())
        mixed = build_lines(0, (chars[0], replace(chars[1], paragraph=0)))
        self.assertEqual(detect_columns(0, 600, 800, mixed, {}, DUAL).split_paragraphs, (0,))

    def test_visual_row_crossing_makes_all_author_blocks_spanning(self):
        coords = ((40, 100, 20, 0), (40, 120, 20, 0), (295, 100, 10, 1),
                  (295, 120, 10, 1), (500, 100, 20, 2), (500, 120, 20, 2))
        chars = tuple(Char(i, i, 0, x, y-10, x+w, y, 'x', p)
                      for i, (x, y, w, p) in enumerate(coords))
        rows = build_lines(0, chars)
        result = detect_columns(0, 600, 800, rows, {}, DUAL)
        self.assertEqual(result.line_column, (SPANNING,) * len(rows))
        self.assertEqual(result.paragraph_column, ((0, SPANNING), (1, SPANNING), (2, SPANNING)))
        self.assertEqual(result.split_paragraphs, ())

    def test_touching_midline_is_not_crossing_at_any_scale(self):
        rows = (line(0, 40, 300, 100), line(1, 300, 560, 200))
        for f in (0.001, 1, 1000):
            scaled = [replace(r, x0=r.x0*f, x1=r.x1*f, y0=r.y0*f, y1=r.y1*f,
                              baseline=r.baseline*f) for r in rows]
            result = detect_columns(0, 600*f, 800*f, scaled, {}, DUAL)
            self.assertEqual(result.line_column, (0, 1))

    def test_spanning_paragraph_precedes_split_and_margins_are_omitted(self):
        rows = (line(90, 40, 560, 50, 90), line(1, 40, 280, 100, 7),
                line(2, 320, 560, 200, 7), line(3, 295, 305, 300, 7))
        result = detect_columns(0, 600, 800, rows, {90: 'tagged'}, DUAL)
        self.assertEqual(result.line_column, (MARGIN, 0, 1, SPANNING))
        self.assertEqual(result.paragraph_column, ((7, SPANNING),))
        self.assertEqual(result.split_paragraphs, ())

    def test_yolo_and_all_spanning_page(self):
        rows = (line(0, 40, 280, 100), line(1, 320, 560, 100))
        result = detect_columns(0, 600, 800, rows, {}, DUAL, ((30, 80, 570, 110),))
        self.assertEqual((result.columns, result.line_column), (2, (SPANNING, SPANNING)))
        self.assertIn('spanning_figure', result.reasons)
        result = detect_columns(0, 600, 800, (line(0, 40, 560, 100),), {}, DUAL)
        self.assertEqual((result.columns, result.line_column), (2, (SPANNING,)))

    def test_single_document_applies_to_every_page(self):
        rows = (line(0, 40, 560, 100), line(1, 320, 560, 200))
        result = detect_columns(3, 600, 800, rows, {}, DocumentColumns(1, 'tag'))
        self.assertEqual(result.line_column, (0, 0))

    def test_invalid_coordinates_raise_fixed_error_codes(self):
        valid = line(0, 40, 280, 100)
        for w, h in ((float('nan'), 800), (600, float('inf')), (0, 800), (600, -1)):
            with self.assertRaisesRegex(ValueError, '^invalid_page_size$'):
                detect_columns(0, w, h, (valid,), {}, DUAL)
        for bad in (replace(valid, x0=float('nan')), replace(valid, baseline=float('inf')),
                    replace(valid, x1=30), replace(valid, y1=0)):
            with self.assertRaisesRegex(ValueError, '^invalid_line_box$'):
                detect_columns(0, 600, 800, (bad,), {}, DUAL)
        for bounds in ((0, 0, float('inf'), 100), (100, 0, 0, 100)):
            with self.assertRaisesRegex(ValueError, '^invalid_figure_box$'):
                detect_columns(0, 600, 800, (valid,), {}, DUAL, (bounds,))

    def test_real_regression_pages(self):
        names = ('chen2026-p10', 'chen2026-p18', 'foba-p01', 'foba-p17', 'foba-p19',
                 'P01-p02', 'P01-p03', 'P05-p02', 'P05-p04', 'P05-p01', 'foba-p13', 'foba-p16')
        for name in names:
            with self.subTest(sample=name):
                raw, chars, rows = fixture(name)
                margins = {r.index: 'repeated' for r in rows
                           if set(r.paragraphs) & set(raw.get('margin_paragraphs', []))}
                result = detect_columns(raw['page'], raw['width'], raw['height'], rows, margins, DUAL)
                self.assertEqual(result.columns, 2)
                self.assertEqual(result.gutters, ((raw['width']/2,) * 2,))
                if name in ('foba-p13', 'foba-p16'):
                    self.assertEqual(result.split_paragraphs, ())
                    for p in raw['caption_paragraphs']:
                        self.assertEqual(dict(result.paragraph_column)[p], SPANNING)
                if name == 'P05-p01':
                    for p in raw['author_paragraphs']:
                        self.assertEqual(dict(result.paragraph_column)[p], SPANNING)

    def test_foba_short_columns_from_actual_il_without_rawdict_rounding(self):
        path = Path(__file__).parent / 'fixtures/split080/foba-p13.il.json.gz'
        raw = json.loads(gzip.decompress(path.read_bytes()))
        chars = [Char(*c[:3], *c[3:7], 'x', *c[7:]) for c in raw['chars']]
        rows = build_lines(raw['page'], chars)
        margins = {0: {int(k): v for k, v in raw['margins'].items()}}
        result = detect_document_columns([rows], [raw['width']], [raw['height']],
                                         margins, {0: raw['figure_boxes']})
        self.assertEqual((result.columns, result.basis, result.page), (2, 'jump', 0))

    def test_all_four_documents_have_actual_il_jump_evidence(self):
        for name in ('chen2026-p07', 'foba-p01', 'P01-p02', 'P05-p01'):
            path = Path(__file__).parent / 'fixtures/split080' / (name + '.il.json.gz')
            raw = json.loads(gzip.decompress(path.read_bytes()))
            chars = [Char(*c[:3], *c[3:7], 'x', *c[7:]) for c in raw['chars']]
            rows = build_lines(raw['page'], chars)
            margins = {0: {int(k): v for k, v in raw['margins'].items()}}
            result = detect_document_columns([rows], [raw['width']], [raw['height']],
                                             margins, {0: raw['figure_boxes']})
            self.assertEqual((result.columns, result.basis), (2, 'jump'), name)

    def test_actual_il_author_blocks_and_figure_captions_are_whole_spanning_paragraphs(self):
        for name, key in (('P05-p01', 'author_paragraphs'), ('foba-p13', 'caption_paragraphs'),
                          ('foba-p16', 'caption_paragraphs')):
            path = Path(__file__).parent / 'fixtures/split080' / (name + '.il.json.gz')
            raw = json.loads(gzip.decompress(path.read_bytes()))
            rows = build_lines(raw['page'], [Char(*c[:3], *c[3:7], 'x', *c[7:]) for c in raw['chars']])
            margins = {int(k): v for k, v in raw['margins'].items()}
            result = detect_columns(raw['page'], raw['width'], raw['height'], rows, margins,
                                    DUAL, raw['figure_boxes'])
            self.assertEqual(result.split_paragraphs, ())
            for paragraph in raw[key]:
                self.assertEqual(dict(result.paragraph_column)[paragraph], SPANNING)

    def test_original_sample_character_and_paragraph_anchors_are_preserved(self):
        samples = {
            'chen2026-p10': ((3347, 2, 0), (4624, 7, 1)),
            'chen2026-p18': ((297, 2, 0), (4541, 24, 1)),
            'foba-p01': ((759, 5, 0), (5402, 14, 1)),
            'foba-p17': ((2043, 7, 0), (3403, 11, 1)),
            'foba-p19': ((2786, 15, 0), (5022, 27, 1)),
            'P01-p02': ((1013, 2, 0), (3860, 3, 1)),
            'P01-p03': ((1832, 0, 0), (3889, 4, 1)),
            'P05-p02': ((1921, 2, 0), (7099, 10, 1)),
            'P05-p04': ((1111, 3, 0), (4728, 9, 1)),
        }
        for name, anchors in samples.items():
            raw, chars, rows = fixture(name)
            # 沿用旧测试的人工水印标注，不冒充 T3 或真实 IL 判定。
            margins = {r.index: 'repeated' for r in rows
                       if name == 'foba-p17' and any(i >= len(chars)-141 for i in r.char_ids)}
            result = detect_columns(raw['page'], raw['width'], raw['height'], rows, margins, DUAL)
            for char_id, paragraph, column in anchors:
                self.assertEqual(chars[char_id].paragraph, paragraph)
                self.assertEqual(dict(result.paragraph_column)[paragraph], column)
                position, = (i for i, row in enumerate(rows) if char_id in row.char_ids)
                self.assertEqual(result.line_column[position], column)


if __name__ == '__main__':
    unittest.main()
