"""T5 最小回归：布局、逐字隔离、整份变体、安全网、原子写入。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import pymupdf as fitz
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon/content/python'))
import ztf_split_compose as compose

class SplitComposeFixture:
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'build/t5d/unit'
        root.mkdir(parents=True, exist_ok=True)
        mkdir = os.mkdir
        with patch('tempfile._os.mkdir', side_effect=lambda path, mode: mkdir(path, 0o777)):
            self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.mono, self.output = [self.root / n for n in ('source.pdf','mono.pdf','dual.pdf')]
    def make_pdf(self, path, pages):
        with fitz.open() as doc:
            for items in pages:
                page = doc.new_page(width=400, height=500)
                for x,y,text in items:
                    page.insert_text((x,y), text, fontsize=10)
            doc.save(path)
    def fixture(self, count=2):
        self.make_pdf(self.source, [[(10,25,'HEADER full width crosses center '+str(i)),
            (30,120,'LEFT'+chr(65+i)), (230,120,'RIGHT'+chr(65+i)), (180,65,'SPANNING'+chr(65+i)),
            (10,480,'FOOTER full width original position')] for i in range(count)])
        self.make_pdf(self.mono, [[(30,120,'ZHLEFT'+str(i)),(230,120,'ZHRIGHT'+str(i))] for i in range(count)])
        variants=self.root/'variants.pdf'
        self.make_pdf(variants, [[(x,y,t)] for i in range(count) for x,y,t in
            ((30,120,'ZHLEFT'+str(i)),(230,550 if i==0 else 120,'ZHRIGHT'+str(i)),(180,65,'ZHSPAN'+str(i)))])
        mapping={'version':1,'pdf_path':str(variants),'pages':[
            {'source_page':i,'columns':2,'basis':'jump','variants':[
                {'column':c,'pdf_page':i*3+j} for j,c in enumerate((0,1,-2))]} for i in range(count)]}
        Path(str(self.mono)+'.variants.json').write_text(json.dumps(mapping),encoding='utf-8')
        return {'version':1,'pages':[{'index':i,'paragraphs':[],'layouts':[]} for i in range(count)]}
    def run_compose(self, layout):
        self.events=[]
        return compose.compose_split_document(str(self.source),str(self.mono),layout,str(self.output),log=self.events.append)
    def layout(self, doc):
        _,ref=doc.xref_get_key(-1,'Info')
        return json.loads(doc.xref_get_key(int(ref.split()[0]),'ZTFSplitLayout')[1])

    def graphic_fixture(self):
        layout=self.fixture()
        image=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,2,2),False)
        image.clear_with(128)
        boxes=[(80,200,100,220),(280,240,285,245),(30,320,40,330),(230,320,240,330)]
        for path in (self.source,self.root/'variants.pdf'):
            with fitz.open(path) as doc:
                for i in range(2):
                    for j,box in enumerate(boxes):
                        page=doc[i] if path==self.source else doc[i*3+(2 if j<2 else j-2)]
                        page.insert_image(fitz.Rect(box),pixmap=image)
                    page=doc[i] if path==self.source else doc[i*3+2]
                    page.draw_line((61,191),(90,191))
                    page.draw_line((70,270),(330,270))
                    shape=page.new_shape()
                    shape.draw_rect(fitz.Rect(30,350,40,360));shape.draw_rect(fitz.Rect(230,350,240,360))
                    shape.finish(fill=(0,0,0));shape.commit()
                    shade=doc.get_new_xref()
                    doc.update_object(shade,'<< /ShadingType 2 /ColorSpace /DeviceRGB '
                        '/BBox [80 265 110 275] /Coords [80 265 110 275] '
                        '/Function << /FunctionType 2 /Domain [0 1] '
                        '/C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>')
                    resources=int(doc.xref_get_key(page.xref,'Resources')[1].split()[0])
                    doc.xref_set_key(resources,'Shading',f'<< /ZTFShade {shade} 0 R >>')
                    stream=doc.get_new_xref();doc.update_object(stream,'<<>>')
                    doc.update_stream(stream,b'/ZTFShade sh')
                    doc.xref_set_key(page.xref,'Contents','['+' '.join(
                        f'{xref} 0 R' for xref in [*page.get_contents(),stream])+']')
                    # Pattern 的内部绘制越过栏界；归属取使用它的路径，资源不可再筛。
                    patterned=doc[i] if path==self.source else doc[i*3]
                    pattern=doc.get_new_xref()
                    doc.update_object(pattern,'<< /Type /Pattern /PatternType 1 /PaintType 1 '
                        '/TilingType 1 /BBox [0 0 20 20] /XStep 20 /YStep 20 /Resources <<>> >>')
                    doc.update_stream(pattern,b'0 g 0 0 400 500 re f')
                    res=int(doc.xref_get_key(patterned.xref,'Resources')[1].split()[0])
                    doc.xref_set_key(res,'Pattern',f'<< /ZTFPat {pattern} 0 R >>')
                    stream=doc.get_new_xref();doc.update_object(stream,'<<>>')
                    doc.update_stream(stream,b'q /Pattern cs /ZTFPat scn 30 100 20 20 re f Q')
                    doc.xref_set_key(patterned.xref,'Contents','['+' '.join(
                        f'{xref} 0 R' for xref in [*patterned.get_contents(),stream])+']')
                doc.saveIncr()
        for page in layout['pages']:
            page['layouts']=[{'class':'figure','box':[60,190,340,280]}]
        Path(str(self.mono)+'.layout.json').write_text(json.dumps(layout),encoding='utf-8')
        return layout

class SplitComposeTests(SplitComposeFixture,unittest.TestCase):
    def test_graphics_follow_spanning_figure_owner_and_column_instances(self):
        self.run_compose(self.graphic_fixture())
        with fitz.open(self.output) as doc:
            for page in doc:
                boxes=[tuple(info['bbox']) for info in page.get_image_info()]
                self.assertEqual(sorted(boxes),sorted([(80,200,100,220),(280,240,285,245),
                    (30,320,40,330),(230,320,240,330),(80,225,110,235)]))
                self.assertEqual([box for _,box in compose._GraphicObjects(page).run().paintings],
                    [(61,191,90,191),(70,270,330,270),(30,350,240,360)]+
                    ([(30,380,50,400),(230,380,250,400)] if page.number%2==0 else []))
                pixels=page.get_pixmap(alpha=False)
                expected=(0,0,0) if page.number%2==0 else (255,255,255)
                self.assertEqual(pixels.pixel(35,385),expected)
                self.assertEqual(pixels.pixel(235,385),expected)
                self.assertEqual([box for kind,box in page.get_bboxlog() if kind=='fill-shade'],
                    [(80,225,110,235)])

    def test_shading_infinite_bounds_do_not_grow_output_pages(self):
        layout = self.fixture()
        path = self.root/'variants.pdf'
        with fitz.open(path) as doc:
            page = doc[0]
            shade = doc.get_new_xref()
            doc.update_object(shade, '<< /ShadingType 2 /ColorSpace /DeviceRGB '
                '/Coords [0 0 400 0] /Function << /FunctionType 2 /Domain [0 1] '
                '/C0 [1 0 0] /C1 [0 0 1] /N 1 >> /Extend [true true] >>')
            resources = int(doc.xref_get_key(page.xref,'Resources')[1].split()[0])
            doc.xref_set_key(resources, 'Shading', f'<< /ZTFShade {shade} 0 R >>')
            stream = doc.get_new_xref()
            doc.update_object(stream, '<<>>')
            doc.update_stream(stream, b'/ZTFShade sh')
            doc.xref_set_key(page.xref, 'Contents', '[' + ' '.join(
                f'{xref} 0 R' for xref in [*page.get_contents(),stream]) + ']')
            doc.saveIncr()
        self.run_compose(layout)
        with fitz.open(self.output) as doc:
            self.assertEqual(doc[0].rect.height,500)
            self.assertGreater(doc[1].rect.height,550)
            self.assertLess(doc[1].rect.height,600)

    def test_columns_margins_spans_and_height_without_scaling(self):
        stats=self.run_compose(self.fixture())
        self.assertEqual((stats['output_pages'],stats['fallback_pages']),(4,0))
        with fitz.open(self.output) as doc:
            self.assertEqual([p.rect.width for p in doc],[400]*4)
            self.assertGreater(doc[1].rect.height,550)
            self.assertEqual(doc[0].rect.height,500)
            for i,page in enumerate(doc):
                text=page.get_text()
                self.assertIn(('LEFT' if i%2==0 else 'RIGHT')+chr(65+i//2),text)
                self.assertNotIn(('RIGHT' if i%2==0 else 'LEFT')+chr(65+i//2),text)
                self.assertNotIn('SPANNING',text)
                self.assertEqual(text.count('ZHSPAN'+str(i//2)),1)
                self.assertEqual(text.count('HEADER'),1)
                self.assertEqual(text.count('FOOTER'),1)
                self.assertTrue(all(s['size']==10 for b in page.get_text('dict')['blocks']
                    for l in b.get('lines',[]) for s in l['spans']))
            meta=self.layout(doc)
            self.assertEqual(meta['v'],1)
            self.assertEqual(meta['pages'][1]['height'],doc[1].rect.height)
        self.assertEqual(len(self.events),2)
    def test_t4_and_runtime_errors_only_replace_affected_page(self):
        layout=self.fixture(3)
        path=Path(str(self.mono)+'.variants.json');mapping=json.loads(path.read_text())
        mapping['pages'][1]['error']='PRIVATE正文sk-secret'
        path.write_text(json.dumps(mapping),encoding='utf-8')
        original=compose._compose_columns
        def fail(*args,**kwargs):
            if args[2]['source_page']==2:
                args[4].new_page()
                raise RuntimeError('PRIVATE正文sk-secret')
            return original(*args,**kwargs)
        with patch.object(compose,'_compose_columns',side_effect=fail): stats=self.run_compose(layout)
        self.assertEqual((stats['output_pages'],stats['fallback_pages']),(6,2))
        self.assertNotIn('PRIVATE',json.dumps(self.events))
        self.assertEqual([p['reason'] for p in stats['page_results']],
                         ['composed','t4_page_error','compose_error'])
        with fitz.open(self.output) as doc:
            self.assertIn('ZHSPAN0',doc[0].get_text())
            self.assertIn('LEFTB',doc[2].get_text());self.assertIn('RIGHTB',doc[2].get_text())
            self.assertIn('ZHLEFT1',doc[3].get_text())
            self.assertTrue(all(p.rect.width==400 for p in doc))
        native_filter=compose._filter
        def bad_source_page(page,*args,**kwargs):
            if page.parent.name==str(self.source) and page.number==2 and kwargs.get('collect'):
                raise RuntimeError('PRIVATE source extraction')
            return native_filter(page,*args,**kwargs)
        with patch.object(compose,'_filter',side_effect=bad_source_page):
            self.assertEqual(self.run_compose(layout)['fallback_pages'],2)
        content_boxes=compose._content_boxes
        def bad_chinese_page(page):
            if 'ZHLEFT2' in page.get_text():
                raise RuntimeError('PRIVATE extent extraction')
            return content_boxes(page)
        with patch.object(compose,'_content_boxes',side_effect=bad_chinese_page):
            self.assertEqual(self.run_compose(layout)['fallback_pages'],2)
        mapping['pdf_path']=str(self.root/'missing.pdf')
        path.write_text(json.dumps(mapping),encoding='utf-8')
        self.assertEqual(self.run_compose(layout)['fallback_pages'],3)
    def test_single_column_and_all_spanning_pages(self):
        layout=self.fixture();path=Path(str(self.mono)+'.variants.json');mapping=json.loads(path.read_text())
        mapping['pages'][0]['columns']=1;path.write_text(json.dumps(mapping),encoding='utf-8')
        self.make_pdf(self.source,[[(30,120,'SINGLE')],[(180,65,'SPANNING')]])
        self.make_pdf(self.mono,[[(30,550,'ZHOVERFLOW')],[(30,120,'ZHLEFT1')]])
        self.make_pdf(self.root/'variants.pdf', [[(30,120,'ZHLEFT0')],[(230,120,'ZHRIGHT0')],[(180,65,'ZHSPAN0')],[],[],[(180,65,'ZHSPAN1')]])
        self.run_compose(layout)
        with fitz.open(self.output) as doc:
            self.assertEqual(doc[0].get_text().strip(),'SINGLE')
            self.assertEqual(doc[0].rect.width,doc[1].rect.width)
            self.assertGreater(doc[1].rect.height,550)
            self.assertIn('ZHOVERFLOW',doc[1].get_text())
            self.assertEqual(doc[2].get_text(),doc[3].get_text())
            self.assertIn('ZHSPAN1',doc[2].get_text())
        self.make_pdf(self.source,[[(100,120,'SINGLE')],[(180,65,'SPANNING')]])
        self.make_pdf(self.mono,[[(100,120,'TOP'),(100,550,'ZHOVERFLOW')],[(30,120,'ZHLEFT1')]])
        for path in (self.source,self.mono):
            with fitz.open(path) as doc:
                doc[0].set_cropbox(fitz.Rect(50,50,350,450))
                doc.saveIncr()
        self.run_compose(layout)
        with fitz.open(self.output) as doc:
            self.assertEqual(doc[1].rect.width,300)
            self.assertGreater(doc[1].rect.height,500)
            self.assertIn('ZHOVERFLOW',doc[1].get_text())
            top=next(word for word in doc[1].get_text('words') if word[4]=='TOP')
            self.assertEqual(top[0],50)
    def test_save_failure_preserves_previous_output(self):
        layout=self.fixture();self.output.write_bytes(b'previous')
        with patch.object(compose.os,'replace',side_effect=OSError('private')):
            with self.assertRaisesRegex(RuntimeError,'^split_compose_failed$'):self.run_compose(layout)
        self.assertEqual(self.output.read_bytes(),b'previous')
        self.assertEqual(list(self.root.glob('.ztf-split-*')),[])
if __name__=='__main__':unittest.main()
