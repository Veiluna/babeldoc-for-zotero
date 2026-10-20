"""通过真实 BabelDOC 进程、模拟接口和最终 PDF 验证行内公式。

使用已安装 BabelDOC 的 Python 直接运行；夹具完全合成，不含用户论文。
诊断产物保存在 build/inline-formulas/，不调用真实翻译服务。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import unittest

import pymupdf

os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path.home() / ".cache/babeldoc/tiktoken"))
sys.path.insert(0, str(Path(__file__).parent))
from mock_openai_server import (
    MockOpenAIHandler, MockOpenAIServer, _prompt_items, _synthetic_translation,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(os.environ.get("ZTF_FORMULA_TEST_RUNNER") or
              ROOT / "addon/content/python/ztf_babeldoc_runner.py")
ARTIFACTS = Path(os.environ.get("ZTF_FORMULA_TEST_ARTIFACTS") or ROOT / "build/inline-formulas")
FORMULAS = ("n2+1", "Nf", "O(n2)", "m=n2+1", "m=-n2+1", "m=+n2+1", "(n2+1)")


class FormulaHandler(MockOpenAIHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        items = _prompt_items(payload)
        self.server.count_request()
        self.server.inputs.extend(items)
        outputs = []
        for index, item in enumerate(items):
            translated = _synthetic_translation(item["input"], index)
            # 模拟译者把整块公式移到中文句中；裸露的英文字母作为正文被翻译。
            marker = re.search(r"[<{]", translated)
            if marker:
                plain, tokens = translated[:marker.start()], translated[marker.start():]
                middle = len(plain) // 2
                translated = plain[:middle] + tokens + plain[middle:]
            outputs.append({"id": item["id"], "output": translated})
        self._send_json(200, {
            "id": "formula-mock", "object": "chat.completion", "created": 0,
            "model": "mock-model",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": json.dumps(outputs, ensure_ascii=False),
            }, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
        })


def write_fixture(path):
    doc = pymupdf.open()
    for page_index, formula in enumerate(FORMULAS):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "The proposed method uses a compact representation.",
                         fontname="tiro", fontsize=10)
        if page_index == 6:
            page.insert_text((60, 114), "We choose a ", fontname="body",
                             fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
            x = 60 + pymupdf.get_text_length("We choose a ", fontname="tiro", fontsize=10)
        else:
            # 两种词间距使 BabelDOC 的 dummy-space 启发式遗漏较窄的那个，复现 Chen。
            page.insert_text((60, 114), "We", fontname="body",
                             fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
            word_x = 65 + pymupdf.get_text_length("We", fontname="tiro", fontsize=10)
            page.insert_text((word_x, 114), "use", fontname="body",
                             fontfile=r"C:\Windows\Fonts\times.ttf", fontsize=10)
            x = 62.5 + pymupdf.get_text_length("We use ", fontname="tiro", fontsize=10)
        for char in formula:
            script = char in "2f"
            # 一元运算符用例刻意使用普通正文字体，不能依赖数学字体替解析器兜底。
            font = "tiit" if page_index < 4 and char in "nNOmf" else "tiro"
            size = 7 if script else 10
            y = (110.4 if char == "2" else 115.5) if script else 114
            if page_index in (4, 5):
                # 标准正文 Times-Roman 的 '-' 映射为 U+002D；某些嵌入字体映射为 U+00AD。
                page.insert_text((x, y), char, fontname="tiro", fontsize=size)
            else:
                page.insert_text((x, y), char,
                                 fontname="math" if font == "tiit" else "body",
                                 fontfile=(r"C:\Windows\Fonts\timesi.ttf" if font == "tiit"
                                           else r"C:\Windows\Fonts\times.ttf"), fontsize=size)
            x += pymupdf.get_text_length(char, fontname=font, fontsize=size)
        suffix = " representation." if page_index == 6 else " categories to describe the entire process."
        page.insert_text((x, 114), suffix,
                         fontname="tiro", fontsize=10)
        page.insert_text((60, 128), "This representation preserves the meaning of each observation.",
                         fontname="tiro", fontsize=10)
        page.insert_text((200, 220), "x + y = 1", fontname="display",
                         fontfile=r"C:\Windows\Fonts\CascadiaMono.ttf", fontsize=10)
    doc.save(path)
    doc.close()


def glyphs(page):
    return [dict(char, size=span["size"], font=span["font"])
            for block in page.get_text("rawdict")["blocks"]
            for line in block.get("lines", []) for span in line["spans"]
            for char in span["chars"] if not char["c"].isspace()]


def run_translation(source, destination, runner=RUNNER, pages=None):
    destination.mkdir(parents=True, exist_ok=True)
    server = MockOpenAIServer(("127.0.0.1", 0), FormulaHandler)
    server.inputs = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("ZTF_") and "API_KEY" not in key.upper()}
    environment.update({
        "ZTF_OPENAI_API_KEY": "mock-test-key", "ZTF_MAX_OUTPUT_TOKENS": "8192",
        "ZTF_MAX_REQUESTS": "20", "ZTF_REPAIR_FAILED": "1",
        "ZTF_TRANSLATE_REFERENCES": "0", "PYTHONIOENCODING": "utf-8",
        "ZTF_JOB_LOG_PATH": str(destination / "job.jsonl"),
    })
    command = [sys.executable, str(runner), "--files", str(source),
               "--output", str(destination / "output"),
               "--working-dir", str(destination / "work"),
               "--openai", "--openai-model", "mock-model", "--openai-base-url",
               f"http://127.0.0.1:{server.server_address[1]}/v1",
               "--lang-in", "en", "--lang-out", "zh", "--no-dual",
               "--no-auto-extract-glossary", "--disable-same-text-fallback",
               "--watermark-output-mode", "no_watermark",
               "--skip-scanned-detection"]
    if pages:
        command.extend(["--pages", pages])
    try:
        completed = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=600)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    (destination / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (destination / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    (destination / "inputs.json").write_text(json.dumps(server.inputs, ensure_ascii=False,
                                                      indent=2), encoding="utf-8")
    match = re.search(r"(?m)^ZTF_RESULT (\{.*\})\s*$", completed.stdout)
    result = json.loads(match.group(1)) if match else None
    return completed, result, server.inputs


class InlineFormulaIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.source = ARTIFACTS / "source.pdf"
        write_fixture(cls.source)
        # 模拟插件从 XPI 解出资源后，在 addon 目录之外启动 runner。
        deployed = ARTIFACTS / "deployed"
        deployed.mkdir(exist_ok=True)
        shutil.copyfile(RUNNER, deployed / RUNNER.name)
        for helper_name in ("ztf_inline_formulas.py", "ztf_style_guards.py"):
            helper = ROOT / "addon/content/python" / helper_name
            shutil.copyfile(helper, deployed / helper_name)
        cls.completed, cls.result, cls.inputs = run_translation(
            cls.source, ARTIFACTS, runner=deployed / RUNNER.name)

    def output(self):
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-4000:])
        self.assertIsNotNone(self.result, self.completed.stdout[-4000:])
        self.assertTrue(self.inputs, "没有经过模拟接口")
        return pymupdf.open(self.result["mono"])

    def assert_formula_geometry(self, page_number, formula, start=None):
        with pymupdf.open(self.source) as original, self.output() as translated:
            before = [c for c in glyphs(original[page_number])
                      if 108 < c["origin"][1] < 117 and c["c"] in formula]
            # 原文只取紧挨 We use 后的数学区域，排除两侧正文。
            if start is None:
                start = 62.5 + pymupdf.get_text_length("We use ", fontname="tiro", fontsize=10)
            before = [c for c in before if start - .1 <= c["origin"][0] < start + 45]
            before = before[:len(formula)]
            self.assertEqual("".join(c["c"] for c in before), formula)
            after_all = glyphs(translated[page_number])
            candidates = [c for c in after_all if c["c"] == formula[0]]
            self.assertTrue(candidates, f"译文缺少完整公式的底数 {formula!r}")
            anchor = min(candidates, key=lambda c: c["origin"][1])
            after = [anchor]
            for source_char in before[1:]:
                dx = (source_char["origin"][0] - before[0]["origin"][0]) / before[0]["size"]
                dy = (source_char["origin"][1] - before[0]["origin"][1]) / before[0]["size"]
                matches = [c for c in after_all if c["c"] == source_char["c"]
                           and abs((c["origin"][0] - anchor["origin"][0]) / anchor["size"] - dx) < .06
                           and abs((c["origin"][1] - anchor["origin"][1]) / anchor["size"] - dy) < .06]
                self.assertTrue(matches, f"{formula!r} 的 {source_char['c']!r} 相对位置被破坏")
                self.assertAlmostEqual(matches[0]["size"] / anchor["size"],
                                       source_char["size"] / before[0]["size"], delta=.02)
                after.append(matches[0])
            self.assertTrue(re.search(r"[\u3400-\u9fff]", translated[page_number].get_text()),
                            "公式必须进入实际中文译句")
            chinese = [c for c in after_all if "\u3400" <= c["c"] <= "\u9fff"
                       and abs(c["origin"][1] - anchor["origin"][1]) < anchor["size"]]
            self.assertTrue(any(c["origin"][0] < anchor["origin"][0] for c in chinese))
            self.assertTrue(any(c["origin"][0] > after[-1]["origin"][0] for c in chinese))

    def test_base_superscript_and_adjacent_sum_move_together(self):
        self.assert_formula_geometry(0, "n2+1")

    def test_subscript_stays_below_its_base(self):
        self.assert_formula_geometry(1, "Nf")

    def test_parenthesized_complexity_is_preserved(self):
        self.assert_formula_geometry(2, "O(n2)")

    def test_adjacent_left_operator_and_operand_join_the_atom(self):
        self.assert_formula_geometry(3, "m=n2+1")

    def test_unary_signs_in_body_font_stay_inside_the_complete_formula(self):
        for page_number, formula in ((4, "m=-n2+1"), (5, "m=+n2+1")):
            with self.subTest(formula=formula):
                self.assert_formula_geometry(page_number, formula)

    def test_article_before_parenthesis_is_translated_as_prose(self):
        with self.output() as translated:
            self.assertNotIn("a", [c["c"] for c in glyphs(translated[6])],
                             "冠词 a 被误吞进公式，在最终 PDF 中未经翻译保留下来")
        start = 60 + pymupdf.get_text_length("We choose a ", fontname="tiro", fontsize=10)
        self.assert_formula_geometry(6, "(n2+1)", start=start)
        article_inputs = [item["input"] for item in self.inputs if "We choose" in item["input"]]
        self.assertEqual(len(article_inputs), 1)
        self.assertRegex(article_inputs[0], r"We choose a\s*\{v\d+\}")
        self.assertIn("representation.", article_inputs[0])

    def test_display_formulas_and_ordinary_prose_are_preserved(self):
        with pymupdf.open(self.source) as original, self.output() as translated:
            for index in range(len(FORMULAS)):
                before = [c for c in glyphs(original[index]) if 215 < c["origin"][1] < 225]
                after = [c for c in glyphs(translated[index]) if 215 < c["origin"][1] < 225]
                self.assertEqual("".join(c["c"] for c in after), "x+y=1")
                for source_char, output_char in zip(before, after):
                    for a, b in zip(source_char["origin"], output_char["origin"]):
                        self.assertAlmostEqual(a, b, delta=.02)
        sources = [item["input"] for item in self.inputs]
        body = [source for source in sources if "compact representation" in source][:4]
        self.assertEqual(len(body), 4)
        for source in body:
            self.assertIn("categories to describe the entire process", source)
            self.assertIn("This representation preserves the meaning of each observation", source)
            self.assertNotRegex(source, r"(?<![A-Za-z])[nNOmf](?![A-Za-z])")
            self.assertEqual(len(re.findall(r"\{v\d+\}", source)), 1,
                             "整式只能成为一个原子，不能拆成多个可移动片段")


if __name__ == "__main__":
    unittest.main()
