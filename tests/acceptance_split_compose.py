"""显式离线验收：Zotero pdf.js 文本层检查与既有 P05 样本。

仓库根目录运行 python tests/acceptance_split_compose.py，不调用真实 API。
缺少 Node/pdf.js 或 P05 样本时明确跳过；默认 unittest 不依赖这些资源。
"""

import json
from collections import Counter
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import pymupdf as fitz

import test_split_compose as fixtures
from ztf_split_plan import PagePlan, Region, plan_page
import ztf_split_compose as compose


class SplitComposeAcceptance(fixtures.SplitComposeFixture, unittest.TestCase):
    def make_pdf(self, path, items, width=400, height=500):
        super().make_pdf(path, items, width, height, embed_cjk=True)

    def pdfjs_content(self, path=None):
        directory = (Path(__file__).resolve().parents[1] /
                     "build/research/omni/resource/reader/pdf/build")
        if not shutil.which("node") or not all((directory / name).is_file()
                for name in ("pdf.mjs", "pdf.worker.mjs")):
            self.skipTest("缺少 Node 或本机 Zotero 打包的 pdf.js（含 worker）")
        script = r"""
import fs from 'node:fs';
globalThis.DOMMatrix = class {};
globalThis.Path2D = class {};
globalThis.ImageData = class {};
Uint8Array.prototype.toHex ??= function() { return Buffer.from(this).toString('hex'); };
const pdfjs = await import(process.argv[1] + '/pdf.mjs');
pdfjs.GlobalWorkerOptions.workerSrc = process.argv[1] + '/pdf.worker.mjs';
const task = pdfjs.getDocument({ data: new Uint8Array(fs.readFileSync(process.argv[2])), useSystemFonts: false });
try {
  const doc = await task.promise;
  const metadata = await doc.getMetadata();
  const pages = [];
  for (let number = 1; number <= doc.numPages; number++) {
    const page = await doc.getPage(number);
    const content = await page.getTextContent();
    pages.push(content.items.filter(item => item.str?.trim()).map(item => {
      const style = content.styles[item.fontName];
      const t = item.transform;
      const scale = Math.hypot(t[0], t[1]);
      const points = [style.descent, style.ascent].flatMap(a => [
        [t[4] + t[2] * a, t[5] + t[3] * a],
        [t[4] + t[2] * a + t[0] / scale * item.width,
         t[5] + t[3] * a + t[1] / scale * item.width]
      ]);
      return { str: item.str, transform: item.transform, width: item.width,
        left: Math.min(...points.map(p => p[0])), right: Math.max(...points.map(p => p[0])),
        bottom: Math.min(...points.map(p => p[1])), top: Math.max(...points.map(p => p[1])) };
    }));
  }
  console.log('ZTFRESULT:' + JSON.stringify({
    version: pdfjs.version,
    layout: metadata.info.Custom?.ZTFSplitLayout ? JSON.parse(metadata.info.Custom.ZTFSplitLayout) : null, pages
  }));
} finally { await task.destroy(); }
"""
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script,
                                 directory.as_uri(), str(path or self.output)], capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = [line.removeprefix("ZTFRESULT:") for line in result.stdout.splitlines()
                  if line.startswith("ZTFRESULT:")]
        self.assertEqual(len(values), 1, result.stdout)
        return json.loads(values[0])

    def assert_pdfjs_text_and_regions(self, expected):
        result = self.pdfjs_content()
        self.assertEqual(result["layout"]["v"], 1)
        self.assertEqual(len(result["pages"]), len(expected))
        self.assertEqual(len(result["layout"]["pages"]), len(expected))
        ranks = {"hf": 0, "span": 1, "en": 2, "zh": 3}
        for items, entry, groups in zip(result["pages"], result["layout"]["pages"], expected):
            self.assert_disjoint_regions(entry)
            # 完整文本逐类精确比较，能检出截尾、重复作者、隐藏的栏外文字。
            actual = {kind: "" for kind in ranks}
            order = []
            for item in items:
                kind = self.item_kind(item, entry)
                actual[kind] += re.sub(r"\s+", "", item["str"])
                order.append(ranks[kind])
            self.assertEqual(order, sorted(order))
            self.assertEqual([ranks[r["k"]] for r in entry["regions"]],
                             sorted(ranks[r["k"]] for r in entry["regions"]))
            self.assertEqual(actual, {kind: re.sub(r"\s+", "", groups.get(kind, "")) for kind in ranks})

    def assert_disjoint_regions(self, entry):
        for i, region in enumerate(entry["regions"]):
            for peer in entry["regions"][i + 1:]:
                if region["k"] != peer["k"]:
                    self.assertFalse(fitz.Rect(region["r"]).intersects(fitz.Rect(peer["r"])),
                                     (entry["src"], region, peer))

    def item_kind(self, item, entry):
        kinds = {r["k"] for r in entry["regions"]
                 if r["r"][0] - 0.05 <= item["left"]
                 and item["right"] <= r["r"][2] + 0.05
                 and r["r"][1] - 0.05 <= item["bottom"]
                 and item["top"] <= r["r"][3] + 0.05}
        self.assertEqual(len(kinds), 1, (entry["src"], item, entry["regions"]))
        return kinds.pop()

    def test_pdfjs_taller_span_keeps_complete_source_body_without_leakage(self):
        self.run_compose([self.make_tall_span_case()])
        self.assert_pdfjs_text_and_regions([
            {"span": "TITLE_ZHAUTHOR_ONE", "en": "LEFT_EN", "zh": "左栏译文"},
            {"en": "RIGHT_EN", "zh": "右栏译文"}])

    def test_pdfjs_merged_spans_embed_each_author_and_institute_once(self):
        self.run_compose([self.make_overlapping_spans_case()])
        self.assert_pdfjs_text_and_regions([
            {"span": "TITLE_ZHAUTHOR_ONEINSTITUTE", "en": "LEFT_EN", "zh": "左栏译文"},
            {"en": "RIGHT_EN", "zh": "右栏译文"}])

    def test_pdfjs_gutter_keeps_every_character_without_other_column(self):
        self.run_compose([self.make_gutter_case()])
        self.assert_pdfjs_text_and_regions([
            {"en": "LEFT_EN", "zh": "左栏译文保留全部十八字包括最后四个字"},
            {"en": "RIGHT_EN", "zh": "右栏译文"}])

    def test_pdfjs_expanded_span_does_not_truncate_gutter_translation(self):
        with self.assertNoLogs(compose.__name__, level="WARNING"):
            self.run_compose([self.make_expanded_span_gutter_case()])
        self.assert_pdfjs_text_and_regions([
            {"span": "TITLE_ZHAUTHOR_ONEINSTITUTE", "en": "LEFT_EN",
             "zh": "左栏译文保留全部十八字包括最后四个字"},
            {"en": "RIGHT_EN", "zh": "右栏译文"}])

    def test_pdfjs_separated_lines_keep_complete_text(self):
        self.run_compose([self.make_gutter_case(separated=True)])
        self.assert_pdfjs_text_and_regions([
            {"en": "LEFT_EN", "zh": "左栏译文保留全部十八字包括最后四个字"},
            {"en": "RIGHT_EN", "zh": "右栏译文"}])

    def test_pdfjs_header_and_title_in_one_block_remain_separate(self):
        self.run_compose([self.make_header_title_case()])
        self.assert_pdfjs_text_and_regions([
            {"hf": "HEADER", "span": "TITLE_ZH", "en": "LEFT_EN", "zh": "左栏译文"},
            {"hf": "HEADER", "en": "RIGHT_EN", "zh": "右栏译文"}])

    def test_true_overlap_fails_instead_of_accepting_truncated_text(self):
        with self.assertLogs(compose.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "^split_compose_failed$"):
                self.run_compose([self.make_gutter_case(collision=True)])
        self.assertIn({"event": "split_compose_collision", "reason": "column_text_overlap", "count": 1,
                       "source_index": 0},
                      [json.loads(record.message) for record in logs.records])
        self.assertFalse(self.output.exists())

    def test_pdfjs_stream_order_headers_spans_english_chinese_and_redaction(self):
        self.make_pdf(self.source, [(10, 20, "HEADER"), (10, 480, "FOOTER"),
                                   (30, 140, "LEFT_EN"), (230, 140, "RIGHT_EN"),
                                   (10, 450, "OUTSIDE_SECRET")])
        self.make_pdf(self.mono, [(100, 100, "跨栏标题"), (30, 140, "左栏译文"),
                                 (230, 140, "右栏译文"), (10, 450, "区域外文字")])
        plan = PagePlan(0, 2, (
            Region("column", (25, 120, 170, 220), column=0),
            Region("column", (225, 120, 370, 220), column=1),
            Region("spanning", (0, 80, 400, 105), "top"),
            Region("header_footer", (0, 5, 400, 25)),
            Region("header_footer", (0, 465, 400, 490))))
        self.run_compose([plan])
        self.assert_pdfjs_text_and_regions([
            {"hf": "HEADERFOOTER", "span": "跨栏标题", "en": "LEFT_EN", "zh": "左栏译文"},
            {"hf": "HEADERFOOTER", "en": "RIGHT_EN", "zh": "右栏译文"}])

    @unittest.skipUnless(Path("G:/Program Files/Zotero/datas/storage/JYCINM3Z/P05.pdf").is_file()
        and (Path(__file__).resolve().parents[1] / "build/t09/out/input.no_watermark.zh.mono.pdf").is_file(),
        "缺少 P05 源 PDF 或既有 mono 样本")
    def test_real_p05_all_13_pages_conserve_text_by_column_and_match_metadata(self):
        source = "G:/Program Files/Zotero/datas/storage/JYCINM3Z/P05.pdf"
        mono = Path(__file__).resolve().parents[1] / "build/t09/out/input.no_watermark.zh.mono.pdf"
        doc = fitz.open(source)
        try:
            # 样本没有版面 JSON，以文字块构造近似版面；不替代真实 IL 验收。
            self.assertEqual(len(doc), 13)
            plans = []
            for index, page in enumerate(doc):
                paragraphs = []
                for block in page.get_text("blocks"):
                    if block[6] != 0:
                        continue
                    author = index == 0 and 105 < block[1] and block[3] < 160
                    marginal = block[3] < 75 or block[1] > 740 or (
                        index == 0 and block[0] < 300 and 645 < block[1] < 715)
                    label = "author_info_hybrid" if author else (
                        "title" if index == 0 and block[3] < 105 else "text")
                    paragraphs.append({"box": list(block[:4]), "label": label,
                                       "translated": not (author or marginal),
                                       "marginal": marginal, "author": author})
                plans.append(plan_page({"index": index, "width": page.rect.width,
                                        "height": page.rect.height, "paragraphs": paragraphs,
                                        "layouts": [], "rotation": page.rotation}))
        finally:
            doc.close()
        stats = compose.compose_split_document(source, str(mono), plans, str(self.output))
        result = self.pdfjs_content()
        original = self.pdfjs_content(source)
        translated = self.pdfjs_content(mono)
        self.assertEqual(result["version"], "5.7.0")
        self.assertEqual(len(result["pages"]), sum(max(1, p.columns) for p in plans))
        self.assertEqual(len(result["layout"]["pages"]), len(result["pages"]))
        expected = {}
        actual = {}

        def add(target, key, text):
            target.setdefault(key, Counter()).update(c for c in text if not c.isspace())

        omitted = {"源英文跨栏（仅使用 mono）": 0, "mono 页眉页脚（仅使用 source）": 0}
        duplicated_headers = 0
        for plan, en_items, zh_items in zip(plans, original["pages"], translated["pages"]):
            frames = [fitz.Rect() for _ in range(plan.columns)]
            for region in plan.regions:
                if region.kind == "column":
                    frames[region.column] |= fitz.Rect(region.box)
            for language, items in (("en", en_items), ("zh", zh_items)):
                for item in items:
                    text = item["str"]
                    if not plan.columns:
                        add(expected, (plan.source_index, "a", language), text)
                        continue
                    # 独立于合成模块的输入归属判定：pdf.js 行的基线/横向范围与冻结计划。
                    y = 792 - item["transform"][5] - 0.3 * (item["top"] - item["bottom"])
                    header = next((r for r in plan.regions if r.kind == "header_footer"
                                   and r.box[1] - 0.1 <= y <= r.box[3] + 0.1), None)
                    span = next((r for r in plan.regions if r.kind == "spanning"
                                 and r.box[1] - 0.1 <= y <= r.box[3] + 0.1), None)
                    if header:
                        if language == "en":
                            duplicated_headers += (plan.columns - 1) * len(re.sub(r"\s+", "", text))
                            for column in range(plan.columns):
                                add(expected, (plan.source_index, "ab"[column], "hf"), text)
                        else:
                            omitted["mono 页眉页脚（仅使用 source）"] += len(re.sub(r"\s+", "", text))
                    elif span:
                        if language == "zh":
                            part = "b" if span.position == "bottom" and plan.columns == 2 else "a"
                            add(expected, (plan.source_index, part, "span"), text)
                        else:
                            omitted["源英文跨栏（仅使用 mono）"] += len(re.sub(r"\s+", "", text))
                    else:
                        x = item["transform"][4]
                        overlaps = [max(0, min(x + item["width"], b.x1) - max(x, b.x0)) for b in frames]
                        column = max(range(plan.columns), key=lambda c: overlaps[c])
                        add(expected, (plan.source_index, "ab"[column], language), text)
        for items, entry in zip(result["pages"], result["layout"]["pages"]):
            self.assert_disjoint_regions(entry)
            order = []
            for item in items:
                kind = self.item_kind(item, entry)
                add(actual, (entry["src"], entry["part"], kind), item["str"])
                order.append({"hf": 0, "span": 1, "en": 2, "zh": 3}[kind])
                if kind == "en":
                    self.assertNotRegex(item["str"], r"[\u4e00-\u9fff]")
            self.assertEqual(order, sorted(order), (entry["src"], entry["part"]))
        for key in sorted(expected.keys() | actual.keys()):
            want, got = expected.get(key, Counter()), actual.get(key, Counter())
            self.assertEqual(got, want, (key, {"缺字": dict(want - got), "多字或串栏": dict(got - want)}))
        print("P05 全 13 页逐字守恒：", json.dumps(stats, ensure_ascii=False))
        for plan in plans:
            counts = {f"{part}.{kind}": f"{sum(expected[key].values())}/{sum(actual[key].values())}"
                      for key in sorted(expected) for src, part, kind in [key] if src == plan.source_index}
            print(f"P05 第 {plan.source_index + 1:02d} 页，输入/输出字符数：",
                  json.dumps(counts, ensure_ascii=False))
        omitted["双栏页原文页眉页脚在 B 页额外保留一份"] = duplicated_headers
        print("允许差异：忽略 pdf.js 合成的空白与换行；", json.dumps(omitted, ensure_ascii=False))
        doc = fitz.open(self.output)
        try:
            self.assertEqual(len(doc), stats["output_pages"])
            all_text = "\n".join(page.get_text() for page in doc)
            a = doc[0].get_text()
            for email in ("max.landauer@ait.ac.at", "florian.skopik@ait.ac.at",
                          "markus.wurzenberger@ait.ac.at"):
                self.assertEqual(a.count(email), 1)
                self.assertEqual(all_text.count(email), 1)
            self.assertEqual(all_text.count("Austrian Institute of Technology"), 3)
            self.assertEqual(all_text.count("Vienna, Austria"), 3)
            self.assertNotRegex(all_text, r"(?m)^\s*(?:florian\.skopi|Vienna,)\s*$")
            self.assertIn("用于多步骤攻击分析的新型警报数据集", a)
            metadata = self.layout(doc)
            for page, entry in zip(doc, metadata["pages"]):
                # 左侧正文不包括横跨整页的中文标题。
                for region in entry["regions"]:
                    if region["k"] == "en":
                        rect = fitz.Rect(region["r"]) * page.transformation_matrix
                        self.assertNotRegex(page.get_text(clip=rect), r"[\u4e00-\u9fff]")
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
