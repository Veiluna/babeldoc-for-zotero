"""T5 分栏合成：英文逐字过滤，中文变体整份嵌入，不裁剪、不涂黑。

输入源 PDF、单语 PDF、runner 导出的源段落/图表布局及 T4 sidecar。
每源页输出两页，单页程序异常只撤销该页；保存失败不替换已有输出。
导航和 ZTFSplitLayout 沿用 v1，坐标为实际输出页的 PDF 用户空间。
"""
from contextlib import ExitStack
from collections import Counter
from dataclasses import dataclass
import ctypes
import json
import logging
import math
import os
from pathlib import Path
import tempfile

import pymupdf as fitz
from ztf_split_types import Char, DocumentColumns, MARGIN, SPANNING
from ztf_split_lines import build_lines
from ztf_split_columns import detect_columns
from ztf_split_margins import classify_margins, read_pdf_evidence
from ztf_split_variants import object_region


def _log(event, reason, **fields):
    page = fields.get('source_index', fields.get('output_index'))
    logging.getLogger(__name__).warning(json.dumps({'event':event,'reason':reason,
        **({'page':page} if page is not None else {}),
        **({'count':fields['count']} if 'count' in fields else {})}))


@dataclass(frozen=True)
class RegionPlacement:
    src: int
    out: int
    part: str
    input: str
    k: str
    source_box: tuple
    output_box: tuple
    matrix: tuple


@dataclass(frozen=True)
class _Page:
    source_index: int
    columns: int


class _GraphicObjects(fitz.mupdf.PdfProcessor2):
    """按内容流调用顺序读取原路径和渐变载体，不使用描边外扩框。"""
    def __init__(self, page, matrix=None, carrier=None, forms=()):
        super().__init__()
        self.page=page
        self.document=fitz.mupdf.pdf_document_from_fz_document(page.parent.this)
        self.matrix=matrix if matrix is not None else fitz.Identity
        self.carrier,self.forms=carrier,forms
        self.stack,self.boxes,self.paths,self.parts=[],[],[],[]
        self.paintings=[]
        self.closed=False
        self.clip=False
        self.clip_box=fitz.INFINITE_RECT()
        for name in ('q','Q','cm','Do_form','sh','m','l','c','v','y','h','re',
                     'S','s','f','F','fstar','B','Bstar','b','bstar','n','W','Wstar'):
            getattr(self,'use_virtual_op_'+name)()

    def op_q(self, ctx):
        self.stack.append((self.matrix,self.clip_box))

    def op_Q(self, ctx):
        if self.stack: self.matrix,self.clip_box=self.stack.pop()

    def op_cm(self, ctx, a, b, c, d, e, f):
        self.matrix=fitz.Matrix(a,b,c,d,e,f)*self.matrix

    def op_Do_form(self, ctx, name, raw):
        if self.clip_box.is_empty: return
        native=fitz.mupdf
        obj=native.PdfObj(native.ll_pdf_keep_obj(raw))
        identity=native.pdf_to_num(obj) or obj.m_internal_value()
        if identity in self.forms:
            return
        m=native.pdf_xobject_matrix(obj)
        matrix=fitz.Matrix(m.a,m.b,m.c,m.d,m.e,m.f)*self.matrix
        b=native.pdf_xobject_bbox(obj)
        box=fitz.Rect(b.x0,b.y0,b.x1,b.y1)*matrix*self.page.transformation_matrix
        child=_GraphicObjects(self.page,matrix,box,(*self.forms,identity))
        native.pdf_process_contents(child,self.document,native.pdf_xobject_resources(obj),
                                    obj,native.FzCookie())
        self.boxes.extend(child.boxes)
        self.paths.extend(child.paths)
        self.paintings.extend(child.paintings)

    def op_sh(self, ctx, name, raw):
        if not self.clip_box.is_empty:
            self.boxes.append(self.carrier if self.carrier is not None else fitz.INFINITE_RECT())

    def op_m(self, ctx, x, y):
        if not self.clip_box.is_empty:
            self.parts.append([(x,y)]);self.closed=False

    def op_l(self, ctx, x, y):
        if self.parts and not self.clip_box.is_empty:
            if self.closed:
                self.parts.append([self.parts[-1][0]]);self.closed=False
            self.parts[-1].append((x,y))

    def op_c(self, ctx, x1, y1, x2, y2, x3, y3):
        for x,y in ((x1,y1),(x2,y2),(x3,y3)): self.op_l(ctx,x,y)

    def op_v(self, ctx, x2, y2, x3, y3):
        self.op_l(ctx,x2,y2);self.op_l(ctx,x3,y3)

    def op_y(self, ctx, x1, y1, x3, y3):
        self.op_l(ctx,x1,y1);self.op_l(ctx,x3,y3)

    def op_h(self, ctx):
        self.closed=True

    def op_re(self, ctx, x, y, w, h):
        if not self.clip_box.is_empty:
            self.parts.append([(x,y),(x+w,y),(x+w,y+h),(x,y+h)])
            self.closed=True

    def op_W(self, ctx):
        self.clip=True

    op_Wstar=op_W

    def _paint(self, kind):
        if self.parts:
            matrix=self.matrix*self.page.transformation_matrix
            points=[fitz.Point(*point)*matrix for part in self.parts for point in part]
            box=fitz.Rect(min(p.x for p in points),min(p.y for p in points),
                          max(p.x for p in points),max(p.y for p in points))
            if kind: self.paintings.append((kind,tuple(box)))
            for part in self.parts:
                if len(part)<2: continue
                transformed=[fitz.Point(*point)*matrix for point in part]
                bounds=fitz.Rect(min(p.x for p in transformed),min(p.y for p in transformed),
                                 max(p.x for p in transformed),max(p.y for p in transformed))
                self.paths.append((bounds,box,kind+4*int(self.clip)))
            if self.clip: self.clip_box=self.clip_box & box
        self.parts=[];self.clip=False;self.closed=False

    def op_S(self, ctx): self._paint(2)
    op_s=op_S
    def op_f(self, ctx): self._paint(1)
    op_F=op_f
    op_fstar=op_f
    def op_B(self, ctx): self._paint(3)
    op_Bstar=op_B
    op_b=op_B
    op_bstar=op_B
    def op_n(self, ctx): self._paint(0)

    def run(self):
        native=fitz.mupdf
        page=native.pdf_page_from_fz_page(self.page.this)
        native.pdf_process_contents(self,self.document,native.pdf_page_resources(page),
                                    native.pdf_page_contents(page),native.FzCookie())
        return self


class _GlyphFilter(fitz.mupdf.PdfSanitizeFilterOptions2):
    """MuPDF 原生逐字回调；同位置重叠字、连字和 Form 实例均按书写序号隔离。"""
    def __init__(self, matrix, selected=None, graphic=None, shades=(), paths=()):
        super().__init__()
        self.matrix, self.selected, self.graphic = matrix, selected, graphic
        self.chars = []
        self.shades=iter(shades)
        self.paths=iter((kind,parent) for _,parent,kind in paths if kind<4)
        self.use_virtual_text_filter()
        if graphic is not None:
            self.use_virtual_culler()

    def text_filter(self, ctx, ucs, length, trm, ctm, bbox, render, ca, CA):
        # 1.28.2 mupdf.py director 签名；ucs 指针只在回调期间读取，不保留。
        codepoints = ctypes.cast(int(ucs), ctypes.POINTER(ctypes.c_int))
        text = ''.join(chr(codepoints[i]) if 0 <= codepoints[i] <= 0x10ffff else '\ufffd'
                       for i in range(length))
        matrix = fitz.Matrix(trm.a,trm.b,trm.c,trm.d,trm.e,trm.f)
        matrix *= fitz.Matrix(ctm.a,ctm.b,ctm.c,ctm.d,ctm.e,ctm.f)
        box = fitz.Rect(bbox.x0,bbox.y0,bbox.x1,bbox.y1) * matrix * self.matrix
        index = len(self.chars)
        self.chars.append(Char(index,index,0,*box,text))
        return int(self.selected is not None and index not in self.selected)

    def culler(self, ctx, bbox, kind):
        # Clip paths必须保留：删除裁剪指令会改变仍保留的字和图形。
        native = fitz.mupdf
        if kind == native.FZ_CULL_GLYPH:
            return 0
        if 4<=kind<=7: return 0
        if 0<=kind<4:
            expected,box=next(self.paths)
            if expected!=kind: raise ValueError('graphic_order_mismatch')
            return int(not self.graphic(box))
        # MuPDF 1.28.2 pdf_filter_sh 丢弃 fz_bound_shade 的返回值，传来的是
        # 单位方框；不能据此把任意位置的渐变判成左栏。T4 透传渐变随载体。
        box = (next(self.shades) if kind==native.FZ_CULL_SHADING else
               fitz.Rect(bbox.x0,bbox.y0,bbox.x1,bbox.y1) * self.matrix)
        return int(not self.graphic(box))


def _filter(page, selected=None, graphic=None, *, collect=False):
    native = fitz.mupdf
    objects=_GraphicObjects(page).run() if graphic is not None else None
    callback = _GlyphFilter(page.transformation_matrix,selected,graphic,
                           objects.boxes if objects else (),objects.paths if objects else ())
    # 实例化 Form，避免同一 XObject 多次调用时一次筛选污染其他实例。
    # 只按实际绘制调用筛选。recurse 会再次进入 Pattern 等资源的局部坐标流；
    # 其归属已由使用它的路径决定，不能在资源坐标中再按页面中线删一遍。
    # instance_forms 独立保证所有实际 Form 调用仍逐实例递归筛选。
    options = fitz._make_PdfFilterOptions(recurse=0,instance_forms=1,
                                         no_update=int(collect),sanitize=1,sopts=callback)
    native.pdf_filter_page_contents(native.pdf_document_from_fz_document(page.parent.this),
                                   native.pdf_page_from_fz_page(page.this),options)
    return tuple(callback.chars)


def analyze_source(source, layout):
    """只读内容流与 T3 证据，供合成/质量检查共用；不使用 YOLO 栏框。"""
    pages = {p['index']:p for p in layout.get('pages',[])}
    chars, lines, figures, errors = [], [], [], set()
    for number,page in enumerate(source):
        data = pages.get(number,{})
        paragraphs = data.get('paragraphs',[])
        assigned=[]
        # no_update 不写回源 PDF；真实回调顺序也是随后过滤时的序号。
        try:
            glyphs = _filter(page, collect=True)
        except Exception:
            glyphs = ()
            errors.add(number)
        for char in glyphs:
            center = fitz.Point((char.x0+char.x1)/2,(char.y0+char.y1)/2)
            owner = next((p.get('index',i) for i,p in enumerate(paragraphs)
                          if fitz.Rect(p['box']).contains(center)), -1)
            assigned.append(Char(char.id,char.order,0,char.x0,char.y0,char.x1,char.y1,
                                 char.text,owner))
        try:
            page_lines = build_lines(number, assigned)
        except Exception:
            assigned, page_lines = [], ()
            errors.add(number)
        chars.append(tuple(assigned))
        lines.append(page_lines)
        figures.append(tuple(tuple(p['box']) for p in data.get('layouts',[])
                             if p.get('class') in {'figure','table','formula','isolate_formula'}))
    try:
        tagged,appended = read_pdf_evidence(source.name) if source.name else ({},{})
    except Exception:
        tagged, appended = {}, {}
        errors.update(range(len(source)))
    margins = classify_margins(lines,[p.rect.height for p in source],tagged,appended)
    return chars,lines,figures,margins,errors


def _owners(number, entry, analysis, width, height):
    chars,lines,figures,margins,_ = analysis
    document = DocumentColumns(entry['columns'],entry.get('basis','single'))
    columns = detect_columns(number,width,height,lines[number],margins[number],document,figures[number])
    special = {cid:owner for line,owner in zip(lines[number],columns.line_column)
               if owner in (MARGIN,SPANNING) for cid in line.char_ids}
    paragraphs = dict(entry.get('paragraph_column',columns.paragraph_column))
    owners = {c.id:(MARGIN if special.get(c.id)==MARGIN else
                    SPANNING if paragraphs.get(c.paragraph)==SPANNING or special.get(c.id)==SPANNING else
                    0 if c.x1<=width/2 else 1) for c in chars[number]}
    return owners


def body_starts(source, analysis, entries):
    """全文正文书写行的两栏起点；排除页边、跨栏、图表公式内文字。"""
    _,lines,figures,margins,errors=analysis
    starts=[Counter(),Counter()]
    for number,page in enumerate(source):
        if number in errors or entries.get(number,{}).get('columns')!=2:
            continue
        mid=page.rect.width/2
        owners=_owners(number,entries[number],analysis,page.rect.width,page.rect.height)
        for owner in (0,1):
            # 同一基线连续书写可能从左栏一直写到右栏；先按逐字认栏分开。
            for line in build_lines(number,[c for c in analysis[0][number] if owners[c.id]==owner]):
                box=(line.x0,line.y0,line.x1,line.y1)
                if (not line.text.strip() or any(
                    b[0]<=box[0] and b[1]<=box[1] and box[2]<=b[2] and box[3]<=b[3]
                    for b in figures[number])):
                    continue
                starts[owner][line.x0]+=1
    return tuple(c.most_common(1)[0][0] if c else None for c in starts)


def column_translation(source, analysis, entries):
    left,right=body_starts(source,analysis,entries)
    # 全跨栏/空页没有可移动正文；不以半页宽猜测缺失的起点。
    return right-left if left is not None and right is not None else 0


def column_origin_status(source, analysis, entries):
    """返回双栏正文起点及是否只测得一侧。"""
    origins = body_starts(source, analysis, entries)
    return origins, (origins[0] is None) != (origins[1] is None)


def _layer(source, number, selected, graphic):
    result = fitz.open()
    try:
        result.insert_pdf(source,from_page=number,to_page=number,links=False,annots=False,widgets=False)
        _filter(result[0],selected,graphic)
        return result
    except Exception:
        result.close()
        raise


def _content_boxes(page):
    # bboxlog 返回实际内容范围（包括 MediaBox 之外），不是裁剪后的提取文本。
    graphics, skipped = [], 0
    for kind,box,*_ in page.get_bboxlog():
        if kind in ('ignore-text', 'fill-text', 'stroke-text'):
            continue
        if not all(math.isfinite(v) for v in box) or fitz.Rect(box).is_infinite:
            skipped += 1
            continue
        if not fitz.Rect(box).is_empty:
            graphics.append(fitz.Rect(box))
    if skipped:
        _log('split_compose_bounds_filtered','non_finite_bounds',
             source_index=page.number,count=skipped)
    return graphics + [fitz.Rect(line.x0,line.y0,line.x1,line.y1)
                       for line in build_lines(0, _filter(page, collect=True))]


def _text_boxes(page):
    """划词区域取真实文本行；未知字映射以实际绘制框补齐，不借用图形外框。"""
    boxes=[fitz.Rect(line['bbox']) for block in page.get_text('dict',
            flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES,clip=fitz.INFINITE_RECT())['blocks']
            for line in block.get('lines',()) if any(span['text'].strip() for span in line['spans'])]
    for char in _filter(page,collect=True):
        box=fitz.Rect(char.x0,char.y0,char.x1,char.y1)
        if char.text.strip() and not any(owner.contains(box) for owner in boxes):
            boxes.append(box)
    return boxes


def _selection(regions, page, kind, document, dx=0, elements=()):
    # span 的矩形同时是 readerSelection 的元素身份，沿用 IL 段落/图表身份，
    # 不能把同一多行元素按字体 span 分裂；未知文字仍由真实文本框补齐。
    boxes=[fitz.Rect(box) for box in elements]
    for box in _text_boxes(document[0]):
        if not any(owner.contains(box) for owner in boxes):
            boxes.append(box)
    regions.extend((page.number,kind,tuple(box+(dx,0,dx,0))) for box in boxes)


def _extend(page, height):
    if height > page.rect.height:
        media,crop=page.mediabox,page.cropbox
        delta=height-page.rect.height
        page.set_mediabox(fitz.Rect(media.x0,media.y0-delta,media.x1,media.y1))
        # set_mediabox 会删除 CropBox；恢复原裁剪原点/宽度，仅向页底延伸。
        page.set_cropbox(fitz.Rect(crop.x0,crop.y0,crop.x1,crop.y1+delta))


def _embed_full(page, document, number, dx=0, *, kind, rotate=0):
    source=document[number]
    # 不传 clip；目标与源同尺寸，平移后完整 Form 嵌入，不改变字号。
    if source.get_contents():
        bounds=(fitz.Rect(0,0,source.rect.height,source.rect.width)
                if rotate%180 else source.rect)
        page.show_pdf_page(bounds+(dx,0,dx,0),document,number,
                           keep_proportion=False,rotate=rotate)
        # 标准 PDF marked content 记录真实层身份；不改变字形/几何或 v1 接口。
        # QA 可据实际内容流核对顺序，不能从可能相交的选择框反推文字来源。
        xref=page.get_contents()[-1]
        page.parent.update_stream(xref,b'/ZTFSplit_'+kind.encode('ascii')+b' BMC\n'
            +page.parent.xref_stream(xref)+b'\nEMC')


def _record(placements, number, out, part, kind, box, dx=0, dy=0):
    box=fitz.Rect(box)
    if box.is_empty: return
    placements.append(RegionPlacement(number,out,part,'source' if kind in {'en','hf'} else 'mono',
        kind,tuple(box),tuple(box+(dx,dy,dx,dy)),(1,0,0,1,dx,dy)))


def _compose_columns(source, mono, entry, variants, output, placements, analysis, regions, page_layout,
                     column_shift):
    number=entry['source_page']; original=source[number]; width,height=original.rect.width,original.rect.height
    if original.rotation: raise ValueError('rotated_page')
    owners=_owners(number,entry,analysis,width,height)
    indices={v['column']:v['pdf_page'] for v in entry['variants']}
    if set(indices)!={0,1,SPANNING}: raise ValueError('variant_set_invalid')
    paragraphs=dict(entry.get('paragraph_column',()))
    span_elements=[p.get('output_box') or p['box'] for p in page_layout.get('paragraphs',())
                   if paragraphs.get(p.get('index'))==SPANNING]
    span_elements.extend(p['box'] for p in page_layout.get('layouts',())
                         if p.get('class') in {'figure','table','formula','isolate_formula'}
                         and p['box'][0]<width/2<p['box'][2])
    with ExitStack() as stack:
        # 各变体单独复制，可调整 MediaBox 露出页底内容；不修改 T4 文件。
        zh={}
        for column in (0,1,SPANNING):
            doc=stack.enter_context(fitz.open())
            doc.insert_pdf(variants,from_page=indices[column],to_page=indices[column],links=False,annots=False,widgets=False)
            boxes=_content_boxes(doc[0])
            bottom=max([height]+[b.y1 for b in boxes])
            _extend(doc[0],bottom)
            zh[column]=(doc,boxes,bottom)
        margins={i for i,owner in owners.items() if owner==MARGIN}
        hf=stack.enter_context(_layer(source,number,margins,lambda box:False))
        hf_boxes=[(l.x0,l.y0,l.x1,l.y1) for l in analysis[1][number] if l.index in analysis[3][number]]
        for column in (0,1):
            selected={i for i,owner in owners.items() if owner==column}
            en=stack.enter_context(_layer(source,number,selected,
                lambda box,c=column: object_region(tuple(box),analysis[2][number],
                                                    ((width/2,width/2),))==c))
            page_height=max(height,zh[column][2],zh[SPANNING][2])
            page=output.new_page(width=width,height=page_height)
            part='ab'[column]
            _embed_full(page,hf,0,kind='hf')
            _selection(regions,page,'hf',hf)
            for box in hf_boxes: _record(placements,number,page.number,part,'hf',box)
            _embed_full(page,zh[SPANNING][0],0,kind='span')
            _selection(regions,page,'span',zh[SPANNING][0],elements=span_elements)
            for box in zh[SPANNING][1]: _record(placements,number,page.number,part,'span',box)
            dx=-column*column_shift
            _embed_full(page,en,0,dx,kind='en')
            _selection(regions,page,'en',en,dx)
            for box in _content_boxes(en[0]): _record(placements,number,page.number,part,'en',box,dx)
            dx=(1-column)*column_shift
            _embed_full(page,zh[column][0],0,dx,kind='zh')
            _selection(regions,page,'zh',zh[column][0],dx)
            for box in zh[column][1]: _record(placements,number,page.number,part,'zh',box,dx)


def _whole_pages(source, mono, number, output, placements, regions, *, expand=True):
    for document,kind,part in ((source,'en','a'),(mono,'zh','b')):
        original=document[number]
        # 单栏中文也完整保留页底溢出；英文页仍为原大小。只修改用于嵌入的副本。
        with fitz.open() as copied:
            copied.insert_pdf(document,from_page=number,to_page=number,links=False,annots=False,widgets=False)
            # remove_rotation 会重置非全页 CropBox；仅清 Rotate，再在嵌入矩阵旋转，
            # 同时保持原可视范围和页面宽高，避免裁掉整页安全网的内容。
            copied[0].set_rotation(0)
            if expand and kind=='zh' and not original.rotation:
                _extend(copied[0],max([original.rect.height]+[b.y1 for b in _content_boxes(copied[0])]))
            page=output.new_page(width=original.rect.width,
                height=original.rect.height if original.rotation else copied[0].rect.height)
            _embed_full(page,copied,0,kind=kind,rotate=-original.rotation)
            regions.append((page.number,kind,tuple(page.rect)))
        matrix=original.rotation_matrix
        placements.append(RegionPlacement(number,page.number,part,'source' if kind=='en' else 'mono',kind,
            tuple(original.rect*original.derotation_matrix),tuple(page.rect),tuple(matrix)))


def _write_layout(output, reports, regions):
    pages=[]
    for page in output:
        # 即便全部为空白，调用方仍提供页身份；不伪造可划词区域。
        report=reports[page.number//2]
        pages.append({'src':report['source_page'],'part':'ab'[page.number%2],
            'width':page.rect.width,'height':page.rect.height,
            **({'fallback':True} if report['safety_net'] else {}),
            **({'reason':report['reason']} if report['reason'] != 'composed' else {}),
            'regions':[{'k':kind,'r':list(fitz.Rect(box)*~page.transformation_matrix)}
                       for number,kind,box in regions if number==page.number]})
    output.set_metadata({'producer':'BabelDOC for Zotero'})
    _,ref=output.xref_get_key(-1,'Info')
    output.xref_set_key(int(ref.split()[0]),'ZTFSplitLayout',
        fitz.get_pdf_str(json.dumps({'v':1,'pages':pages},separators=(',',':'))))
def _navigation_values(value, size):
    """第三方坐标先检查有限性，再交给 Point / Rect 做运算。"""
    try:
        values = tuple(value)
        return len(values) == size and all(math.isfinite(v) for v in values)
    except (TypeError, ValueError, OverflowError):
        return False


def _navigation_index(placements):
    """按输入文档、源页建立放置索引；hf 只属于原文，span 只属于译文。"""
    index = {}
    for placement in placements:
        index.setdefault((placement.input, placement.src), []).append(placement)
    return index


def _navigation_target(index, source_index, point, side):
    """返回 (输出页、点、回退原因)；无对应页时返回 (None, None, 原因)。

    point 是相应输入文档的未旋转坐标；英文不借用中文跨栏映射。
    页眉页脚或空白落点以最近同侧区域选择 A/B，跳到该侧的页顶。
    """
    items = index.get(("source" if side == "en" else "mono", source_index), [])
    body = [p for p in items if p.k in {side, "span"}]
    if not body:
        return None, None, "target_page_missing"
    valid = _navigation_values(point, 2)
    header = valid and any(p.source_box[1] <= point[1] <= p.source_box[3]
                           for p in index.get(("source", source_index), []) if p.k == "hf")
    if valid:
        point = fitz.Point(point)
        if not header:
            # 扩框可能覆盖邻区空白；中文跨栏优先于普通栏。
            hit = next((p for p in sorted(body, key=lambda p: p.k != "span")
                        if fitz.Rect(p.source_box).contains(point)), None)
            if hit is not None:
                return hit.out, point * fitz.Matrix(hit.matrix), ""

        def distance(p):
            x0, y0, x1, y1 = p.source_box
            return max(x0 - point.x, 0, point.x - x1) ** 2 + max(y0 - point.y, 0, point.y - y1) ** 2

        # hf 在 A、B 重复，选择目标 x 所在的正文栏，不固定跳 A。
        candidates = [p for p in body if p.k == side] if header else body
        chosen = min(candidates or body, key=lambda p: (distance(p), p.out))
    else:
        chosen = min(body, key=lambda p: p.out)
    # span 仅参与选择目标页；它的左边缘不代表任何语言侧的页顶。
    same_side = [p for p in items if p.out == chosen.out and p.k == side]
    if not same_side:
        # 整页跨栏没有正文侧时，落到跨栏元素本身，而非伪造语言侧页顶。
        return chosen.out, fitz.Point(chosen.output_box[:2]), "target_unmapped"
    return chosen.out, fitz.Point(min(p.output_box[0] for p in same_side), 0), (
        "target_header_footer" if header else "target_unmapped")


def _navigation_zoom(zoom):
    """无效缩放回退为 0，原因固定且独立于目标位置回退。"""
    try:
        if isinstance(zoom, (int, float)) and math.isfinite(zoom):
            return zoom, ""
    except OverflowError:
        pass
    return 0, "invalid_zoom"


def _navigation_destination(document, link, index, side):
    """返回目标、位置回退原因、缩放回退原因；两种回退分别计数。"""
    kind = link.get("kind")
    if kind == fitz.LINK_URI:
        uri = link.get("uri")
        return ({"kind": kind, "uri": uri}, "", "") if uri else (None, "empty_uri", "")
    if kind not in {fitz.LINK_GOTO, fitz.LINK_NAMED}:
        return None, "unsupported_link_kind", ""
    page, point = link.get("page", -1), link.get("to")
    name = link.get("nameddest") or (point if isinstance(point, str) else None)
    if name:
        # resolve_link 与 get_links 一样返回可视坐标。
        page, x, y = document.resolve_link("#nameddest=" + name)
        point = (x, y)
    if type(page) is not int or not 0 <= page < len(document):
        return None, "target_page_missing", ""
    if _navigation_values(point, 2):
        point = fitz.Point(point) * document[page].derotation_matrix
    target_page, target_point, reason = _navigation_target(index, page, point, side)
    if target_page is None:
        return None, reason, ""
    zoom, zoom_reason = _navigation_zoom(link.get("zoom", 0))
    return {"kind": fitz.LINK_GOTO, "page": target_page, "to": target_point, "zoom": zoom}, reason, zoom_reason


def _insert_navigation_link(output, page_index, link):
    """用公开接口插入；URI 单独编码，避免 1.28.2 直接拼接括号造成改值。"""
    page = output[page_index]
    if link["kind"] != fitz.LINK_URI:
        page.insert_link(link)
        return
    before = {xref for xref, _, _ in page.annot_xrefs()}
    page.insert_link({**link, "uri": "https://example.invalid/"})
    added = [xref for xref, _, _ in page.annot_xrefs() if xref not in before]
    if len(added) != 1:
        raise ValueError("link_insertion_failed")
    output.xref_set_key(added[0], "A/URI", fitz.get_pdf_str(link["uri"]))


def _rebuild_split_links(source, mono, output, index):
    """返回输入链接数、写入注释数、完全未写入的输入链接数、页顶回退数。

    输入数包含 source、mono 两份；跨栏原文/mono 页眉没有可见区域，
    也计未写入并记录。单个链接可裁成多个注释，hf 可复制到 A、B。
    """
    stats = {"links_source": 0, "links_written": 0, "links_unresolved": 0,
             "links_fallback": 0, "links_zoom_fallback": 0}
    for document, side, input_name in ((source, "en", "source"), (mono, "zh", "mono")):
        for source_index in range(len(document)):
            page = document[source_index]
            for number, link in enumerate(page.get_links()):
                stats["links_source"] += 1
                fields = {"source_index": source_index, "side": side, "link_index": number}
                reason = "anchor_unmapped"
                pieces = []
                if _navigation_values(link.get("from"), 4):
                    box = fitz.Rect(link["from"]) * page.derotation_matrix
                    for placement in index.get((input_name, source_index), []):
                        clipped = box & fitz.Rect(placement.source_box)
                        if not clipped.is_empty:
                            pieces.append((placement, clipped))
                else:
                    reason = "invalid_link_box"
                if pieces:
                    destination, reason, zoom_reason = _navigation_destination(document, link, index, side)
                    if destination is not None:
                        if zoom_reason:
                            stats["links_zoom_fallback"] += 1
                            _log("split_navigation_fallback", zoom_reason, **fields)
                        if reason:
                            stats["links_fallback"] += 1
                            _log("split_navigation_fallback", reason, **fields)
                        for placement, clipped in pieces:
                            rectangle = clipped * fitz.Matrix(placement.matrix)
                            _insert_navigation_link(output, placement.out, {**destination, "from": rectangle})
                            stats["links_written"] += 1
                        continue
                stats["links_unresolved"] += 1
                _log("split_navigation_unresolved", reason, **fields)
    return stats


def _rebuild_split_toc(source, output, index):
    """保留源大纲层级；内部目标按英文侧映射，失败保留无动作条目。"""
    toc = []
    external = []
    stats = {"toc_entries": 0, "toc_unresolved": 0, "toc_zoom_fallback": 0}
    for number, (level, title, page, destination) in enumerate(source.get_toc(simple=False)):
        destination = dict(destination)
        destination.pop("xref", None)
        if destination.get("kind") in {fitz.LINK_GOTO, fitz.LINK_NAMED}:
            mapped, reason, zoom_reason = _navigation_destination(source, destination, index, "en")
            if zoom_reason:
                stats["toc_zoom_fallback"] += 1
                _log("split_navigation_fallback", zoom_reason, toc_index=number, side="en")
            if reason:
                _log("split_navigation_fallback" if mapped else "split_navigation_unresolved",
                     reason, toc_index=number, side="en")
            if mapped:
                destination = {**destination, **mapped}
                destination.pop("nameddest", None)
                page = mapped["page"] + 1
            else:
                stats["toc_unresolved"] += 1
                destination = {"kind": fitz.LINK_NONE}
                page = -1
        elif destination.get("kind") == fitz.LINK_NONE:
            page = -1
        else:
            # 1.28.2 set_toc 的 tuple 与 GOTOR 编码要求的 Point 不兼容；
            # get_toc 对远程命名目标和 LAUNCH 的 kind 也不能可靠往返。
            # 文件类及未知动作明确停用，保留标题/层级；URI 独立写入。
            if destination.get("kind") == fitz.LINK_URI:
                external.append((number, destination))
            else:
                stats["toc_unresolved"] += 1
                _log("split_navigation_unresolved", "unsupported_toc_action",
                     toc_index=number, side="en")
            destination = {"kind": fitz.LINK_NONE}
            page = -1
        toc.append([level, title, page, destination])
    if toc:
        output.set_toc(toc, collapse=0)
    for number, destination in external:
        try:
            output.set_toc_item(number, dest_dict=destination)
        except Exception as error:
            stats["toc_unresolved"] += 1
            _log("split_navigation_unresolved", "toc_action_failed", toc_index=number,
                 side="en", error_type=type(error).__name__)
    stats["toc_entries"] = len(toc)
    return stats


def _pdf_label_string(text):
    """ASCII 按 PDF 字面量转义；非 ASCII 一律使用带 BOM 的 UTF-16BE。"""
    if not text.isascii():
        return "<feff" + text.encode("utf-16-be").hex() + ">"
    return "(" + "".join(
        "\\" + char if char in "()\\" else
        f"\\{ord(char):03o}" if ord(char) < 32 or ord(char) == 127 else char
        for char in text) + ")"


def _source_label_rules(source):
    """读取完整数字树，保留规则对象；不经过丢失字符串转义的 get_label。"""
    pdf = fitz.mupdf
    document = pdf.PdfDocument(source.this)
    catalog = pdf.pdf_load_object(document, source.pdf_catalog())
    root = pdf.pdf_dict_gets(catalog, "PageLabels")
    if pdf.pdf_is_null(root):
        return []  # 缺省页序号是正常情况，不属于读取失败。
    pending, visited, rules = [root], set(), {}
    while pending:
        node = pending.pop()
        if pdf.pdf_is_indirect(node):
            xref = pdf.pdf_to_num(node)
            if xref in visited:
                raise ValueError("invalid_label_tree")
            visited.add(xref)
        if not pdf.pdf_is_dict(node):
            raise ValueError("invalid_label_tree")
        nums, kids = (pdf.pdf_dict_gets(node, key) for key in ("Nums", "Kids"))
        if not pdf.pdf_is_null(nums):
            if not pdf.pdf_is_null(kids) or not pdf.pdf_is_array(nums) or pdf.pdf_array_len(nums) % 2:
                raise ValueError("invalid_label_tree")
            for offset in range(0, pdf.pdf_array_len(nums), 2):
                key = pdf.pdf_array_get(nums, offset)
                if not pdf.pdf_is_int(key):
                    raise ValueError("invalid_label_tree")
                start = pdf.pdf_to_int(key)
                if start < 0 or start in rules:
                    raise ValueError("invalid_label_tree")
                rules[start] = pdf.pdf_array_get(nums, offset + 1)
        elif pdf.pdf_is_array(kids) and pdf.pdf_array_len(kids):
            pending.extend(pdf.pdf_array_get(kids, i) for i in range(pdf.pdf_array_len(kids)))
        else:
            raise ValueError("invalid_label_tree")
    if 0 not in rules:
        raise ValueError("invalid_label_tree")
    return sorted(rules.items())


def _source_page_label(source, rules, number):
    """按 PDF 标签规则展开 /S、/P、/St，字母编号为 A…Z、AA、BB…。"""
    if not rules:
        return str(number + 1)
    start, rule = next(item for item in reversed(rules) if item[0] <= number)
    pdf = fitz.mupdf
    if not pdf.pdf_is_dict(rule):
        raise ValueError("invalid_label_rule")
    prefix_obj, style_obj, first_obj = (pdf.pdf_dict_gets(rule, key) for key in ("P", "S", "St"))
    prefix, style, first = "", "", 1
    if not pdf.pdf_is_null(prefix_obj):
        if not pdf.pdf_is_string(prefix_obj):
            raise ValueError("invalid_label_prefix")
        # 与 1.28.2 xref_get_key 的 string 分支使用同一 PDF 字符串解码器。
        # 直接规则没有 xref；底层解码同时支持字面量、八进制、十六进制及 UTF-16BE。
        prefix = pdf.pdf_to_text_string(prefix_obj)
    if not pdf.pdf_is_null(style_obj):
        if not pdf.pdf_is_name(style_obj):
            raise ValueError("invalid_label_style")
        style = pdf.pdf_to_name(style_obj)
        if style not in {"D", "R", "r", "A", "a"}:
            raise ValueError("invalid_label_style")
    if not pdf.pdf_is_null(first_obj):
        if not pdf.pdf_is_int(first_obj) or pdf.pdf_to_int(first_obj) < 1:
            raise ValueError("invalid_label_start")
        first = pdf.pdf_to_int(first_obj)
    value = first + number - start
    suffix = ""
    if style == "D":
        suffix = str(value)
    elif style in {"A", "a"}:
        base = ord("A" if style == "A" else "a")
        suffix = chr(base + (value - 1) % 26) * ((value - 1) // 26 + 1)
    elif style in {"R", "r"}:
        for size, roman in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                            (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                            (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
            count, value = divmod(value, size)
            suffix += roman * count
        if style == "r":
            suffix = suffix.lower()
    return prefix + suffix


def _write_split_labels(source, output, plans, placements):
    """逐页写固定标签；单项失败写空规则，避免继承相邻页的标签。"""
    columns = {plan.source_index: plan.columns for plan in plans}
    pages = {p.out: p for p in placements}
    nums = []
    stats = {"labels": 0, "labels_unresolved": 0, "labels_read_fallback": 0}
    tree_error = None
    try:
        rules = _source_label_rules(source)
    except Exception as error:
        rules, tree_error = [], type(error).__name__
    for number in range(len(output)):
        placement = pages[number]
        read_error = tree_error
        label = str(placement.src + 1)
        if read_error is None:
            try:
                label = _source_page_label(source, rules, placement.src) or label
            except Exception as error:
                read_error = type(error).__name__
        if read_error:
            stats["labels_read_fallback"] += 1
            stats["labels_unresolved"] += 1
            _log("split_navigation_fallback", "label_read_failed",
                 source_index=placement.src, output_index=number, error_type=read_error)
        try:
            if columns[placement.src] == 2:
                label += placement.part
            # 1.28.2 set_page_labels 不转义 /P；直接写数字树的独立规则对象。
            # 标签已展开为完整文本，与此前一样不设 /S，/St 默认 1。
            encoded = _pdf_label_string(label)
            xref = output.get_new_xref()
            output.update_object(xref, "<<>>")
            output.xref_set_key(xref, "P", encoded)
            # 底层接口按 PDF 字符串规则解码；get_label 的读取缺陷不影响写入。
            if output.xref_get_key(xref, "P") != ("string", label):
                raise ValueError("page_label_decode_mismatch")
            nums.append(f"{number} {xref} 0 R")
            stats["labels"] += 1
        except Exception as error:
            # 不挂入可能已部分写入的对象；空规则也覆盖首项与末项失败。
            nums.append(f"{number} <<>>")
            if not read_error:
                stats["labels_unresolved"] += 1
            _log("split_navigation_unresolved", "label_write_failed",
                 source_index=placement.src, output_index=number,
                 error_type=type(error).__name__)
    output.xref_set_key(output.pdf_catalog(), "PageLabels", "<</Nums [" + " ".join(nums) + "]>>")
    return stats


def _rebuild_split_navigation(source, mono, output, plans, placements):
    """保存前独立重建导航，不改变合成区域；异常由公开入口统一脱敏。"""
    index = _navigation_index(placements)
    stats = _rebuild_split_links(source, mono, output, index)
    stats.update(_rebuild_split_toc(source, output, index))
    stats.update(_write_split_labels(source, output, plans, placements))
    return stats




def compose_split_document(source_pdf, mono_pdf, layout, output_pdf, *, log=None):
    """消费 T4 sidecar；逐源页事务，固定原因码日志；原子替换输出。"""
    temporary=None
    try:
        with ExitStack() as stack:
            source=stack.enter_context(fitz.open(source_pdf))
            mono=stack.enter_context(fitz.open(mono_pdf))
            output=stack.enter_context(fitz.open())
            if len(source)!=len(mono): raise ValueError('page_count_mismatch')
            try:
                mapping=json.loads(Path(str(mono_pdf)+'.variants.json').read_text(encoding='utf-8'))
                if mapping.get('version') != 1:
                    raise ValueError('variant_version')
            except (OSError, ValueError):
                mapping={'pages':[]}
            entries={e['source_page']:e for e in mapping['pages']}
            try:
                variants=stack.enter_context(fitz.open(mapping['pdf_path'])) if mapping.get('pdf_path') else None
            except Exception:
                variants=None
            analysis=analyze_source(source,layout)
            origins, column_origin_missing = column_origin_status(source,analysis,entries)
            column_shift=origins[1]-origins[0] if not any(origin is None for origin in origins) else 0
            if column_origin_missing:
                _log('split_compose_single_column','column_origin_missing',count=len(source))
                if log:
                    log({'event':'split_compose_single_column','reason':'column_origin_missing',
                         'count':len(source)})
            layout_pages={p['index']:p for p in layout.get('pages',())}
            placements=[];reports=[];plans=[];regions=[]
            for number in range(len(source)):
                entry=entries.get(number,{})
                start,count,region_count=len(output),len(placements),len(regions)
                safety=False;reason='composed'
                try:
                    if number in analysis[4]: raise ValueError('source_page_error')
                    if entry.get('error'): raise ValueError('t4_page_error')
                    if entry.get('columns') not in (1,2): raise ValueError('t4_page_missing')
                    if entry['columns']==1 or (entry['columns']==2 and column_origin_missing):
                        _whole_pages(source,mono,number,output,placements,regions)
                    else:
                        if variants is None: raise ValueError('variants_missing')
                        _compose_columns(source,mono,entry,variants,output,placements,analysis,regions,
                                         layout_pages.get(number,{}),column_shift)
                except Exception:
                    # 第三方异常不进入日志（可能包含正文或凭据）。
                    if len(output)>start: output.delete_pages(from_page=start,to_page=len(output)-1)
                    del placements[count:]
                    del regions[region_count:]
                    safety=True
                    reason=('t4_page_error' if entry.get('error') else
                            'source_analysis_error' if number in analysis[4] else
                            'variant_data_missing' if entry.get('columns') not in (1,2)
                            or (entry['columns']==2 and variants is None) else 'compose_error')
                    _whole_pages(source,mono,number,output,placements,regions,expand=False)
                heights=[output[i].rect.height for i in range(start,len(output))]
                handled_columns = (1 if entry.get('columns')==2 and column_origin_missing else
                                   entry.get('columns') if entry.get('columns') in (1,2) else 1)
                reason = ('column_origin_missing' if entry.get('columns')==2 and column_origin_missing
                          else reason)
                report={'source_page':number,'columns':handled_columns,
                    'basis':entry.get('basis','single') if entry.get('basis') in ('tag','jump','single') else 'single',
                    'margin_basis':sorted(set(analysis[3][number].values())),
                    'grown':[h>source[number].rect.height for h in heights],
                    'safety_net':safety,'reason':reason}
                reports.append(report)
                plans.append(_Page(number,0 if safety else handled_columns))
                if log and not column_origin_missing:
                    log({'event':'split_compose_page','page':number,'reason':reason})
            _write_layout(output,reports,regions)
            navigation=_rebuild_split_navigation(source,mono,output,plans,placements)
            handle=tempfile.NamedTemporaryFile(prefix='.ztf-split-',suffix='.pdf',
                dir=os.path.dirname(os.path.abspath(output_pdf)),delete=False)
            temporary=handle.name;handle.close()
            output.save(temporary,garbage=4,deflate=True)
            os.replace(temporary,output_pdf);temporary=None
            return {'input_pages':len(source),'output_pages':len(output),'regions':len(placements),
                    'fallback_pages':sum(r['safety_net'] for r in reports),
                    'column_origin_missing':len(source) if column_origin_missing else 0,
                    'page_results':reports,**navigation}
    except Exception:
        raise RuntimeError('split_compose_failed') from None
    finally:
        if temporary is not None:
            try: os.remove(temporary)
            except OSError: _log('split_compose_cleanup_error','temporary_remove_failed')
