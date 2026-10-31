"""T5 离线 pdf.js 文本层与 v1 元数据验收，不访问 API。"""
import unittest
from tests.test_split_compose import SplitComposeFixture
from tests import split_quality_check as quality
import pymupdf as fitz

class SplitComposeAcceptance(SplitComposeFixture, unittest.TestCase):
    def test_pdfjs_selection_order_and_v1(self):
        self.run_compose(self.fixture())
        with fitz.open(self.output) as doc: layout=self.layout(doc)
        passed,evidence=quality.pdfjs_order(self.output,layout)
        self.assertTrue(passed,evidence)

if __name__=='__main__':unittest.main()
