"""T5 质量门禁：页边重复/移位和中文变体缩放必须报错。"""
import json
import unittest
from unittest.mock import patch
import pymupdf as fitz
from tests.test_split_compose import SplitComposeFixture
from tests import split_quality_check as quality

class SplitQualityTests(SplitComposeFixture,unittest.TestCase):
    def check(self):
        with patch.object(quality,'pdfjs_order',return_value=(True,{})):
            return quality.build_report(self.source,self.output,self.mono)
    def test_margin_once_at_original_position_and_duplicate_rejected(self):
        layout=self.fixture()
        # 跨栏图形范围可以覆盖正文，不能作为划词身份框抢占两侧文本。
        with fitz.open(self.root/'variants.pdf') as doc:
            doc[2].draw_rect(fitz.Rect(0,0,400,500))
            doc.saveIncr()
        self.run_compose(layout)
        self.assertEqual(self.check()['status'],'PASS')
        with fitz.open(self.output) as doc:
            doc[0].insert_text((210,300),'HEADER full width crosses center 0',fontsize=10)
            doc.saveIncr()
        checks={c['id']:c for c in self.check()['checks']}
        self.assertEqual(checks['headers_footers']['status'],'FAIL')
    def test_shifted_margin_and_missing_variant_are_rejected(self):
        layout=self.fixture();self.run_compose(layout)
        with fitz.open(self.output) as doc:
            for xref in doc[0].get_contents():
                content=doc.xref_stream(xref)
                if b' Do' in content:
                    doc.update_stream(xref,b'q 1 0 0 1 0 5 cm '+content+b' Q')
                    break
            doc.saveIncr()
        self.assertEqual(self.check()['status'],'FAIL')
    def test_scaled_chinese_variant_is_rejected(self):
        self.run_compose(self.fixture())
        with fitz.open(self.output) as doc:
            xref=doc[0].get_contents()[-1]
            doc.update_stream(xref,b'q 0.5 0 0 0.5 100 100 cm '+doc.xref_stream(xref)+b' Q')
            doc.saveIncr()
        checks={c['id']:c for c in self.check()['checks']}
        self.assertEqual(checks['variants_complete']['status'],'FAIL')
    def test_quality_error_omits_exception_text(self):
        with patch.object(quality,'build_report',side_effect=RuntimeError('PRIVATE??')),patch.object(
            quality.sys,'argv',['quality','--source','a','--dual','b','--json']),patch('builtins.print') as display:
            self.assertEqual(quality.main(),1)
        self.assertNotIn('PRIVATE',display.call_args.args[0])

if __name__=='__main__':unittest.main()
