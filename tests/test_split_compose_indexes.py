"""T1: 与安装的 PyMuPDF 1.28.2 逐项比较，保留旧循环作参照。"""
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon/content/python'))
import ztf_split_compose as compose


class ContainmentIndexTests(unittest.TestCase):
    def assert_owners(self, boxes, points):
        paragraphs = [{'box': box, 'index': 100-i} for i, box in enumerate(boxes)]
        # 同时覆盖 index 缺省及值不等于枚举序号。
        if paragraphs:
            paragraphs[-1].pop('index')
        index = compose._ParagraphOwners(paragraphs)
        for point in points:
            expected = next((p.get('index', i) for i, p in enumerate(paragraphs)
                             if fitz.Rect(p['box']).contains(fitz.Point(*point))), -1)
            self.assertEqual(index.owner(*point), expected, (boxes, point))

    def test_point_edges_degenerate_and_order(self):
        boxes = [(0, 0, 10, 10), (0, 0, 10, 10), (10, 0, 20, 10),
                 (0, 0, 0, 10), (20, 20, 10, 10), (-1e9, -1e9, 1e9, 1e9)]
        points = [(0, 0), (10, 0), (0, 10), (10, 10), (20, 10), (5, 5),
                  (10-1e-8, 5), (10+1e-8, 5), (-1e-50, 0), (math.nan, 0)]
        self.assert_owners(boxes, points)
        self.assert_owners(list(reversed(boxes)), points)
        self.assert_owners([(0, 0, 0, 10), (5, 5, 2, 2)], points)

    def test_point_float32_rounding(self):
        # Double 中不同的边界进入 MuPDF 后可能成为同一 float32。
        self.assert_owners([(0.100000001, 0, 10.00000001, 32.00000001)],
                           [(x, y) for x in (0.1, 0.100000002, 9.99999999, 10)
                            for y in (0, 31.99999999, 32, 32.00000001)])

    def test_rect_edges_empty_invalid_and_append_order(self):
        initial = [(0, 0, 10, 10), (4, 4, 4, 8), (20, 20, 10, 10)]
        additions = [(0, 0, 10, 10), (10, 10, 10, 10), (99, 99, 99, 99),
                     (0, 0, 10+1e-8, 10), (10, 10, 0, 0),
                     (-10, -10, 50, 50), (30, 30, 40, 40),
                     (0, 32, 1, 32), (math.nan, 0, 1, 1)]
        self.assert_boxes(initial, additions)

    def assert_boxes(self, initial, additions):
        expected = [fitz.Rect(b) for b in initial]
        actual = [fitz.Rect(b) for b in initial]
        index = compose._BoxIndex(actual)
        for coordinates in additions:
            box = fitz.Rect(coordinates)
            old = any(owner.contains(box) for owner in expected)
            self.assertEqual(index.contains(box), old, coordinates)
            if not old:
                expected.append(box)
            if not index.contains(box):
                actual.append(box)
                index.add(box)
            # 对 NaN 比较使用同一对象；其余逐项检查，禁止重排或替换框。
            self.assertEqual([tuple(b) for b in actual], [tuple(b) for b in expected])

    def test_randomized_points_and_rectangles_match_old_loops(self):
        rng = random.Random(821)
        boxes = [tuple(rng.uniform(-200, 400) for _ in range(4)) for _ in range(300)]
        boxes += [(-1e20, -1e20, 1e20, 1e20), (32, 32, 32, 32)]
        points = [tuple(rng.uniform(-200, 400) for _ in range(2)) for _ in range(1000)]
        points += [(b[x], b[y]) for b in boxes for x in (0, 2) for y in (1, 3)]
        self.assert_owners(boxes, points)
        self.assert_boxes(boxes[:30], boxes[30:] + boxes[:30])

    def test_text_boxes_and_selection_match_original_lists(self):
        rng = random.Random(822)
        initial = [fitz.Rect(*(rng.uniform(-80, 120) for _ in range(4))) for _ in range(50)]
        chars = [compose.Char(i, i, 0, *(rng.uniform(-80, 120) for _ in range(4)),
                              'x' if i % 5 else ' ') for i in range(400)]
        # 含循环刚追加的框、重合框和非空文本的退化框。
        chars += [compose.Char(400+i, 400+i, 0, *b, 'x') for i, b in enumerate(
            [(200, 200, 300, 300), (220, 220, 250, 250), (200, 200, 300, 300),
             (320, 320, 320, 320), (0, 32, 1, 32)])]
        expected = initial[:]
        for char in chars:
            box = fitz.Rect(char.x0, char.y0, char.x1, char.y1)
            if char.text.strip() and not any(owner.contains(box) for owner in expected):
                expected.append(box)
        fake = SimpleNamespace(get_text=lambda *a, **kw: {'blocks': [{'lines': [
            {'bbox': tuple(b), 'spans': [{'text': 'x'}]} for b in initial]}]})
        with patch.object(compose, '_filter', return_value=chars):
            actual = compose._text_boxes(fake)
        self.assertEqual([tuple(b) for b in actual], [tuple(b) for b in expected])
        elements = [(180, 180, 310, 310), (0, 0, 0, 0)]
        reference = [fitz.Rect(b) for b in elements]
        for box in expected:
            if not any(owner.contains(box) for owner in reference):
                reference.append(box)
        regions, cache = [], {}
        with patch.object(compose, '_text_boxes', return_value=actual) as text_boxes:
            for page in (0, 1):
                compose._selection(regions, SimpleNamespace(number=page), 'span', [fake],
                                   elements=elements, cache=cache)
            self.assertEqual(text_boxes.call_count, 1)
        self.assertEqual(regions, [(page, 'span', tuple(b+(0,0,0,0)))
                                   for page in (0, 1) for b in reference])


if __name__ == '__main__':
    unittest.main()
