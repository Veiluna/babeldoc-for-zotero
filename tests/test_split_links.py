"""T5 导航回归：语言侧、命名目标、页边/跨栏链接、目录和页码标签。"""
import json
import hashlib
import re
import unittest
from unittest.mock import patch
import pymupdf as fitz
from tests.test_split_compose import SplitComposeFixture
import ztf_split_compose as compose

class SplitLinksTests(SplitComposeFixture, unittest.TestCase):
    def test_document_modal_starts_drive_layers_regions_links_and_toc(self):
        layout=self.fixture()
        for path in (self.source,self.mono,self.root/'variants.pdf'):
            with fitz.open(path) as doc:
                # Right-column start 250 minus left start 30 is 220, not half-width 200.
                for index,page in enumerate(doc):
                    for xref in page.get_contents():
                        data=doc.xref_stream(xref)
                        if b'230 ' in data:
                            data=data.replace(b'230 ',b'250 ')
                        # 后一页段首缩进是离群值；全文仍共用前一页众数差 220。
                        if (index//3 if path==self.root/'variants.pdf' else index)==1:
                            data=data.replace(b' 30 ',b' 40 ').replace(b' 250 ',b' 270 ')
                        doc.update_stream(xref,data)
                if path!=self.root/'variants.pdf':
                    doc[0].insert_link({'kind':fitz.LINK_GOTO,'from':fitz.Rect(30,110,58,122),
                        'page':1,'to':fitz.Point(275,120)})
                    if path==self.source:
                        doc.set_toc([[1,'right',2,{'kind':fitz.LINK_GOTO,'to':fitz.Point(275,120)}]])
                doc.saveIncr()
        with fitz.open(self.source) as source:
            analysis=compose.analyze_source(source,layout)
            self.assertEqual(compose.column_translation(source,analysis,{i:{'columns':2} for i in range(2)}),220)
        self.run_compose(layout)
        with fitz.open(self.output) as doc:
            for n,page in enumerate(doc):
                words={w[4]:w[0] for w in page.get_text('words')}
                self.assertEqual(words[('LEFT' if n%2==0 else 'RIGHT')+chr(65+n//2)],
                    30 if n//2==0 else 40 if n%2==0 else 50)
                self.assertEqual(words[('ZHLEFT' if n%2==0 else 'ZHRIGHT')+str(n//2)],
                    250 if n//2==0 else 260 if n%2==0 else 270)
            self.assertEqual(sorted(l['to'].x for l in doc[0].get_links()),[55,275])
            self.assertEqual(doc.get_toc(False)[0][3]['to'].x,55)
            regions=self.layout(doc)['pages'][0]['regions']
            self.assertTrue(any(r['k']=='zh' and r['r'][0]==250 for r in regions))

    def prefixes(self, doc):
        tree=doc.xref_get_key(doc.pdf_catalog(),'PageLabels/Nums')[1]
        return [doc.xref_get_key(int(ref),'P')[1]
                for ref in re.findall(r'\d+ (\d+) 0 R',tree)]

    def test_navigation_and_page_labels(self):
        layout=self.fixture()
        for path, x in ((self.source,30),(self.mono,30)):
            with fitz.open(path) as doc:
                doc[0].insert_link({'kind':fitz.LINK_GOTO,'from':fitz.Rect(x,110,x+28,122),
                                    'page':1,'to':fitz.Point(240,120)})
                doc[0].insert_link({'kind':fitz.LINK_URI,'from':fitz.Rect(x,110,x+28,122),
                                    'uri':'https://example.org/'})
                if path==self.source:
                    doc.set_toc([[1,'Right column',2,{'kind':fitz.LINK_GOTO,'to':fitz.Point(240,120)}]])
                    doc.xref_set_key(doc.pdf_catalog(),'PageLabels','<</Nums [0 <</P <feff7b2c4e00> >>]>>')
                doc.saveIncr()
        stats=self.run_compose(layout)
        self.assertEqual(stats['toc_entries'],1)
        self.assertEqual(stats['labels_unresolved'],0)
        with fitz.open(self.output) as doc:
            links=doc[0].get_links()
            internal=[l for l in links if l['kind']==fitz.LINK_GOTO]
            self.assertEqual({l['page'] for l in internal},{3})
            self.assertEqual(sorted(l['to'].x for l in internal),[40,240])
            self.assertEqual(sum(l.get('uri')=='https://example.org/' for l in links),2)
            self.assertEqual(doc.get_toc()[0][2],4)
            self.assertEqual([r['prefix'] for r in doc.get_page_labels()],['<FEFF7B2C4E000061>','<FEFF7B2C4E000062>']*2)

    def test_named_targets_margin_span_links_and_escaped_nested_labels(self):
        layout=self.fixture()
        uri='https://example.org/路径/(a)\\tail?q=1'
        with fitz.open(self.source) as doc:
            doc.xref_set_key(doc.pdf_catalog(),'Names',
                f'<</Dests <</Names [(right) [{doc.page_xref(1)} 0 R /XYZ 240 380 0]]>>>>')
            doc[0].insert_link({'kind':fitz.LINK_GOTO,'page':-1,'to':'right',
                'from':fitz.Rect(30,110,58,122)})
            doc[0].insert_link({'kind':fitz.LINK_URI,'uri':'https://example.org/',
                'from':fitz.Rect(10,15,100,27)})
            xref=next(l['xref'] for l in doc[0].get_links() if l.get('uri'))
            doc.xref_set_key(xref,'A/URI',fitz.get_pdf_str(uri))
            doc.xref_set_key(doc.pdf_catalog(),'PageLabels',
                '<</Kids [<</Nums [0 <</S /r /St 4 /P (A\\(\\)\\\\)>>]>>]>>')
            doc.set_toc([[1,'Top',1],[2,'Right',2,{'kind':fitz.LINK_GOTO,'to':fitz.Point(240,120)}]])
            doc.saveIncr()
        with fitz.open(self.mono) as doc:
            doc[0].insert_link({'kind':fitz.LINK_GOTO,'page':1,'to':fitz.Point(185,60),
                'from':fitz.Rect(180,55,200,65)})
            doc.saveIncr()
        stats=self.run_compose(layout)
        self.assertEqual(stats['links_unresolved'],0)
        with fitz.open(self.output) as doc:
            self.assertEqual(self.prefixes(doc),['A()\\iva','A()\\ivb','A()\\va','A()\\vb'])
            self.assertEqual([row[0] for row in doc.get_toc()],[1,2])
            for n in (0,1):
                self.assertIn(uri,[link.get('uri') for link in doc[n].get_links()])
                span=next(l for l in doc[n].get_links() if l['kind']==fitz.LINK_GOTO and l['to'].x==185)
                self.assertEqual(span['page'],2)
            named=next(l for l in doc[0].get_links() if l['kind']==fitz.LINK_GOTO and l['page']==3)
            self.assertEqual(named['to'],fitz.Point(40,120))

    def test_invalid_navigation_and_label_write_are_local_and_log_only_codes(self):
        layout=self.fixture()
        with fitz.open(self.source) as doc:
            doc[0].insert_link({'kind':fitz.LINK_GOTO,'page':1,'to':fitz.Point(240,120),
                'from':fitz.Rect(30,110,58,122)})
            doc.saveIncr()
        get_links=fitz.Page.get_links
        set_key=fitz.Document.xref_set_key
        def invalid_zoom(page):
            links=get_links(page)
            if page.parent.name==str(self.source):
                for link in links:link['zoom']=float('nan')
            return links
        failed=False
        def fail_label(doc,xref,key,value):
            nonlocal failed
            if key=='P' and not failed:
                failed=True
                raise ValueError('PRIVATE label')
            return set_key(doc,xref,key,value)
        with patch.object(fitz.Page,'get_links',invalid_zoom),patch.object(
            fitz.Document,'xref_set_key',fail_label),self.assertLogs(compose.__name__,level='WARNING') as logs:
            stats=self.run_compose(layout)
        self.assertEqual(stats['links_zoom_fallback'],1)
        self.assertEqual(stats['labels_unresolved'],1)
        self.assertEqual(stats['fallback_pages'],0)
        records=[json.loads(record.getMessage()) for record in logs.records]
        self.assertTrue(all(set(record)<={'event','reason','page'} for record in records))
        self.assertNotIn('PRIVATE',json.dumps(records))
        with fitz.open(self.output) as doc:
            self.assertEqual(len(doc),4)
            self.assertEqual(doc[0].get_links()[0]['zoom'],0)

    def test_rotated_safety_net_preserves_visual_content_and_link_positions(self):
        for rotation,cropped in ((90,False),(180,False),(270,False),(90,True)):
            with self.subTest(rotation=rotation,cropped=cropped):
                layout=self.fixture()
                expected=[]
                for path in (self.source,self.mono):
                    with fitz.open(path) as doc:
                        x,y=(100,400) if cropped else (30,450)
                        doc[0].insert_text((x,y),'BOTTOM',fontsize=10)
                        doc[0].insert_link({'kind':fitz.LINK_GOTO,'page':0,'to':fitz.Point(x,y),
                            'from':fitz.Rect(x,y-10,x+40,y+2)})
                        if cropped:doc[0].set_cropbox(fitz.Rect(50,50,350,450))
                        doc[0].set_rotation(rotation)
                        # Rendering is an exact before/after visual check, with no tolerance.
                        pixmap=doc[0].get_pixmap()
                        expected.append((pixmap.width,pixmap.height,hashlib.sha256(pixmap.samples).hexdigest()))
                        doc.saveIncr()
                self.run_compose(layout)
                with fitz.open(self.output) as doc:
                    for n in (0,1):
                        actual=doc[n].get_pixmap()
                        self.assertEqual((actual.width,actual.height,hashlib.sha256(actual.samples).hexdigest()),expected[n])
                        self.assertIn('BOTTOM',doc[n].get_text())
                        self.assertEqual(doc[n].get_links()[0]['page'],n)

if __name__=='__main__':unittest.main()
