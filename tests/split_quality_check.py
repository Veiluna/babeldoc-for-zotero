#!/usr/bin/env python3
"""只读的分栏双语 PDF 门禁；不调用翻译 API。

坐标证据均为 PyMuPDF 左上原点 pt（元数据原值除外）。--json 输出完整证据。
如有 mono.layout.json，复用公开 planner 解释页数及区域，按 output_box
定位单语内容；跨栏完整性、份数、正文副本和导航锚点均从输入建立基准，
与输出 PDF 文本、几何和落点独立核对。输入缺失或核对失败时返回 FAIL。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata

sys.dont_write_bytecode = True
import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
RANK = {"hf": 0, "span": 1, "en": 2, "zh": 3}
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]")
TOL = 0.2  # pt：仅容忍 PDF 浮点变换误差，不容忍额外字符。
LAYOUT_MARGIN = 12.0  # pt：分栏合成的两侧外边距及栏间距。


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def box_list(box):
    return [round(float(v), 2) for v in box]


def contains(box, point):
    return box.x0 - TOL <= point.x <= box.x1 + TOL and box.y0 - TOL <= point.y <= box.y1 + TOL


def center(box):
    return fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)


def lines(page):
    result = []
    for block in page.get_text("rawdict", sort=False)["blocks"]:
        for line in block.get("lines", []):
            chars = [c for s in line["spans"] for c in s["chars"]]
            result.append({"text": "".join(c["c"] for c in chars),
                           "box": fitz.Rect(line["bbox"]), "chars": chars})
    return result


def kind_at(point, regions):
    kinds = {k for k, box in regions if contains(box, point)}
    return next(iter(kinds)) if len(kinds) == 1 else "unknown"


def load_metadata(doc, source):
    typ, info = doc.xref_get_key(-1, "Info")
    if typ != "xref":
        raise ValueError("PDF 无 Info 字典")
    typ, value = doc.xref_get_key(int(info.split()[0]), "ZTFSplitLayout")
    if typ != "string":
        raise ValueError("未读出字符串 ZTFSplitLayout")
    layout = json.loads(value)
    if layout.get("v") != 1 or len(layout.get("pages", [])) != len(doc):
        raise ValueError("元数据版本或页数不符")
    converted = []
    for n, entry in enumerate(layout["pages"]):
        if type(entry.get("src")) is not int or not 0 <= entry["src"] < len(source):
            raise ValueError(f"输出页 {n + 1} 的 src 无效")
        if entry.get("part") not in ("a", "b") or not isinstance(entry.get("fallback", False), bool):
            raise ValueError(f"输出页 {n + 1} 的 part/fallback 无效")
        regions = []
        for r in entry.get("regions", []):
            values = r.get("r", [])
            if r.get("k") not in RANK or len(values) != 4 or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
                raise ValueError(f"输出页 {n + 1} 的区域无效")
            rect = fitz.Rect(values) * doc[n].transformation_matrix
            bounds = doc[n].rect + (-TOL, -TOL, TOL, TOL)
            if rect.is_empty or not bounds.contains(rect):
                raise ValueError(f"输出页 {n + 1} 的区域超出页面 {box_list(rect)}")
            if any(k != r["k"] and (rect & b).get_area() > TOL for k, b in regions):
                raise ValueError(f"输出页 {n + 1} 的不同类别区域重叠")
            regions.append((r["k"], rect))
        if not regions:
            raise ValueError(f"输出页 {n + 1} 没有区域")
        converted.append(regions)
    return layout, converted


def author_lines(source_lines, layout_page):
    """样本锚点来自原文，故不会因 runner 漏标 author 而漏检。"""
    text = "\n".join(l["text"] for l in source_lines)
    if "max.landauer@ait.ac.at" in text:
        expected = {"Max Landauer", "Florian Skopik", "Markus Wurzenberger",
                    "max.landauer@ait.ac.at", "florian.skopik@ait.ac.at", "markus.wurzenberger@ait.ac.at",
                    "Austrian Institute of Technology", "Vienna, Austria"}
        return [l for l in source_lines if l["text"].strip() in expected]
    if "Iman Sharafaldin" in text:
        return [l for l in source_lines if "Iman Sharafaldin" in l["text"] or
                "Canadian Institute for Cybersecurity" in l["text"]]
    marked = [fitz.Rect(p["box"]) for p in (layout_page or {}).get("paragraphs", []) if p.get("author")]
    if marked:
        return [l for l in source_lines if any(contains(b, center(l["box"])) for b in marked)]
    # 通用后备：首页上半部的邮箱/机构行，以及其正上方姓名行。
    anchors = [i for i, l in enumerate(source_lines) if l["box"].y0 < 350 and
               re.search(r"@|\b(?:University|Institute|Laboratory)\b", l["text"], re.I)]
    indexes = {j for i in anchors for j in (max(0, i - 1), i)}
    return [source_lines[i] for i in sorted(indexes)]


def mono_author_lines(authors, mono_page, layout_page):
    """用原文锚点定位对应的单语段落，返回完整 PDF 行及无法定位的区域。

    不依赖 author 标记或 planner；output_box 只负责定位，期望文本来自单语 PDF。
    无 sidecar 时用原作者行框，不能退回匹配全文中的任意姓名。
    """
    paragraphs = (layout_page or {}).get("paragraphs", [])
    boxes = []
    for line in authors:
        matches = [p for p in paragraphs if contains(fitz.Rect(p["box"]), center(line["box"]))]
        boxes.extend(fitz.Rect(p.get("output_box", p["box"])) for p in matches)
        if not matches:
            boxes.append(line["box"])
    rows = lines(mono_page)
    selected = [l for l in rows if compact(l["text"]) and
                any(contains(b, center(l["box"])) for b in boxes)]
    missing = [box_list(b) for b in boxes if not any(contains(b, center(l["box"])) for l in selected)]
    return selected, missing


def ordinary_author_mentions(source_lines, mono_lines, authors, reference, entries, regions, grouped, widths):
    """识别输入已有的非作者提及，严格按源页、输出 part、侧和完整行配额核对。

    行结构同 lines()，grouped 为逐页/类别的输出行；返回可排除的输出行 id
    及逐行证据。不豁免作者基准整行；hf 每个输出页取源行一份，正文行仅在
    输入所在半页对应的 en/zh 各出现一次。新增或异侧副本不能复用配额。
    """
    def identity(line):
        return compact(line["text"]), tuple(line["box"])

    wanted = {compact(l["text"]) for l in reference}
    phrases = wanted | {compact(l["text"]) for l in authors}
    excluded = [{identity(l) for l in authors}, {identity(l) for l in reference}]
    allowance = Counter()
    for n, entry in enumerate(entries):
        src = entry["src"]
        split = any(e["src"] == src and e["part"] == "b" for e in entries)
        hf = [b for k, b in regions[n] if k == "hf"]
        for side, pages, omit in (("en", source_lines, excluded[0]), ("zh", mono_lines, excluded[1])):
            if pages is None:
                continue
            for line in pages[src]:
                text = compact(line["text"])
                if ((src == 0 and identity(line) in omit) or text in wanted
                        or not any(p in text for p in phrases)):
                    continue
                if any(b.y0 - TOL <= center(line["box"]).y <= b.y1 + TOL for b in hf):
                    if side == "en":
                        allowance[n, "hf", text] += 1
                    continue
                part = "b" if split and center(line["box"]).x > widths[src] / 2 else "a"
                if entry["part"] == part:
                    allowance[n, side, text] += 1
    accepted, evidence = set(), []
    for n, groups in enumerate(grouped):
        for kind, items in groups.items():
            for item in items:
                key = n, kind, compact(item["text"])
                if allowance[key] > 0:
                    allowance[key] -= 1
                    accepted.add(id(item))
                    evidence.append({"kind": kind, "existing_non_author_line": item})
    return accepted, evidence


def pdfjs_order(path, layout):
    directory = ROOT / "build/research/omni/resource/reader/pdf/build"
    if not shutil.which("node") or not all((directory / n).is_file() for n in ("pdf.mjs", "pdf.worker.mjs")):
        return False, {"reason": "missing_dependency", "count": 1,
                       "description": "缺少 Node 或 Zotero pdf.js/worker；未完成 pdf.js 验证"}
    # 与 acceptance_split_compose.py 相同的本机 pdf.js 5.7.0 加载方式。
    script = r"""
import fs from 'node:fs';
globalThis.DOMMatrix = class {}; globalThis.Path2D = class {}; globalThis.ImageData = class {};
Uint8Array.prototype.toHex ??= function() { return Buffer.from(this).toString('hex'); };
const pdfjs = await import(process.argv[1] + '/pdf.mjs');
pdfjs.GlobalWorkerOptions.workerSrc = process.argv[1] + '/pdf.worker.mjs';
const task = pdfjs.getDocument({data:new Uint8Array(fs.readFileSync(process.argv[2])),useSystemFonts:false});
try {
  const doc = await task.promise; const meta = await doc.getMetadata(); const pages = [];
  for (let i=1;i<=doc.numPages;i++) {
    const page = await doc.getPage(i); const content = await page.getTextContent();
    pages.push(content.items.filter(x=>x.str?.trim()).map(x=>({text:x.str,
      point:[x.transform[4]+x.width/2,x.transform[5]+x.height*0.3]})));
  }
  console.log('ZTFRESULT:'+JSON.stringify({version:pdfjs.version,layout:meta.info.Custom?.ZTFSplitLayout,pages}));
} finally { await task.destroy(); }
"""
    completed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script,
                                directory.as_uri(), str(path)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
    values = [l[len("ZTFRESULT:"):] for l in completed.stdout.splitlines() if l.startswith("ZTFRESULT:")]
    if completed.returncode or len(values) != 1:
        return False, {"description": "pdf.js 执行失败", "reason": "pdfjs_failed",
                       "returncode": completed.returncode, "count": 1}
    result = json.loads(values[0])
    errors = []
    for n, items in enumerate(result["pages"]):
        previous = -1
        regions = [(r["k"], fitz.Rect(r["r"])) for r in layout["pages"][n]["regions"]]
        for item in items:
            kind = kind_at(fitz.Point(item["point"]), regions)
            rank = RANK.get(kind, -1)
            if rank < previous or kind == "unknown":
                errors.append({"page": n + 1, "kind": kind, "previous_rank": previous, **item})
            previous = rank
    metadata_equal = bool(result.get("layout")) and json.loads(result["layout"]) == layout
    return not errors and metadata_equal, {"version": result["version"], "metadata_equal": metadata_equal, "errors": errors}


def link_key(doc, link, entries=None):
    if link["kind"] == fitz.LINK_URI:
        return ("uri", link.get("uri", ""))
    if link["kind"] in (fitz.LINK_GOTO, fitz.LINK_NAMED):
        page = link.get("page", -1)
        if link.get("nameddest"):
            page = doc.resolve_link("#nameddest=" + link["nameddest"])[0]
        if entries is not None and 0 <= page < len(entries):
            page = entries[page]["src"]
        return ("goto", page)
    return ("kind", link["kind"])


def build_report(source_path, dual_path, mono_path=None):
    report = {"source": str(source_path), "dual": str(dual_path), "mono": str(mono_path or ""),
              "coordinates": "左上原点 pt；页号从 1 开始", 'pymupdf_version': fitz.VersionBind, "checks": []}

    def add(key, name, passed, evidence):
        report["checks"].append({"id": key, "name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence})

    with ExitStack() as stack:
        source = stack.enter_context(fitz.open(source_path))
        dual = stack.enter_context(fitz.open(dual_path))
        mono = stack.enter_context(fitz.open(mono_path)) if mono_path else None
        try:
            metadata, regions = load_metadata(dual, source)
        except (ValueError, KeyError, TypeError) as error:
            add("metadata", "ZTFSplitLayout 元数据", False, {"errors": str(error)})
            report["status"] = "FAIL"
            return report
        entries = metadata["pages"]
        add("metadata", "ZTFSplitLayout 元数据", True, metadata)
        source_lines = [lines(p) for p in source]
        output_lines = [lines(p) for p in dual]
        grouped = []
        unknown = []
        for n, page_lines in enumerate(output_lines):
            groups = {k: [] for k in RANK}
            for line in page_lines:
                chunks = defaultdict(list)
                for c in line["chars"]:
                    if c["c"].isspace():
                        continue
                    kind = kind_at(center(fitz.Rect(c["bbox"])), regions[n])
                    chunks[kind].append(c)
                for kind, chars in chunks.items():
                    text = "".join(c["c"] for c in chars)
                    item = {"page": n + 1, "box": box_list(line["box"]), "text": text}
                    if kind == "unknown":
                        unknown.append(item)
                    else:
                        groups[kind].append(item)
            grouped.append(groups)

        def group_text(n, kind):
            return "".join(compact(x["text"]) for x in grouped[n][kind])

        # 英文区不能混入汉字；原文汉字数只作诊断，没有依据放宽到任意百分比。
        cjk_errors, cjk_counts = [], []
        for n, entry in enumerate(entries):
            got = Counter(CJK.findall(group_text(n, "en")))
            baseline = Counter(CJK.findall("".join(l["text"] for l in source_lines[entry["src"]])))
            cjk_counts.append({"page": n + 1, "cjk_count": sum(got.values()), "source_baseline": sum(baseline.values())})
            if got:
                cjk_errors.extend(x for x in grouped[n]["en"] if CJK.search(x["text"]))
        add("english_cjk", "英文区域无汉字", not cjk_errors,
            {"threshold_basis": "汉字容忍数=0，直接执行英文区无汉字要求；原文汉字只作诊断，不豁免跨栏混入，不设无依据的比例误差", "pages": cjk_counts, "errors": cjk_errors})

        # layout 仅作解释/过滤，不决定文本层和作者是否正确。
        layout_pages, plans = {}, {}
        sidecar = Path(str(mono_path) + ".layout.json") if mono_path else None
        if sidecar and sidecar.is_file():
            layout_pages = {p["index"]: p for p in json.loads(sidecar.read_text(encoding="utf-8"))["pages"]}
            sys.path.insert(0, str(ROOT / "addon/content/python"))
            from ztf_split_plan import plan_page
            plans = {i: plan_page(p) for i, p in layout_pages.items()}

        cross_errors, tested = [], 0
        for n, entry in enumerate(entries):
            if entry.get("fallback") or sum(e["src"] == entry["src"] for e in entries) != 2:
                continue
            src = entry["src"]
            zh = group_text(n, "zh").casefold()
            # 对应单语栏已经存在的英文是允许基准（如专名、公式、表内原文）。
            # 使用规划的栏间隙中点；无 sidecar 时只能按源页中线确定对应栏。
            divider = source[src].rect.width / 2
            column_boxes = {column: [r.box for r in plans[src].regions
                                    if r.kind == "column" and r.column == column]
                            for column in (0, 1)} if src in plans else {}
            if column_boxes.get(0) and column_boxes.get(1):
                divider = (max(b[2] for b in column_boxes[0]) + min(b[0] for b in column_boxes[1])) / 2
            mono_baseline = "".join(compact(l["text"]) for l in lines(mono[src])
                                    if (center(l["box"]).x > divider) == (entry["part"] == "b")).casefold() if mono is not None else ""
            for line in source_lines[src]:
                phrase = compact(line["text"])
                if not re.search(r"[A-Za-z]{2,}", line["text"]) or CJK.search(line["text"]):
                    continue
                box = line["box"]
                other = center(box).x > divider if entry["part"] == "a" else center(box).x < divider
                if not other:
                    continue
                tested += 1
                if zh.count(phrase.casefold()) > mono_baseline.count(phrase.casefold()):
                    cross_errors.append({"page": n + 1, "source_page": src + 1, "source_box": box_list(box),
                                         "source": line["text"], "output": [x for x in grouped[n]["zh"] if phrase.casefold() in compact(x["text"]).casefold()]})
        add("chinese_cross_column", "中文区域无另一栏英文正文", not cross_errors,
            {"matching_rule": "匹配原文另一栏英文整行（含短行）；输出份数不得超过单语对应栏已有份数", "candidate_count": tested,
             "fallback_exempt_pages": [n + 1 for n, e in enumerate(entries) if e.get("fallback")], "errors": cross_errors})

        # 期望带只取输入证据，不能让输出删除文字和 hf 元数据后自行变成“期望零”。
        source_hf_bands = {}
        source_body_boxes = {}
        for src, page_lines in enumerate(source_lines):
            # planner 从 sidecar 的 marginal/版面证据排除正文、图表内的小框。
            # 不能直接把每个 marginal 框扩成整带，否则图内刻度也会被当成页眉。
            bands = [fitz.Rect(r.box) for r in plans[src].regions if r.kind == "header_footer"] if src in plans else []
            body_boxes = [fitz.Rect(p["box"]) for p in layout_pages.get(src, {}).get("paragraphs", [])
                          if p.get("marginal") is False]
            if src in plans:
                body_boxes.extend(fitz.Rect(r.box) for r in plans[src].regions
                                  if r.kind in ("column", "spanning"))
            source_body_boxes[src] = body_boxes
            for line in page_lines:
                if any(contains(b, center(line["box"])) for b in body_boxes):
                    continue
                if not (line["box"].y1 < source[src].rect.height * .08 or
                        line["box"].y0 > source[src].rect.height * .92):
                    continue
                signature = re.sub(r"\d", "#", compact(line["text"]))
                repeated = sum(any(re.sub(r"\d", "#", compact(l["text"])) == signature for l in ls) for ls in source_lines)
                # 单页自匹配不是重复；独立的纯数字边缘页码证据仍须保留。
                if signature and (repeated >= 2 or compact(line["text"]).isdigit()):
                    bands.append(line["box"])
            source_hf_bands[src] = bands

        hf_errors, hf_counts, hf_expected = [], [], {}
        for n, entry in enumerate(entries):
            if entry.get("fallback"):
                continue  # 规格允许整页并排回退；在页数项明示。
            bands = source_hf_bands[entry["src"]]
            expected_lines = [line for line in source_lines[entry["src"]]
                              if not any(contains(b, center(line["box"])) for b in source_body_boxes[entry["src"]])
                              if any(b.y0 - TOL <= center(line["box"]).y <= b.y1 + TOL for b in bands)]
            hf_expected[n] = expected_lines
            expected = Counter("".join(compact(l["text"]) for l in expected_lines))
            actual = Counter(group_text(n, "hf"))
            if expected != actual:
                hf_errors.append({"page": n + 1,
                                  "missing_chars": [{"text": c, "count": count} for c, count in (expected - actual).items()],
                                  "extra_chars": [{"text": c, "count": count} for c, count in (actual - expected).items()]})
            # 元数据不能自行把正文改名为 hf：独立核对 exporter 的非 marginal 段落。
            content_boxes = [fitz.Rect(p["box"]) for p in layout_pages.get(entry["src"], {}).get("paragraphs", [])
                             if p.get("marginal") is not True]
            for line in source_lines[entry["src"]]:
                swallowed = [c for c in line["chars"] if not c["c"].isspace()
                             and any(contains(b, center(fitz.Rect(c["bbox"]))) for b in content_boxes)
                             and any(k == "hf" and contains(b, center(fitz.Rect(c["bbox"])))
                                     for k, b in regions[n])]
                if swallowed:
                    hf_errors.append({"page": n + 1, "non_marginal_text_in_header_footer": "".join(c["c"] for c in swallowed),
                                      "source_box": box_list(line["box"])})
            hf_counts.append({"page": n + 1, "source_char_count": sum(expected.values()), "output_char_count": sum(actual.values()),
                              "expected_source_bands": [box_list(b) for b in bands]})

        authors = author_lines(source_lines[0], layout_pages.get(0))
        reference, missing = (mono_author_lines(authors, mono[0], layout_pages.get(0))
                              if mono is not None else (authors, []))
        wanted = Counter(compact(l["text"]) for l in reference)
        author_fallback = any(e["src"] == 0 and e.get("fallback") for e in entries)
        original_authors = Counter(compact(l["text"]) for l in authors)
        ordinary, ordinary_evidence = ordinary_author_mentions(
            source_lines, [lines(p) for p in mono] if mono is not None else None,
            authors, reference, entries, regions, grouped, [p.rect.width for p in source])
        author_evidence, author_errors = [], []
        if missing:
            author_errors.append({"empty_mono_author_regions": missing})
        author_bands = set()
        accepted_author_items = set()
        phrases = wanted.keys() | original_authors.keys() if author_fallback else wanted.keys()
        for phrase in phrases:
            expected_sides = {"en": original_authors[phrase], "zh": wanted[phrase]} if author_fallback else {"span": wanted[phrase]}
            count = sum(expected_sides.values())
            hits = []
            complete_items = set()
            for n, entry in enumerate(entries):
                # 扫描所有输出页；正常分栏只允许同一 span，整页回退按原文/单语两侧分别计数。
                for kind in RANK:
                    for item in grouped[n][kind]:
                        if id(item) in ordinary:
                            continue
                        actual_text = compact(item["text"])
                        if phrase not in actual_text:
                            continue
                        # 回退两侧的合法整行可互为子串，较长行由其自身的完整期望核对。
                        if author_fallback and actual_text != phrase and (
                                (kind == "en" and actual_text in original_authors) or
                                (kind == "zh" and actual_text in wanted)):
                            continue
                        # 正常分栏必须整行在同一跨栏带；回退必须整行在对应侧的区域内。
                        band = next((i for i, (k, b) in enumerate(regions[n]) if k == kind
                                     and (author_fallback or (k == "span" and b.x0 <= dual[n].rect.width / 2 <= b.x1))
                                     and (b + (-TOL, -TOL, TOL, TOL)).contains(fitz.Rect(item["box"]))), None)
                        hits.append({"page": n + 1, "source_page": entry["src"] + 1, "part": entry["part"], "kind": kind,
                                     "copies": actual_text.count(phrase), "complete_line": actual_text == phrase,
                                     "spanning_region": band})
                        if kind in expected_sides and band is not None:
                            author_bands.add((n, band))
                            if entry["src"] == 0 and entry["part"] == "a" and actual_text == phrase:
                                complete_items.add(id(item))
            good = all(sum(h["copies"] for h in hits if h["source_page"] == 1 and h["part"] == "a"
                           and h["kind"] == kind) == copies for kind, copies in expected_sides.items())
            good = good and all(h["source_page"] == 1 and expected_sides.get(h["kind"], 0) > 0 and h["part"] == "a"
                                and h["spanning_region"] is not None and h["complete_line"] for h in hits)
            baseline = reference + authors if author_fallback else reference
            evidence = {"baseline_text": next(l["text"] for l in baseline if compact(l["text"]) == phrase),
                        "baseline_boxes": [box_list(l["box"]) for l in baseline if compact(l["text"]) == phrase],
                        "expected_copies": count, "expected_sides": expected_sides, "actual": hits}
            author_evidence.append(evidence)
            if not good:
                author_errors.append(evidence)
            else:
                accepted_author_items.update(complete_items)
        if len(author_bands) != (2 if author_fallback else 1):
            author_errors.append({"author_group_bands": sorted(author_bands)})
        # 单语作者已翻译时，也不能在另一侧额外保留一份原姓名/机构行。
        source_only = set() if author_fallback else original_authors.keys() - wanted.keys()
        for n, entry in enumerate(entries):
            for kind in RANK:
                author_errors.extend({"kind": kind, "extra_original_author_copy": item}
                                     for item in grouped[n][kind] if id(item) not in ordinary
                                     and id(item) not in accepted_author_items and any(
                                         phrase in compact(item["text"]) for phrase in source_only))
        # 已知被截断的邮箱/城市残片，以及同一作者高度范围内的栏内副本。
        if reference:
            fragment_baseline = reference + authors if author_fallback else reference
            fragment_phrases = wanted.keys() | original_authors.keys() if author_fallback else wanted.keys()
            top, bottom = min(l["box"].y0 for l in fragment_baseline), max(l["box"].y1 for l in fragment_baseline)
            for n, entry in enumerate(entries):
                if entry["src"] != 0:
                    continue
                for kind in ("en", "zh"):
                    for item in grouped[n][kind]:
                        b = fitz.Rect(item["box"])
                        if (id(item) not in ordinary and not (author_fallback and id(item) in accepted_author_items)
                                and b.y0 < bottom and b.y1 > top and any(
                                    compact(item["text"]) in phrase and len(compact(item["text"])) >= 5 for phrase in fragment_phrases)):
                            author_errors.append({"kind": kind, "fragment_or_copy": item})
        spans = [{"page": n + 1, "box": box_list(b)} for n, rs in enumerate(regions)
                 if entries[n]["src"] == 0 for k, b in rs if k == "span" and
                 b.x0 <= dual[n].rect.width / 2 <= b.x1]
        add("authors", "首页作者机构完整且份数符合分栏或整页回退规则", bool(wanted) and not author_errors,
            {"description": "按原文作者锚点定位单语对应区域；正常分栏须在首页 A 同一跨栏带且只一次；首页整页回退须在原文侧、单语侧各完整一组",
             "fallback": author_fallback,
             "baseline": "mono" if mono is not None else "source",
             "source_anchors": [{"text": l["text"], "box": box_list(l["box"])} for l in authors],
             "existing_non_author_mentions": ordinary_evidence,
             "lines": author_evidence, "spanning_boxes": spans, "errors": author_errors,
             "unrecognized_authors": not bool(wanted)})

        mapping = defaultdict(list)
        for n, entry in enumerate(entries):
            mapping[entry["src"]].append((n, entry))
        fallback_sources = {src for src, items in mapping.items()
                            if any(e.get("fallback") for _, e in items)}
        page_errors, page_details = [], []
        for src in range(len(source)):
            items = mapping[src]
            parts = [e["part"] for _, e in items]
            fallback = any(e.get("fallback") for _, e in items)
            valid = parts in (["a"], ["a", "b"]) and not (fallback and len(items) != 1)
            if src in plans:
                valid &= fallback or (plans[src].columns > 0 and len(items) == plans[src].columns)
            for n, _ in items:
                valid &= abs(dual[n].rect.height - source[src].rect.height) < .1
                valid &= dual[n].rect.width + .1 >= source[src].rect.width
                if fallback:
                    valid &= abs(dual[n].rect.width - 2 * source[src].rect.width) < .1
                    # 回退必须是完整原文/单语两半，不能仅靠 fallback 标记豁免栏数。
                    expected_regions = [("en", fitz.Rect(0, 0, source[src].rect.width, source[src].rect.height)),
                                        ("zh", fitz.Rect(source[src].rect.width, 0,
                                                         2 * source[src].rect.width, source[src].rect.height))]
                    valid &= len(regions[n]) == 2 and all(
                        actual_kind == expected_kind and all(abs(a - b) < .1 for a, b in zip(actual, expected))
                        for (actual_kind, actual), (expected_kind, expected) in zip(regions[n], expected_regions))
            detail = {"source_page": src + 1, "output_pages": [n + 1 for n, _ in items], "parts": parts, "fallback": fallback,
                      "planned_columns": plans[src].columns if src in plans else "无 layout sidecar，仅核对结构",
                      "fallback_stage": ("planning" if plans[src].columns == 0 else "composition")
                      if fallback and src in plans else ("unknown" if fallback else None)}
            # 防止与 planner 一起把 exporter 丢失正文的结果误判为合理回退。
            raw = layout_pages.get(src)
            if fallback and raw and not raw.get("paragraphs") and not source[src].rotation:
                body_boxes = [fitz.Rect(l["box"]) for l in raw.get("layouts", [])
                              if l.get("class") in ("plain text", "text", "paragraph_hybrid")]
                visible = [l for l in source_lines[src] if len(re.findall(r"[A-Za-z]", l["text"])) >= 25 and
                           any(contains(b, center(l["box"])) for b in body_boxes)]
                if len(visible) >= 8:
                    valid = False
                    detail["invalid_fallback_evidence"] = {"reason": "PDF 有可读正文及文本布局，导出的 paragraphs 却为空",
                                                 "readable_line_count": len(visible), "examples": [
                                                     {"box": box_list(l["box"]), "text": l["text"]} for l in visible[:3]]}
            page_details.append(detail)
            if not valid:
                page_errors.append(detail)
        order_ok = [e["src"] for e in entries] == sorted(e["src"] for e in entries)
        add("pages", "页数、回退数及页面尺寸", not page_errors and order_ok and (mono is None or len(mono) == len(source)),
            {"source": len(source), "mono": len(mono) if mono else None, "dual": len(dual),
             "fallback_page_count": sum(bool(e.get("fallback")) for e in entries), "pages": page_details, "errors": page_errors})

        # 按输入区域解释链接计数差异；不调用合成/导航模块生成期望值。
        def input_destination(document, link):
            """读取输入自身的目标，统一为未旋转坐标；命名目标同样由输入解析。"""
            src, point = link.get("page", -1), link.get("to")
            name = link.get("nameddest") or (point if isinstance(point, str) else None)
            if name:
                src, x, y = document.resolve_link("#nameddest=" + name)
                point = (x, y)
            if type(src) is not int or not 0 <= src < len(document):
                return None, None
            if not isinstance(point, (fitz.Point, tuple, list)) or len(point) != 2 or not all(
                    isinstance(v, (int, float)) and math.isfinite(v) for v in point):
                point = None
            else:
                point = fitz.Point(point) * document[src].derotation_matrix
            return src, point

        navigation_rows = {"en": source_lines, "zh": [lines(p) for p in mono] if mono is not None else []}
        navigation_region_cache = {}

        def middle_intervals(src, side):
            """英文按原计划收拢，中文按输出元数据中的实际跨栏高度收拢。"""
            planned = [r for r in plans[src].regions if r.kind == "spanning"] if src in plans else []
            if side == "en":
                intervals = [(r.box[1], r.box[3]) for r in planned if r.position == "middle"]
            else:
                intervals = [(box.y0, box.y1) for n, _ in mapping[src]
                             for kind, box in regions[n] if kind == "span"]
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            if side == "en":
                return merged
            # 先合并实际区间再匹配计划带，延伸部分可能已越过原计划边界。
            return [(start, end) for start, end in merged
                    if any(r.position == "middle" and r.box[1] < end and start < r.box[3] for r in planned)
                    and not any(r.position == "top" and r.box[1] < end and start < r.box[3] for r in planned)]

        def input_regions(src, side):
            """从 sidecar 规划确定输入区域和 A/B，不读取输出链接或目录落点。

            返回 (输出页索引, 类别, 输入框)；跨栏仅有中文映射，页眉另行处理。
            输出元数据用于识别合成回退、实际页序号及中文中部跨栏占据的高度；
            跨栏期望文本仍从单语输入独立提取。
            """
            if (src, side) in navigation_region_cache:
                return navigation_region_cache[src, side]
            items = mapping[src]
            if src in fallback_sources:
                document = source if side == "en" else mono
                page = document[src]
                return [(items[0][0], side, page.rect * page.derotation_matrix)]
            if src not in plans:
                return []
            result = []
            plan = plans[src]
            bands = [fitz.Rect(r.box) for r in plan.regions if r.kind in ("header_footer", "spanning")]
            if side == "zh":
                bands.extend(fitz.Rect(0, start, source[src].rect.width, end)
                             for start, end in middle_intervals(src, side))
            for r in plan.regions:
                if r.kind == "spanning" and side == "zh":
                    part = "b" if r.position == "bottom" and plan.columns == 2 else "a"
                    result.extend((n, "span", fitz.Rect(r.box)) for n, e in items if e["part"] == part)
                elif r.kind == "column":
                    part = "b" if r.column == 1 else "a"
                    # 扣掉独立带，避免把跨栏目标当成普通正文落点。
                    pieces = [fitz.Rect(r.box)]
                    for band in bands:
                        remaining = []
                        for box in pieces:
                            for y0, y1 in ((box.y0, min(box.y1, band.y0)),
                                           (max(box.y0, band.y1), box.y1)):
                                if y0 < y1:
                                    remaining.append(fitz.Rect(box.x0, y0, box.x1, y1))
                        pieces = remaining
                    result.extend((n, side, box) for n, e in items if e["part"] == part for box in pieces)
            expanded = []
            for n, kind, box in result:
                # 规划框只是种子。按输入 PDF 的实际整行扩框，保留越过原边界的字。
                # 单语排版可能移动整段，用 output_box 定位对应的输入文字行。
                seeds = [box]
                if side == "zh":
                    seeds.extend(fitz.Rect(p.get("output_box", p["box"]))
                                 for p in layout_pages.get(src, {}).get("paragraphs", [])
                                 if p.get("marginal") is not True and contains(box, center(fitz.Rect(p["box"]))))
                grown = fitz.Rect(box)
                for row in navigation_rows[side][src]:
                    point = center(row["box"])
                    if not any(contains(seed, point) for seed in seeds):
                        continue
                    if kind != "span" and any(contains(band, point) for band in bands):
                        continue
                    grown |= row["box"]
                expanded.append((n, kind, grown))
            navigation_region_cache[src, side] = expanded
            return expanded

        def expected_destination(document, link, side):
            """独立判定目标输出页、类别及是否必须回退页顶。"""
            src, point = input_destination(document, link)
            if src is None:
                return None
            candidates = input_regions(src, side)
            if not candidates:
                return None
            header = point is not None and src not in fallback_sources and any(
                b.y0 - TOL <= point.y <= b.y1 + TOL for b in source_hf_bands[src])
            if point is not None and not header:
                hit = next((item for item in sorted(candidates, key=lambda item: item[1] != "span")
                            if contains(item[2], point)), None)
                if hit:
                    return hit[0], hit[1], False
            choices = [item for item in candidates if item[1] == side] if header else candidates
            def distance(item):
                box = item[2]
                return (max(box.x0 - point.x, 0, point.x - box.x1) ** 2 +
                        max(box.y0 - point.y, 0, point.y - box.y1) ** 2) if point is not None else 0
            chosen = min(choices or candidates, key=lambda item: (distance(item), item[0]))
            same_side = any(n == chosen[0] and kind == side for n, kind, _ in candidates)
            return chosen[0], side if same_side else "span", same_side

        placement_cache = {}

        def input_placements(src, side):
            """按输入扩框与规格的平移、避让规则计算锚点几何，不借用输出链接。"""
            if (src, side) in placement_cache:
                return placement_cache[src, side]
            if src in fallback_sources:
                document = source if side == "en" else mono
                box = document[src].rect * document[src].derotation_matrix
                dx = source[src].rect.width if side == "zh" else 0
                return [(mapping[src][0][0], side, box, fitz.Matrix(1, 0, 0, 1, dx, 0))]
            all_regions = {s: input_regions(src, s) for s in ("en", "zh") if s == "en" or mono is not None}
            frames = {}
            for items in all_regions.values():
                for n, kind, box in items:
                    if kind in ("en", "zh"):
                        frames[n] = frames.get(n, fitz.Rect(box)) | box
            width = max(source[src].rect.width,
                        2 * max((b.width for b in frames.values()), default=0) + 3 * LAYOUT_MARGIN)
            headers = [fitz.Rect(r.box) for r in plans[src].regions if r.kind == "header_footer"] if src in plans else []
            for i, box in enumerate(headers):
                for row in source_lines[src]:
                    if contains(box, center(row["box"])):
                        headers[i] |= row["box"]
            spans = [(n, box) for n, k, box in all_regions.get("zh", []) if k == "span"]
            middle = middle_intervals(src, side)
            result = []
            for n, entry in mapping[src]:
                obstacles = sorted(headers + [box for out, box in spans if out == n], key=lambda b: b.y0)
                if side == "en":
                    result.extend((n, "hf", box, fitz.Matrix(1, 0, 0, 1, (width - box.width) / 2 - box.x0, 0))
                                  for box in headers)
                down, previous_end = 0, 0
                for out, kind, box in sorted(all_regions.get(side, []), key=lambda item: item[2].y0):
                    if out != n:
                        continue
                    if kind == "span":
                        dx, dy = (width - box.width) / 2 - box.x0, 0
                    else:
                        frame = frames[n]
                        dx = (width / 2 - LAYOUT_MARGIN / 2 - frame.width if side == "en"
                              else width / 2 + LAYOUT_MARGIN / 2) - frame.x0
                        shift = sum(end - start for start, end in middle if end <= box.y0) if entry["part"] == "b" else 0
                        y = max(box.y0 - shift + down, previous_end)
                        for obstacle in obstacles:
                            if y < obstacle.y1 and obstacle.y0 < y + box.height:
                                y = obstacle.y1
                        down = y - (box.y0 - shift)
                        previous_end = y + box.height
                        dy = y - box.y0
                    result.append((n, kind, box, fitz.Matrix(1, 0, 0, 1, dx, dy)))
            placement_cache[src, side] = result
            return result

        def input_text(rows, box):
            """按输入字符中心取区域内容，允许 PDF 行被区域边界分割。"""
            return [compact("".join(c["c"] for c in row["chars"]
                                    if contains(box, center(fitz.Rect(c["bbox"]))))) for row in rows]

        # 所有跨栏带均从单语输入建立内容基准；输出 span 元数据不能定义期望值。
        span_errors, span_details, span_phrases = [], [], set()
        input_content = defaultdict(list)
        for src in range(len(source)):
            for side in navigation_rows:
                if side == "zh" and mono is None:
                    continue
                for n, kind, box, matrix in input_placements(src, side):
                    texts = input_text(navigation_rows[side][src], box)
                    input_content[n, kind].extend(texts)
                    if kind != "span":
                        continue
                    span_phrases.update(t for t in texts if t)
                    destination = box * matrix
                    expected = Counter("".join(texts))
                    actual = Counter(compact("".join(c["c"] for row in output_lines[n] for c in row["chars"]
                                                     if contains(destination, center(fitz.Rect(c["bbox"])))
                                                     and kind_at(center(fitz.Rect(c["bbox"])), regions[n]) == "span")))
                    detail = {"source_page": src + 1, "page": n + 1, "input_box": box_list(box),
                              "output_box": box_list(destination), "expected_chars": sum(expected.values()),
                              "actual_chars": sum(actual.values()), "missing_chars": dict(expected - actual),
                              "extra_chars": dict(actual - expected)}
                    span_details.append(detail)
                    if expected != actual:
                        span_errors.append(detail)
            if src in plans and src not in fallback_sources:
                for r in plans[src].regions:
                    if r.kind == "spanning":
                        span_phrases.update(t for t in input_text(source_lines[src], fitz.Rect(r.box)) if t)
        # 不设长度豁免：短页码与长页眉都按输入所在页/侧的已有份数核对。
        for n, expected_lines in hf_expected.items():
            for phrase in {compact(line["text"]) for line in expected_lines} - {""}:
                for kind in ("span", "en", "zh"):
                    allowed = sum(t.count(phrase) for t in input_content[n, kind])
                    actual = sum(compact(item["text"]).count(phrase) for item in grouped[n][kind])
                    if actual > allowed:
                        hf_errors.append({"page": n + 1, "kind": kind, "text": phrase[:80],
                                          "expected_body_copies": allowed, "actual_body_copies": actual})
        add("headers_footers", "每个输出页保留原文一份页眉页脚", not hf_errors,
            {"pages": hf_counts, "fallback_exempt_pages": [n + 1 for n, e in enumerate(entries) if e.get("fallback")], "errors": hf_errors})

        for phrase in span_phrases:
            for n, groups in enumerate(grouped):
                for kind, items in groups.items():
                    expected = sum(t.count(phrase) for t in input_content[n, kind])
                    actual = sum(compact(item["text"]).count(phrase) for item in items)
                    if actual > expected:
                        span_errors.append({"page": n + 1, "kind": kind, "text": phrase[:80],
                                            "expected_copies": expected, "actual_copies": actual})
        add("spanning", "全部跨栏区域完整、只一份且 A/B 归属正确", not span_errors,
            {"baseline": "mono", "regions": span_details, "errors": span_errors})

        expected_navigation, actual_navigation = [], []
        expected_links, actual_links = Counter(), Counter()
        omitted, link_details, side_errors = [], [], []
        raw_counts, raw_targets = {}, {}
        for document, label in ((source, "source"), (mono, "mono")):
            if document is None:
                continue
            raw_counts[label] = sum(len(p.get_links()) for p in document)
            raw_targets[label] = Counter()
            for src, page in enumerate(document):
                for link in page.get_links():
                    key = link_key(document, link)
                    raw_targets[label][key] += 1
                    copies = 1
                    kind = "column"
                    if src in plans and plans[src].columns and src not in fallback_sources:
                        plan = plans[src]
                        point = center(fitz.Rect(link["from"]) * page.derotation_matrix)
                        kind = next((r.kind for r in plan.regions if r.kind in ("header_footer", "spanning") and
                                     contains(fitz.Rect(r.box), point)), "column")
                        if (label == "source" and kind == "spanning") or (label == "mono" and kind == "header_footer"):
                            copies = 0
                            omitted.append({"input": label, "page": src + 1, "box": box_list(link["from"]), "target": key, "reason": kind})
                        elif label == "source" and kind == "header_footer":
                            copies = plan.columns
                    expected_links[key] += copies
                    if copies and key[0] == "goto":
                        side = "en" if label == "source" else "zh"
                        destination = expected_destination(document, link, side)
                        input_box = fitz.Rect(link["from"]) * page.derotation_matrix
                        anchors = [(n, k, (input_box & box) * matrix)
                                   for n, k, box, matrix in input_placements(src, side)
                                   if not (input_box & box).is_empty]
                        if destination is None or not anchors:
                            side_errors.append({"input": label, "source_page": src + 1,
                                                "input_anchor": box_list(link["from"]), "reason": "input_navigation_unverified"})
                        else:
                            for n, anchor_kind, rectangle in anchors:
                                expected_navigation.append({"page": n, "kind": anchor_kind,
                                                            "box": rectangle, "destination": destination})
        for n, page in enumerate(dual):
            for link in page.get_links():
                key = link_key(dual, link, entries)
                actual_links[key] += 1
                anchor = kind_at(center(fitz.Rect(link["from"])), regions[n])
                detail = {"page": n + 1, "anchor": box_list(link["from"]), "side": anchor, "target": key}
                if link["kind"] in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                    target, point = link.get("page", -1), link.get("to")
                    if link.get("nameddest"):
                        target, x, y = dual.resolve_link("#nameddest=" + link["nameddest"])
                        point = fitz.Point(x, y)
                    valid = 0 <= target < len(dual) and isinstance(point, (fitz.Point, tuple, list))
                    if valid:
                        point = fitz.Point(point) * dual[target].derotation_matrix
                        dest_kind = kind_at(point, regions[target])
                        # 跨栏锚点取单语输入；页眉页脚锚点只取原文输入。
                        wanted_side = "zh" if anchor == "span" else "en" if anchor == "hf" else anchor
                        top = abs(point.y) <= TOL and any(k == wanted_side and b.x0 - TOL <= point.x <= b.x1 + TOL
                                                                        for k, b in regions[target])
                        valid = anchor != "unknown" and (dest_kind == "span" or anchor == "hf" or
                                (wanted_side in ("en", "zh") and (dest_kind == wanted_side or top)))
                        detail.update({"target_page": target + 1, "target_point": box_list(point), "target_kind": dest_kind, "same_side_top_fallback": top})
                        actual_navigation.append({"page": n, "kind": anchor, "box": fitz.Rect(link["from"]),
                                                  "target": target, "point": point, "target_kind": dest_kind,
                                                  "same_side_top": top})
                    if not valid:
                        side_errors.append(detail)
                elif anchor == "unknown":
                    side_errors.append(detail)
                link_details.append(detail)
        differences = [{"target": key, "expected": expected_links[key], "actual": actual_links[key], "difference": actual_links[key] - expected_links[key]}
                       for key in expected_links.keys() | actual_links.keys() if expected_links[key] != actual_links[key]]
        count_ok = not differences if mono is not None else not (expected_links - actual_links)
        input_differences = []
        if mono is not None:
            for key in raw_targets["source"].keys() | raw_targets["mono"].keys():
                a, b = raw_targets["source"][key], raw_targets["mono"][key]
                if a != b:
                    input_differences.append({"target": key, "source": a, "mono": b, "difference": b - a})
        add("link_counts", "链接数量与目标覆盖", count_ok,
            {"source_and_mono": raw_counts, "dual": sum(actual_links.values()), "expected": sum(expected_links.values()),
             "mono_source_differences": input_differences, "output_input_differences": differences,
             "intentional_omissions": omitted, "output_links": link_details,
             "description": "无 --mono：只核对源链接目标至少保留原有数量，额外中文链接不可核实，差异仍全部列出" if mono is None else "差异不自动豁免；边界拆分或译文框扩展须人工核对证据"})
        navigation_differences = []
        remaining = list(actual_navigation)
        for expected in expected_navigation:
            # 先按几何消费唯一输出锚点，再核对其目标；目标错误不能借用邻锚点配额。
            found = next((a for a in remaining if a["page"] == expected["page"] and a["kind"] == expected["kind"]
                          and all(abs(x - y) <= TOL for x, y in zip(a["box"], expected["box"]))), None)
            target, kind, must_fallback = expected["destination"]
            if found is not None:
                remaining.remove(found)
            valid = found is not None and found["target"] == target
            if valid:
                valid = (found["same_side_top"] if must_fallback else found["target_kind"] == kind)
            if not valid:
                navigation_differences.append({"anchor_page": expected["page"] + 1,
                                               "anchor_kind": expected["kind"], "anchor_box": box_list(expected["box"]),
                                               "target_page": target + 1, "target_kind": kind,
                                               "must_fallback": must_fallback,
                                               "actual": found})
        navigation_differences.extend({"unexpected_anchor": a} for a in remaining
                                      if mono is not None or a["kind"] in ("en", "hf"))
        add("link_sides", "内部链接目标页、语言侧及页顶回退", not side_errors and not navigation_differences,
            {"internal_link_count": sum(d["target"][0] == "goto" for d in link_details),
             "destination_differences": navigation_differences, "errors": side_errors})
        toc_source, toc_out = source.get_toc(simple=False), dual.get_toc(simple=False)
        toc_errors = []
        for i, item in enumerate(toc_out):
            if i >= len(toc_source) or item[:2] != toc_source[i][:2]:
                toc_errors.append({"entry": i + 1, "invalid_title_or_level": item[:3]})
            elif toc_source[i][3].get("kind") in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                p = item[3].get("page", -1)
                expected = expected_destination(source, toc_source[i][3], "en")
                point = item[3].get("to")
                valid = item[3].get("kind") == fitz.LINK_GOTO and expected is not None and p == expected[0]
                valid = valid and isinstance(point, (fitz.Point, tuple, list)) and len(point) == 2
                if valid:
                    point = fitz.Point(point) * dual[p].derotation_matrix
                    valid = (abs(point.y) <= TOL if expected[2] else kind_at(point, regions[p]) == expected[1])
                    if expected[2]:
                        valid &= any(k == expected[1] and b.x0 - TOL <= point.x <= b.x1 + TOL for k, b in regions[p])
                if not valid:
                    toc_errors.append({"entry": i + 1, "output_target_page": p + 1,
                                       "expected_target_page": expected[0] + 1 if expected else None,
                                       "expected_target_kind": expected[1] if expected else None,
                                       "expected_page_top": expected[2] if expected else None})
        add("toc", "目录条数、层级、标题及 A/B 落点", len(toc_source) == len(toc_out) and not toc_errors,
            {"source": len(toc_source), "mono": len(mono.get_toc()) if mono else None, "dual": len(toc_out), "errors": toc_errors})
        label_details = []
        for n, entry in enumerate(entries):
            base = source[entry["src"]].get_label() or str(entry["src"] + 1)
            expected = base + entry["part"] if len(mapping[entry["src"]]) == 2 else base
            actual = dual[n].get_label()
            label_details.append({"page": n + 1, "expected": expected, "actual": actual})
        add("labels", "页码标签（单栏沿用，双栏 a/b）", all(d["expected"] == d["actual"] for d in label_details), label_details)
        stream_errors = list(unknown)
        for n, page in enumerate(dual):
            ranks = [RANK[k] for k, _ in regions[n]]
            if ranks != sorted(ranks):
                stream_errors.append({"page": n + 1, "metadata_region_order": ranks})
            previous = -1
            for span in sorted(page.get_texttrace(), key=lambda s: s["seqno"]):
                for code, _, _, box in span["chars"]:
                    if chr(code).isspace():
                        continue
                    kind = kind_at(center(fitz.Rect(box)), regions[n])
                    rank = RANK.get(kind, -1)
                    if rank < previous or kind == "unknown":
                        stream_errors.append({"page": n + 1, "seqno": span["seqno"], "kind": kind, "previous_rank": previous,
                                              "box": box_list(box), "text": chr(code)})
                    previous = rank
        add("stream_order", "内容流顺序 页眉页脚→跨栏→英文→中文", not stream_errors,
            {"method": "PyMuPDF get_texttrace 按 seqno 执行顺序逐字归属；未按视觉坐标重排", "errors": stream_errors})
        js_ok, js_evidence = pdfjs_order(Path(dual_path).resolve(), metadata)
        add("pdfjs_order", "Zotero pdf.js 文本顺序与元数据", js_ok is True, js_evidence)
    report["status"] = "FAIL" if any(c["status"] == "FAIL" for c in report["checks"]) else "PASS"
    return report


def render_text(report):
    output = []
    for check in report["checks"]:
        output.append(f"{check['status']} {check['id']} — {check['name']}")
        evidence = json.dumps(check["evidence"], ensure_ascii=False, default=str)
        output.append("  " + (evidence if len(evidence) <= 2200 else evidence[:2200] + " …（完整证据用 --json）"))
    output.append("总结果：" + report["status"])
    return "\n".join(output)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="分栏双语 PDF 质量检查（只读、无 API）")
    parser.add_argument("--source", type=Path, required=True, help="原文 PDF")
    parser.add_argument("--dual", type=Path, required=True, help="分栏双语 PDF")
    parser.add_argument("--mono", type=Path, help="单语 PDF（可读取邻接 .layout.json）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 证据")
    args = parser.parse_args()
    try:
        report = build_report(args.source, args.dual, args.mono)
    except Exception as error:
        report = {"status": "FAIL", "checks": [{"id": "input", "name": "读取或检查失败", "status": "FAIL",
                                               "evidence": {"error_type": type(error).__name__}}]}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json else render_text(report))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
