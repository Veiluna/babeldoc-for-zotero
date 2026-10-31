"""T5 质量门禁：页边重复/移位和中文变体缩放必须报错。"""
import json
import unittest
from unittest.mock import patch
import pymupdf as fitz
from tests.test_split_compose import SplitComposeFixture
from tests import split_quality_check as quality
import ztf_split_compose as compose

class SplitQualityTests(SplitComposeFixture,unittest.TestCase):
    def test_duplicate_spanning_graphics_and_alternating_body_offsets_are_rejected(self):
        layout=self.graphic_fixture();self.run_compose(layout)
        self.assertEqual(self.check()['status'],'PASS')
        with fitz.open(self.output) as doc:
            image=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,2,2),False);image.clear_with(128)
            doc[0].insert_image(fitz.Rect(80,200,100,220),pixmap=image)
            doc[1].draw_line((70,260),(130,260))
            # show_pdf_page 复用同一层流 xref；只改一张输出页即可构造
            # 明确的偏移回归，同时保留 /ZTFSplit_* 标记在 q/Q 内。
            for n in (2,):
                for xref in doc[n].get_contents():
                    data=doc.xref_stream(xref)
                    if data.startswith((b'/ZTFSplit_en ',b'/ZTFSplit_zh ')):
                        dx=40 if n%2==0 else -40
                        marker,content=data.split(b'\n',1)
                        doc.update_stream(xref,marker+b'\nq 1 0 0 1 '+str(dx).encode()
                            +b' 0 cm '+content.rsplit(b'\nEMC',1)[0]+b' Q\nEMC')
            doc.saveIncr()
            for n in (2,3):
                _,groups=quality.layer_glyphs(doc[n])
                self.assertTrue(groups['en'] and groups['zh'])
                self.assertFalse(groups['unknown'])
        checks={c['id']:c for c in self.check()['checks']}
        self.assertEqual(checks['spanning_graphics']['status'],'FAIL')
        self.assertEqual(checks['body_margins']['status'],'FAIL')

    def test_duplicate_spanning_image_in_chinese_layer_is_rejected(self):
        layout=self.graphic_fixture();self.run_compose(layout)
        self.assertEqual(self.check()['status'],'PASS')
        with fitz.open(self.output) as doc:
            image=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,2,2),False);image.clear_with(128)
            doc[0].insert_image(fitz.Rect(80,200,100,220),pixmap=image)
            xref=doc[0].get_contents()[-1]
            doc.update_stream(xref,b'/ZTFSplit_zh BMC\n'+doc.xref_stream(xref)+b'\nEMC')
            doc.saveIncr()
        checks={c['id']:c for c in self.check()['checks']}
        self.assertEqual(checks['spanning_graphics']['status'],'FAIL')
        page=checks['spanning_graphics']['evidence']['pages'][0]
        self.assertEqual(page['spanning_images'],page['expected_spanning_images']+1)

    def test_missing_one_column_origin_uses_whole_document_pages(self):
        for x in (30,230):
            with self.subTest(origin=x):
                layout=self.fixture()
                self.make_pdf(self.source,[[(x,120,'BODY')],[(180,65,'SPANNING')]])
                stats=self.run_compose(layout)
                self.assertEqual([p['columns'] for p in stats['page_results']],[1,1])
                self.assertEqual(stats['fallback_pages'],0)
                self.assertEqual([p['reason'] for p in stats['page_results']],['column_origin_missing']*2)
                self.assertIn({'event':'split_compose_single_column','reason':'column_origin_missing','count':2},
                              self.events)
                with fitz.open(self.source) as source,fitz.open(self.mono) as mono,fitz.open(self.output) as doc:
                    for n,page in enumerate(doc):
                        expected=(source if n%2==0 else mono)[n//2]
                        self.assertTrue(quality.same_run(quality.glyphs(expected),quality.glyphs(page)))
                report=self.check()
                self.assertEqual(report['status'],'PASS')
                body=next(c for c in report['checks'] if c['id']=='body_margins')['evidence']
                self.assertIsNone(body['translation'])
                self.assertEqual((body['reason'],body['count']),('column_origin_missing',2))
                # 门禁必须拒绝旧的静默零平移分栏输出。
                with patch.object(compose,'column_origin_status',return_value=((30, None),False)):
                    self.run_compose(layout)
                self.assertEqual(self.check()['status'],'FAIL')

    def test_no_body_origins_is_a_valid_zero_translation(self):
        layout=self.fixture()
        self.make_pdf(self.source,[[(180,65,'SPANNING')],[]])
        with fitz.open(self.source) as source:
            analysis=compose.analyze_source(source,layout)
            entries={i:{'columns':2} for i in range(2)}
            self.assertEqual(compose.body_starts(source,analysis,entries),(None,None))
            self.assertEqual(compose.column_translation(source,analysis,entries),0)
        stats=self.run_compose(layout)
        self.assertEqual(stats['fallback_pages'],0)
        self.assertEqual([p['columns'] for p in stats['page_results']],[2,2])
        self.assertNotIn('column_origin_missing',json.dumps(self.events))
        self.assertEqual(self.check()['status'],'PASS')

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
