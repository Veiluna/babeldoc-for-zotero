"""Real PDF extraction/search regressions; requires the BabelDOC environment.

No translator or API is called. Inputs and installed packages stay read-only.
"""

from __future__ import annotations

import importlib.util
import gc
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pymupdf


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "unicode_runner", ROOT / "addon/content/python/ztf_babeldoc_runner.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

# BabelDOC opens its translation cache on import. Keep it inside this clone.
CACHE = tempfile.TemporaryDirectory(dir=ROOT / "build", prefix="unicode-cache-")
import babeldoc.const
babeldoc.const.CACHE_FOLDER = Path(CACHE.name)
from babeldoc.format.pdf import high_level
from babeldoc.format.pdf.document_il.backend import pdf_creater
from babeldoc.translator.cache import db


def tearDownModule():
    db.close()
    CACHE.cleanup()

REAL_PDF = Path(
    r"G:\ProgramData\zotero-translate-files\ztest\samples\liu2022\translated.pdf"
)
FONT = Path.home() / ".cache/babeldoc/fonts/SourceHanSerifCN-Regular.ttf"


def text(doc):
    return "".join(page.get_text() for page in doc)


def geometry(doc):
    return [
        (page.rect, [
            (char["origin"], char["bbox"])
            for block in page.get_text("rawdict")["blocks"] if "lines" in block
            for line in block["lines"] for span in line["spans"]
            for char in span["chars"]
        ]) for page in doc
    ]


class PdfUnicodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runner._install_min_request_patches(
            runner.TranslationPolicy(), runner.RequestBudget(20, lambda *a: None),
            {}, 8192, True, False, "mock-key",
        )

    def test_real_liu_final_pdf_extraction_search_pixels_and_source(self):
        self.assertTrue(REAL_PDF.is_file(), REAL_PDF)
        source = REAL_PDF.read_bytes()
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            output = Path(temp) / "translated.pdf"
            output.write_bytes(source)
            with pymupdf.open(output) as before:
                self.assertEqual(text(before).count("\uf967"), 47)
                original_geometry = geometry(before)
                pixels = [page.get_pixmap().samples for page in before]
                contents = [[before.xref_stream(x) for x in page.get_contents()] for page in before]
                old_text = text(before)
            result = SimpleNamespace(
                mono_pdf_path=output, dual_pdf_path=None,
                no_watermark_mono_pdf_path=output, no_watermark_dual_pdf_path=None,
            )
            config = SimpleNamespace(get_working_file_path=lambda name: Path(temp) / name)
            high_level.fix_cmap(result, config)
            with pymupdf.open(output) as after:
                extracted = text(after)
                self.assertEqual(extracted.count("\uf967"), 0)
                self.assertEqual(extracted.count("不"), old_text.count("不") + 47)
                self.assertFalse(any(0xF900 <= ord(c) <= 0xFAFF for c in extracted))
                self.assertTrue(any(page.search_for("不") for page in after))
                self.assertEqual(geometry(after), original_geometry)
                self.assertEqual([page.get_pixmap().samples for page in after], pixels)
                self.assertEqual(
                    [[after.xref_stream(x) for x in page.get_contents()] for page in after],
                    contents,
                )
            self.assertEqual(REAL_PDF.read_bytes(), source)

    def _fixture(self, path, *, subset=True):
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 70), "不更類A", fontname="SourceHanSerifCN-Regular.ttf",
                             fontfile=str(FONT), fontsize=14)
            if subset:
                doc.subset_fonts()
            doc.save(path)

    def test_generated_font_with_and_without_subset_prefix(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            for subset in (False, True):
                source = Path(temp) / f"input-{subset}.pdf"
                output = Path(temp) / f"output-{subset}.pdf"
                self._fixture(source, subset=subset)
                source_bytes = source.read_bytes()
                with pymupdf.open(source) as doc:
                    self.assertIn("\uf967", text(doc))
                    before = doc[0].get_pixmap().samples
                    high_level.reproduce_cmap(doc)
                    doc.save(output)
                with pymupdf.open(output) as doc:
                    self.assertIn("不更類A", text(doc))
                    self.assertTrue(doc[0].search_for("不更類"))
                    self.assertEqual(doc[0].get_pixmap().samples, before)
                self.assertEqual(source.read_bytes(), source_bytes)

    def test_single_supplementary_and_non_cjk_multichar_mappings(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            source = Path(temp) / "input.pdf"
            output = Path(temp) / "output.pdf"
            self._fixture(source)
            with pymupdf.open(source) as doc:
                font = doc[0].get_fonts()[0]
                cmap_xref = int(doc.xref_get_key(font[0], "ToUnicode")[1].split()[0])
                # Same glyphs and positions, with deliberately varied extraction mappings.
                entries = [(1, "\uf967"), (2, "\U0002f800"), (3, "Å"),
                           (4, "x²=①"), (5, "fi"), (6, "\uf967A")]
                cmap = (pdf_creater.TOUNICODE_HEAD + "\n6 beginbfchar\n" + "\n".join(
                    f"<{cid:04x}><{value.encode('utf-16-be').hex()}>" for cid, value in entries
                ) + "\nendbfchar\n" + pdf_creater.TOUNICODE_TAIL).encode()
                doc.update_stream(cmap_xref, cmap)
                stream = doc[0].get_contents()[0]
                doc.update_stream(stream, b"BT /SourceHanSerifCN-Regular.ttf 14 Tf 40 770 Td <000100020003000400050006> Tj ET")
                before = text(doc)
                self.assertIn("\U0002f800", before)
                pixels = doc[0].get_pixmap().samples
                high_level.reproduce_cmap(doc)
                doc.save(output)
            with pymupdf.open(output) as doc:
                self.assertEqual(text(doc), before.replace("\uf967", "不", 1).replace("\U0002f800", "丽"))
                self.assertTrue(doc[0].search_for("不丽"))
                self.assertEqual(doc[0].get_pixmap().samples, pixels)
                # Multi-character destinations, including a compatibility codepoint,
                # must remain byte-for-byte intact.
                after_cmap = doc.xref_stream(cmap_xref)
                for cid, value in entries[2:]:
                    self.assertIn(f"<{cid:04x}><{value.encode('utf-16-be').hex()}>".encode(), after_cmap)

    def test_failed_stream_update_never_replaces_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            output = Path(temp) / "translated.pdf"
            self._fixture(output)
            original = output.read_bytes()
            result = SimpleNamespace(mono_pdf_path=output, dual_pdf_path=None,
                                     no_watermark_mono_pdf_path=None, no_watermark_dual_pdf_path=None)
            config = SimpleNamespace(get_working_file_path=lambda name: Path(temp) / name)
            with patch.object(pymupdf.Document, "update_stream", side_effect=RuntimeError("write failed")):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    high_level.fix_cmap(result, config)
            gc.collect()  # The upstream fix_cmap leaves its Document open on error.
            self.assertEqual(output.read_bytes(), original)
            with pymupdf.open(output) as doc:
                self.assertIn("\uf967", text(doc))

    def test_real_font_generated_five_digit_scalar_range(self):
        font = Path.home() / ".cache/babeldoc/fonts/LXGWWenKaiGB-Regular.1.520.ttf"
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            source = Path(temp) / "input.pdf"
            output = Path(temp) / "output.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((40, 70), "\U0002f89a\U0002f89b", fontname=font.name,
                                 fontfile=str(font), fontsize=14)
                doc.save(source)
            original = source.read_bytes()
            with pymupdf.open(source) as doc:
                xref = int(doc.xref_get_key(doc[0].get_fonts()[0][0], "ToUnicode")[1].split()[0])
                self.assertIn(b"<565c> <565d> <2f89a>", doc.xref_stream(xref))
                self.assertEqual(text(doc), "\u2f89\u2f8a\n")
                before_pixels = doc[0].get_pixmap().samples
                before_geometry = geometry(doc)
                before_contents = [doc.xref_stream(x) for x in doc[0].get_contents()]
                high_level.reproduce_cmap(doc)
                doc.save(output)
            with pymupdf.open(output) as doc:
                self.assertEqual(text(doc), "\u5f6b\u38e3\n")
                self.assertTrue(doc[0].search_for("\u5f6b\u38e3"))
                self.assertEqual(doc[0].get_pixmap().samples, before_pixels)
                self.assertEqual(geometry(doc), before_geometry)
                self.assertEqual([doc.xref_stream(x) for x in doc[0].get_contents()], before_contents)
            self.assertEqual(source.read_bytes(), original)

    def test_array_range_and_multiline_array_with_scalar_count_records(self):
        cases = [
            (b"1 beginbfrange\n<0001><0002>[<f967><0041>]\nendbfrange\n",
             b"00010002", "\uf967A\n", "不A\n", 1),
            (b"2 beginbfrange\n<0001><0005>[\n<f967>\n<0041>\n<f9670041>\n"
             b"<007800b2003d2460>\n<00c5>\n]\n<0006><0006><f967>\nendbfrange\n",
             b"000100020003000400050006", "\uf967A\uf967Ax²=①Å\uf967\n",
             "不A\uf967Ax²=①Å不\n", 2),
        ]
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            for index, (mapping, cids, before_text, expected, records) in enumerate(cases):
                with self.subTest(index=index):
                    source = Path(temp) / f"input-{index}.pdf"
                    output = Path(temp) / f"output-{index}.pdf"
                    self._fixture(source)
                    with pymupdf.open(source) as doc:
                        xref = int(doc.xref_get_key(doc[0].get_fonts()[0][0], "ToUnicode")[1].split()[0])
                        doc.update_stream(xref, pdf_creater.TOUNICODE_HEAD.encode() + b"\n"
                                          + mapping + pdf_creater.TOUNICODE_TAIL.encode())
                        doc.update_stream(doc[0].get_contents()[0],
                                          b"BT /SourceHanSerifCN-Regular.ttf 14 Tf 40 770 Td <"
                                          + cids + b"> Tj ET")
                        self.assertEqual(text(doc), before_text)
                        before_pixels = doc[0].get_pixmap().samples
                        before_geometry = geometry(doc)
                        before_contents = [doc.xref_stream(x) for x in doc[0].get_contents()]
                        high_level.reproduce_cmap(doc)
                        doc.save(output)
                    with pymupdf.open(output) as doc:
                        self.assertEqual(text(doc), expected)
                        self.assertTrue(doc[0].search_for("不A"))
                        self.assertEqual(doc[0].get_pixmap().samples, before_pixels)
                        self.assertEqual(geometry(doc), before_geometry)
                        self.assertEqual([doc.xref_stream(x) for x in doc[0].get_contents()], before_contents)
                        cmap = doc.xref_stream(xref)
                        self.assertEqual(re.findall(rb"(\d+) beginbfrange", cmap), [str(records).encode()])
                        if index == 1:
                            for value in (b"<f9670041>", b"<007800b2003d2460>", b"<00c5>"):
                                self.assertIn(value, cmap)


if __name__ == "__main__":
    unittest.main()
