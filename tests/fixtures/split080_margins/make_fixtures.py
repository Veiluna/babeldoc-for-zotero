"""从本机样本生成脱正文的 T3 夹具；不复制 PDF。

运行：babeldoc-env/python.exe tests/fixtures/split080_margins/make_fixtures.py
可传样本目录作为唯一参数。rawdict 不排序，保留原始浮点数，不舍入基线。
行数组为 [x0,y0,x1,y1,baseline,text_or_length]；正文只存长度，候选存文本。
候选为人工核实的页眉/页码基线和 IEEE 下载戳，不调用 classify_margins
来选择候选或生成期望值。保留所有正文行框，便于检验 FoBa 第 17 页。
"""

from pathlib import Path
import json
import sys

import pymupdf

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "addon/content/python"))
from ztf_split_types import Line
from ztf_split_margins import read_pdf_evidence

SAMPLE_ROOT = Path("G:/ProgramData/zotero-translate-files/ztest/samples/0.8.0")
# 仅用于选择可入库文本的人工样本标注，不是产品的页边坐标阈值。
# 数字来自 PyMuPDF 1.28.2 的原始 origin，绝不放宽或舍入。
CANDIDATE_BASELINES = {
    "foba": {36.1309814453125},
    "chen2026": {36.1309814453125},
    "P01": {62.33709716796875, 753.7186279296875},
    "P05": {67.34698486328125, 763.0},
}


def extract_lines(pdf_path):
    """直接把 rawdict 行变成 Line；与 T2 的切行实现无依赖。"""
    document = pymupdf.open(pdf_path)
    pages = []
    heights = []
    try:
        for page in document:
            lines = []
            flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            for block in page.get_text("rawdict", flags=flags, sort=False)["blocks"]:
                for item in block.get("lines", ()):
                    spans = item["spans"]
                    text = "".join(char["c"] for span in spans for char in span["chars"])
                    if not text.strip():
                        continue
                    x0, y0, x1, y1 = item["bbox"]
                    index = len(lines)
                    lines.append(Line(page.number, index, (), index, index,
                                      x0, x1, y0, y1, spans[0]["origin"][1], text, ()))
            pages.append(lines)
            heights.append(page.rect.height)
        return pages, heights
    finally:
        document.close()


def main():
    sample_root = Path(sys.argv[1]) if len(sys.argv) > 1 else SAMPLE_ROOT
    for name, baselines in CANDIDATE_BASELINES.items():
        path = sample_root / (name + ".pdf")
        pages, heights = extract_lines(path)
        tagged, appended = read_pdf_evidence(str(path))
        rows = []
        for lines in pages:
            rows.append([[item.x0, item.y0, item.x1, item.y1, item.baseline,
                          item.text if (item.baseline in baselines or
                                        item.text.startswith("Authorized licensed use limited to:"))
                          else len(item.text)] for item in lines])
        fixture = {"source": name + ".pdf", "pymupdf": pymupdf.VersionBind,
                   "heights": heights, "pages": rows,
                   "tagged": tagged, "appended": appended}
        output = Path(__file__).parent / (name + ".json")
        output.write_text(json.dumps(fixture, ensure_ascii=False, separators=(",", ":")) + "\n",
                          encoding="utf-8")
        print(f"{output.name}: {len(pages)} pages, {sum(map(len, rows))} lines, {output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
