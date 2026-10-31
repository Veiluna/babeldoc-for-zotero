"""T5 离线质量门禁：真实文本层、完整变体、页边单份原位、v1 和 pdf.js 顺序。"""
import argparse
from collections import Counter
from bisect import bisect_right
from contextlib import ExitStack
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import pymupdf as fitz
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'addon/content/python'))
import ztf_split_compose as compose
from ztf_split_types import MARGIN,SPANNING
from dataclasses import replace
RANK={'hf':0,'span':1,'en':2,'zh':3}


class RegionIndex:
    """按真实矩形边界建立纵向索引，不量化坐标或增加容差。"""
    def __init__(self, regions):
        events={}
        for i,(_,box) in enumerate(regions):
            events.setdefault(box.y0,[]).append((i,True))
            events.setdefault(box.y1,[]).append((i,False))
        self.edges=sorted(events)
        self.bands=[]
        active=set()
        for y in self.edges:
            for i,adding in events[y]:
                if adding: active.add(i)
                else: active.discard(i)
            self.bands.append(tuple(regions[i] for i in sorted(active)))

    def at(self,y):
        i=bisect_right(self.edges,y)-1
        return self.bands[i] if i>=0 else ()


def glyphs(page):
    return compose._filter(page,collect=True)


def layer_glyphs(page):
    """逐嵌入流读取真实文字层；未知/额外流也保留，不能被预期文本长度掩盖。"""
    groups={k:[] for k in RANK}
    groups['unknown']=[]
    actual=[]
    with fitz.open() as copied:
        copied.insert_pdf(page.parent,from_page=page.number,to_page=page.number,links=False)
        streams=copied[0].get_contents()
        for xref in streams:
            match=re.match(rb'/ZTFSplit_(hf|span|en|zh) BMC\n',copied.xref_stream(xref))
            kind=match[1].decode('ascii') if match else 'unknown'
            copied.xref_set_key(copied[0].xref,'Contents',f'{xref} 0 R')
            chars=glyphs(copied[0])
            actual.extend(chars)
            groups[kind].extend(chars)
    return actual,groups


def text(chars):
    return ''.join(c.text for c in chars)


def counts(chars):
    return Counter(c.text for c in chars if c.text.strip())


def same_position(left,right,reference=None):
    # 同一 PDF 字体矩阵的浮点计算只允许 float32 算术舍入；无版式/视觉容差。
    import struct
    def ulp(value):
        value=abs(value)
        bits=struct.unpack('I',struct.pack('f',value))[0]
        return struct.unpack('f',struct.pack('I',bits+1))[0]-value
    reference=reference or left
    return left.text==right.text and all(abs(a-b)<=max(ulp(a),ulp(b),ulp(r))*4
        for a,b,r in zip((left.x0,left.y0,left.x1,left.y1),(right.x0,right.y0,right.x1,right.y1),
                         (reference.x0,reference.y0,reference.x1,reference.y1)))


def same_run(expected, actual, dx=0):
    # MuPDF 会省掉无墨迹的尾空格；文本重复比较仍使用完整空格，几何核验只核对绘制字。
    expected=[c for c in expected if c.text.strip()]
    actual=[c for c in actual if c.text.strip()]
    return len(expected)==len(actual) and all(same_position(
        replace(a,x0=a.x0+dx,x1=a.x1+dx),b,a) for a,b in zip(expected,actual))


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
    const page = await doc.getPage(i);
    const content = await page.getTextContent({includeMarkedContent:true});
    const roles=[], items=[];
    for (const item of content.items) {
      if (item.type==='beginMarkedContent' || item.type==='beginMarkedContentProps') {
        roles.push(/^ZTFSplit_(hf|span|en|zh)$/.exec(item.tag)?.[1] ?? roles.at(-1) ?? 'unknown');
      } else if (item.type==='endMarkedContent') { roles.pop(); }
      else if (item.str?.trim()) { items.push({kind:roles.at(-1) ?? 'unknown'}); }
    }
    pages.push(items);
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
        for item in items:
            kind = item['kind']
            rank = RANK.get(kind, -1)
            if rank < previous or kind == "unknown":
                errors.append({"page": n + 1, "kind": kind, "previous_rank": previous, **item})
            previous = rank
    metadata_equal = bool(result.get("layout")) and json.loads(result["layout"]) == layout
    return not errors and metadata_equal, {"version": result["version"], "metadata_equal": metadata_equal, "errors": errors}




def build_report(source_path,dual_path,mono_path=None):
    report={'checks':[],'coordinates':'左上原点 pt；页号从 1 开始'}
    def add(key,name,errors,**evidence):
        report['checks'].append({'id':key,'name':name,'status':'FAIL' if errors else 'PASS',
                                 'evidence':{'errors':errors,**evidence}})
    with ExitStack() as stack:
        source=stack.enter_context(fitz.open(source_path))
        dual=stack.enter_context(fitz.open(dual_path))
        mono=stack.enter_context(fitz.open(mono_path)) if mono_path else None
        _,ref=dual.xref_get_key(-1,'Info')
        layout=json.loads(dual.xref_get_key(int(ref.split()[0]),'ZTFSplitLayout')[1])
        if layout.get('v')!=1 or len(layout['pages'])!=2*len(source):
            add('metadata','ZTFSplitLayout v1',['invalid_layout']);report['status']='FAIL';return report
        add('metadata','ZTFSplitLayout v1',[])
        raw_path=Path(str(mono_path)+'.layout.json')
        raw=json.loads(raw_path.read_text(encoding='utf-8')) if raw_path.is_file() else {'pages':[]}
        mapping=json.loads(Path(str(mono_path)+'.variants.json').read_text(encoding='utf-8'))
        entries={e['source_page']:e for e in mapping['pages']}
        variants=stack.enter_context(fitz.open(mapping['pdf_path'])) if mapping.get('pdf_path') else None
        analysis=compose.analyze_source(source,raw)
        page_errors=[];en_errors=[];zh_errors=[];span_errors=[];margin_errors=[];region_errors=[]
        page_results=[]
        cached_variants={}
        for n,page in enumerate(dual):
            meta=layout['pages'][n];src=meta['src'];entry=entries[src]
            regions=RegionIndex([(r['k'],fitz.Rect(r['r'])*page.transformation_matrix) for r in meta['regions']])
            actual,groups=layer_glyphs(page)
            for c in actual:
                if c.text.strip() and not any(fitz.Rect(c.x0,c.y0,c.x1,c.y1).intersects(box)
                    for _,box in regions.at((c.y0+c.y1)/2)):
                    region_errors.append({'page':n+1,'reason':'unmapped_glyph'})
            if any(c.text.strip() for c in groups['unknown']):
                region_errors.append({'page':n+1,'reason':'unmarked_content'})
            whole=entry.get('columns')==1 or meta.get('fallback',False)
            if (page.rect.width!=source[src].rect.width or meta.get('width')!=page.rect.width
                or meta.get('height')!=page.rect.height
                or page.rect.height<source[src].rect.height
                or (whole and n%2==0 and page.rect!=source[src].rect)):
                page_errors.append({'page':n+1,'reason':'page_size'})
            if whole:
                expected=glyphs(source[src] if n%2==0 else mono[src])
                if counts(actual)!=counts(expected): en_errors.append({'page':n+1,'reason':'whole_page_text'})
            else:
                owners=compose._owners(src,entry,analysis,source[src].rect.width,source[src].rect.height)
                column=n%2
                if not same_run([c for c in analysis[0][src] if owners[c.id]==column],groups['en'],
                                -column*source[src].rect.width/2):
                    en_errors.append({'page':n+1,'reason':'english_character_assignment'})
                expected_height=source[src].rect.height
                for key,col in (('zh',column),('span',SPANNING)):
                    index=next(v['pdf_page'] for v in entry['variants'] if v['column']==col)
                    if index not in cached_variants:
                        doc=stack.enter_context(fitz.open())
                        doc.insert_pdf(variants,from_page=index,to_page=index,links=False)
                        boxes=compose._content_boxes(doc[0]);compose._extend(doc[0],max([source[src].rect.height]+[b.y1 for b in boxes]))
                        cached_variants[index]=(glyphs(doc[0]),doc[0].rect.height)
                    expected_chars,variant_height=cached_variants[index]
                    expected_height=max(expected_height,variant_height)
                    dx=(1-column)*source[src].rect.width/2 if key=='zh' else 0
                    if not same_run(expected_chars,groups[key],dx):
                        zh_errors.append({'page':n+1,'reason':key+'_variant_incomplete'})
                if page.rect.height!=expected_height:
                    page_errors.append({'page':n+1,'reason':'unexpected_content_height'})
                expected=[c for c in analysis[0][src] if owners[c.id]==MARGIN and c.text.strip()]
                unmatched=[c for c in groups['hf'] if c.text.strip()]
                for c in expected:
                    match=next((i for i,g in enumerate(unmatched) if same_position(c,g)),None)
                    if match is None:
                        margin_errors.append({'page':n+1,'reason':'margin_not_at_original_position'});break
                    unmatched.pop(match)
                if unmatched: margin_errors.append({'page':n+1,'reason':'extra_margin_glyph'})
                # 每个已认定页边行的整段文本在该输出页只出现一次；正文含相同单字不误报。
                full=text(actual)
                for line in analysis[1][src]:
                    if line.index in analysis[3][src] and full.count(line.text)!=1:
                        margin_errors.append({'page':n+1,'reason':'margin_copy_count'});break
                if n%2:
                    _,previous_groups=layer_glyphs(dual[n-1])
                    previous=previous_groups['span']
                    if len(previous)!=len(groups['span']) or any(not same_position(a,b) for a,b in zip(previous,groups['span'])):
                        span_errors.append({'source_page':src+1,'reason':'span_copies_differ'})
            if n%2==0:
                page_results.append({'page':src+1,'columns':entry.get('columns',1),
                    'grown':[dual[n+i].rect.height>source[src].rect.height for i in (0,1)],
                    'safety_net':bool(meta.get('fallback'))})
        add('pages','每源页两张原宽度页',page_errors,pages=page_results)
        add('english_assignment','英文逐字归属',en_errors)
        add('variants_complete','中文变体完整且未缩放',zh_errors)
        add('spanning_copies','跨栏两份同内容同位置',span_errors)
        add('headers_footers','已认定源页边字符每页一次且原位',margin_errors)
        add('selection_regions','每个字符有 v1 划词区域',region_errors)
        passed,evidence=pdfjs_order(dual_path,layout)
        pdfjs_errors=evidence.pop('errors',[])
        add('pdfjs_order','Zotero pdf.js 内容流顺序',[] if passed else pdfjs_errors or ['pdfjs_order'],**evidence)
    report['status']='PASS' if all(c['status']=='PASS' for c in report['checks']) else 'FAIL'
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True);parser.add_argument('--dual',required=True)
    parser.add_argument('--mono');parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    try: report=build_report(args.source,args.dual,args.mono)
    except Exception as error: report={'status':'FAIL','error':type(error).__name__,'checks':[]}
    print(json.dumps(report,ensure_ascii=False,indent=2) if args.json else report['status'])
    return 0 if report['status']=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
