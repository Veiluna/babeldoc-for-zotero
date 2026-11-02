"""等价性工具必须精确检测字符、像素、导航及布局变化。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import pymupdf as fitz
from run_save_equivalence import OUTPUT, compare_pdfs


class SaveEquivalenceTests(unittest.TestCase):
    def setUp(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        # Windows 沙箱的 TemporaryDirectory 默认 ACL 不可用，与现有夹具一致。
        from unittest.mock import patch
        mkdir = os.mkdir
        with patch('tempfile._os.mkdir', side_effect=lambda path, mode: mkdir(path, 0o777)):
            self.temp = tempfile.TemporaryDirectory(dir=OUTPUT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b = self.root/'a.pdf', self.root/'b.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=200, height=200)
            page.insert_text((20, 40), 'equivalence')
            doc.set_metadata({'producer': 'equivalence test'})
            _, info = doc.xref_get_key(-1, 'Info')
            doc.xref_set_key(int(info.split()[0]), 'ZTFSplitLayout',
                             fitz.get_pdf_str(json.dumps({'v': 1, 'pages': [{'regions': []}]})))
            doc.save(self.a)

    def change(self, mutate):
        with fitz.open(self.a) as doc:
            mutate(doc)
            doc.save(self.b)
        return compare_pdfs(self.a, self.b)

    def test_identical_pdf_passes_every_check(self):
        self.assertEqual(compare_pdfs(self.a, self.a)['status'], 'PASS')

    def test_exact_characters_coordinates_and_pixels(self):
        report = self.change(lambda doc: doc[0].insert_text((20.125, 60), 'new'))
        for key in ('rawdict', 'pixels_72', 'pixels_150'):
            self.assertFalse(report['checks'][key])
            self.assertIn({'page': 1, 'type': key}, report['differences'])

    def test_graphics_only_change_is_detected_by_pixels(self):
        report = self.change(lambda doc: doc[0].draw_line((20, 80), (100, 80)))
        self.assertTrue(report['checks']['rawdict'])
        self.assertFalse(report['checks']['pixels_72'])
        self.assertFalse(report['checks']['pixels_150'])

    def test_extra_page_is_reported_by_number(self):
        report = self.change(lambda doc: doc.new_page(width=200, height=200))
        self.assertFalse(report['checks']['page_count'])
        self.assertIn({'page': 2, 'type': 'missing_or_extra_page'}, report['differences'])

    def test_links_toc_labels_layout_and_sizes(self):
        def mutate(doc):
            doc[0].insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(10, 10, 30, 20),
                                'uri': 'https://example.invalid/'})
            doc.set_toc([[1, 'changed', 1]])
            doc.set_page_labels([{'startpage': 0, 'prefix': 'label'}])
            _, info = doc.xref_get_key(-1, 'Info')
            doc.xref_set_key(int(info.split()[0]), 'ZTFSplitLayout',
                             fitz.get_pdf_str(json.dumps({'v': 1, 'pages': [{'regions': [1]}]})))
            doc[0].set_mediabox(fitz.Rect(0, 0, 220, 200))
        report = self.change(mutate)
        for key in ('links', 'toc', 'label', 'page_labels', 'layout', 'size'):
            self.assertFalse(report['checks'][key], key)

    def test_pdf_id_difference_is_explained(self):
        report = self.change(lambda doc: None)
        self.assertEqual(report['status'], 'PASS')
        self.assertFalse(report['bytes']['identical'])
        self.assertTrue(report['bytes']['indirect_objects_and_raw_streams_identical'])

    def test_mono_or_variants_without_split_layout_still_checks_text_and_pixels(self):
        with fitz.open(self.a) as doc:
            _, info = doc.xref_get_key(-1, 'Info')
            doc.xref_set_key(int(info.split()[0]), 'ZTFSplitLayout', 'null')
            doc[0].insert_text((20.125, 60), 'changed mono or variant')
            doc.save(self.b)
        report = compare_pdfs(self.a, self.b, include_layout=False)
        self.assertFalse(report['checks']['rawdict'])
        self.assertFalse(report['checks']['pixels_72'])
        self.assertFalse(report['checks']['pixels_150'])

    def test_run_comparison_fails_when_only_mono_or_variants_changes(self):
        from run_save_equivalence import compare_runs
        self.change(lambda doc: doc[0].draw_line((20, 80), (100, 80)))
        old = {'pdf': self.a, 'mono': self.a, 'variants': self.a, 'stats': {'pages': 1}}
        for role in ('mono', 'variants'):
            with self.subTest(role=role):
                report = compare_runs(old, dict(old, **{role: self.b}))
                self.assertEqual(report['status'], 'FAIL')
                self.assertFalse(report['checks'][role])
                self.assertEqual(report['artifacts'][role]['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
