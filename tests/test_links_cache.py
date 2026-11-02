"""T3: cache only unchanged page parsing, keeping native clip semantics."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon/content/python'))
import ztf_links as links


class LinkPageCacheTests(unittest.TestCase):
    def document(self):
        doc = pymupdf.open()
        self.addCleanup(doc.close)
        for rotation in (0, 90, 180, 270):
            page = doc.new_page(width=300, height=240)
            page.insert_text((25, 50), 'A fi [12] boundary text')
            page.insert_text((25, 90), 'Second line (3)')
            page.insert_text((150, 105), '中文 \\ 路径', fontname='china-s')
            page.draw_rect((20, 120, 80, 150))
            page.insert_image((100, 120, 130, 150),
                              stream=b'P6\n1 1\n255\n\xff\x00\x00')
            page.set_rotation(rotation)
        return doc

    def regions(self, page):
        char = page.get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['chars'][0]
        box = pymupdf.Rect(char['bbox'])
        return [links._page_text_rect(page), pymupdf.Rect(20, 30, 180, 95), box,
                pymupdf.Rect((box.x0 + box.x1) / 2, box.y0, 200, box.y1),
                pymupdf.Rect(box.x1 - 1e-8, box.y0, 200, box.y1),
                pymupdf.Rect(-10, -10, 310, 250),
                pymupdf.Rect(0, 0, 5, 5)]

    def test_textbox_rawdict_drawings_and_images_equal_native_on_rotated_pages(self):
        doc = self.document()
        cache = links._PageCache()
        for page in doc:
            for region in self.regions(page):
                for repeat in range(2):
                    self.assertEqual(cache.get(page, 'get_textbox', region), page.get_textbox(region))
                    self.assertEqual(cache.get(page, 'get_text', 'rawdict', region),
                                     page.get_text('rawdict', clip=region))
            for name in ('get_drawings', 'get_image_info'):
                self.assertEqual(cache.get(page, name), getattr(page, name)())
                self.assertEqual(cache.get(page, name), getattr(page, name)())

    def test_native_clip_is_kept_and_identical_region_is_parsed_once(self):
        doc = self.document()
        page = doc[0]
        cache = links._PageCache()
        a, b = self.regions(page)[1:3]
        original = pymupdf.Page.get_text
        with patch.object(pymupdf.Page, 'get_text', autospec=True, side_effect=original) as parse:
            cache.get(page, 'get_text', 'rawdict', a)
            cache.get(doc[0], 'get_text', 'rawdict', pymupdf.Rect(a))
            cache.get(page, 'get_text', 'rawdict', b)
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(parse.call_args_list[0].kwargs, {'clip': a})
        self.assertEqual(parse.call_args_list[1].kwargs, {'clip': b})

    def test_textbox_uses_one_default_textpage_per_page(self):
        doc = self.document()
        cache = links._PageCache()
        regions = self.regions(doc[0])
        original = pymupdf.Page.get_textpage
        with patch.object(pymupdf.Page, 'get_textpage', autospec=True, side_effect=original) as parse:
            for region in regions:
                cache.get(doc[0], 'get_textbox', region)
            cache.get(doc[1], 'get_textbox', regions[0])
        self.assertEqual(parse.call_count, 2)
        self.assertTrue(all(not call.kwargs for call in parse.call_args_list))

    def test_graphics_and_images_parse_once_per_page(self):
        doc = self.document()
        cache = links._PageCache()
        for name in ('get_drawings', 'get_image_info'):
            with patch.object(pymupdf.Page, name, autospec=True,
                              side_effect=getattr(pymupdf.Page, name)) as parse:
                cache.get(doc[0], name)
                cache.get(doc[0], name)
                cache.get(doc[1], name)
                self.assertEqual(parse.call_count, 2)

    def test_cropped_rotated_pages_keep_textbox_line_order(self):
        doc = self.document()
        cache = links._PageCache()
        for index in range(len(doc)):
            page = doc[index]
            page.set_cropbox(pymupdf.Rect(10, 10, 290, 230))
            for region in self.regions(page):
                self.assertEqual(cache.get(page, 'get_textbox', region), page.get_textbox(region))

    def test_font_fallback_zero_width_span_keeps_combining_character_and_link_anchor(self):
        doc = pymupdf.open()
        self.addCleanup(doc.close)
        page = doc.new_page(width=300, height=240)
        page.insert_text((30, 50), 'cafe')
        x = page.search_for('cafe')[0].x1
        # The bundled CJK font has U+0301 with zero advance. Use its embedded
        # buffer under a custom name so the combining mark survives PDF encoding.
        page.insert_font(fontname='fallback', fontbuffer=pymupdf.Font('cjk').buffer)
        page.insert_text((x, 50), '\u0301', fontname='fallback')
        region = pymupdf.Rect(20, 30, 110, 60)
        self.assertEqual(page.get_textbox(region), 'cafe\u0301')
        self.assertEqual(links._PageCache().get(page, 'get_textbox', region), 'cafe\u0301')
        page.insert_link({'kind': pymupdf.LINK_URI, 'from': region, 'uri': 'https://example.invalid/'})
        with pymupdf.open(stream=doc.tobytes(), filetype='pdf') as reopened:
            live = links._source_links(reopened, [])
            reopened._ztf_link_cache = links._PageCache()
            cached = links._source_links(reopened, [])
            self.assertEqual(cached, live)
            self.assertEqual(cached[0]['anchor']['text'], 'café')

    def test_link_annotation_insert_and_destination_rewrite_do_not_change_cached_parsing(self):
        doc = self.document()
        cache = links._PageCache()
        page = doc[0]
        region = links._page_text_rect(page)
        requests = [('get_textbox', region), ('get_text', 'rawdict', region),
                    ('get_drawings',), ('get_image_info',)]
        before = [cache.get(page, *request) for request in requests]
        for destination in ({'type': 'uri', 'uri': 'https://example.invalid/'},
                            {'type': 'goto', 'page': 1, 'point': [25, 50]}):
            links._insert_link(doc, page, pymupdf.Rect(20, 30, 80, 55), destination, 1, [25, 50])
            fresh = links._PageCache()
            self.assertEqual([fresh.get(doc[0], *r) for r in requests], before)
            self.assertEqual([cache.get(doc[0], *r) for r in requests], before)

    def test_document_scoping_and_unregistered_live_calls(self):
        first, second = self.document(), self.document()
        first._ztf_link_cache = links._PageCache()
        second._ztf_link_cache = links._PageCache()
        second[0].insert_text((25, 200), 'different document')
        region = links._page_text_rect(first[0])
        a = links._page_extract(first[0], 'get_textbox', region)
        b = links._page_extract(second[0], 'get_textbox', region)
        self.assertNotEqual(a, b)
        del second._ztf_link_cache
        second[0].insert_text((25, 220), 'live modification')
        self.assertEqual(links._page_extract(second[0], 'get_textbox', region),
                         second[0].get_textbox(region))


if __name__ == '__main__':
    unittest.main()
