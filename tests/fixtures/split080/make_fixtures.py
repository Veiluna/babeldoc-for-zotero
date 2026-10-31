"""从授权的本地样本生成 T2 脱文夹具；只读 PDF，不复制源 PDF。

运行：python tests/fixtures/split080/make_fixtures.py <样本目录>
每页 JSON.gz 记录 source、page（0 起算）、width、height、pymupdf_version、
chars；chars 每项为 [x0,y0,x1,y1,paragraph]，数组下标即 id/order，
sub_order 固定 0，text 固定占位 x。坐标不舍入，保留原始浮点值。
PyMuPDF rawdict 的 block.number 代替段落下标，未排序的遍历顺序代替 IL 顺序。
失败直接抛出异常，所有已打开的 Document 均在 finally 关闭。
"""

import argparse
import gzip
import json
from pathlib import Path

import pymupdf


# 验收页码（1 起算），不是算法常量；仅抽取正文回归所需的九页。
SAMPLES = {"chen2026.pdf": (1, 10, 18), "foba.pdf": (1, 13, 16, 17, 19),
           "P01.pdf": (1, 2, 3), "P05.pdf": (1, 2, 4)}


def make_fixtures(source: Path) -> None:
    """将九页几何写入本脚本所在目录，无正文、字体、图片或本机绝对路径。"""
    destination = Path(__file__).resolve().parent
    for filename, numbers in SAMPLES.items():
        document = pymupdf.open(source / filename)
        try:
            for number in numbers:
                page = document[number - 1]
                raw = page.get_text("rawdict", sort=False)
                chars = [list(char["bbox"]) + [block["number"]]
                         for block in raw["blocks"] if block["type"] == 0
                         for line in block["lines"] for span in line["spans"]
                         for char in span["chars"]]
                payload = {"source": filename, "page": number - 1,
                           "width": page.rect.width, "height": page.rect.height,
                           "pymupdf_version": pymupdf.__version__, "chars": chars}
                # 人工核对的 rawdict block.number，仅存下标，不存论文/作者文字。
                if filename == "P05.pdf" and number == 1:
                    payload["author_paragraphs"] = [1, 2, 3, 4, 5, 6]
                if filename == "foba.pdf" and number in (13, 16):
                    payload["caption_paragraphs"] = [89 if number == 13 else 15]
                    payload["margin_paragraphs"] = [0, 98 if number == 13 else 24]
                encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
                # mtime=0 消除生成时间；gzip 自身默认压缩级别，不是认栏参数。
                target = destination / f"{Path(filename).stem}-p{number:02d}.json.gz"
                target.write_bytes(gzip.compress(encoded, mtime=0))
                print(f"{target.name}: {len(chars)} chars, {target.stat().st_size} bytes")
        finally:
            document.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="含四份源 PDF 的本地目录")
    make_fixtures(parser.parse_args().source)
