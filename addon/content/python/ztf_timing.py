"""默认关闭的保存计时；输入固定挂点名，输出结构化统计，不检查正文/异常。

runner 管理唯一进程级会话，并传入既有脱敏日志函数。functions 是包含子调用的
墙钟时间，stages 只计最外层入口，不可与 functions 相加。产物只读检查失败写
固定 unavailable；日志失败不影响原函数。无全局 tracing，不改变子进程 target。
"""
import os
import re
import sys
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any

_active = None
_source = ContextVar('ztf_timing_source', default=())
_HOOKS = {
    'ztf_split_variants': ('create_variants', '_copy_render_page', '_isolate_form_streams',
                           'render_variants', 'inject_background_sequence', 'inject_backgrounds'),
    'ztf_split_compose': ('analyze_source', 'build_lines', 'read_pdf_evidence', 'classify_margins',
                          'body_starts', 'column_translation', 'column_origin_status', '_owners',
                          '_layer', '_filter', '_content_boxes', '_text_boxes', '_selection',
                          '_embed_full', '_record', '_whole_pages', '_write_layout',
                          '_rebuild_split_navigation', '_rebuild_split_links', '_rebuild_split_toc',
                          '_write_split_labels', 'compose_split_document'),
    'ztf_figure_background': ('clip_source_layer_outside_figures', 'inject_background_sequence',
                             'inject_backgrounds'),
    'ztf_figure_text': ('restore_figure_text',),
    'ztf_punctuation_cleanup': ('clean_duplicate_punctuation',),
    'ztf_links': ('restore_links', 'restore_dual_links', '_source_links', '_restore_one',
                  '_text_chars', '_graphics_region_preserved'),
    'babeldoc.format.pdf.high_level': ('fix_cmap', 'add_metadata', 'safe_save', 'reproduce_cmap'),
}


def enabled(directory: str | Path | None = None, environ: Mapping | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get('ZTF_TIMING') == '1'
            or Path(directory or Path(__file__).parent, 'ztf_timing.enabled').is_file())


def install(log: Callable, *, directory: str | Path | None = None,
            environ: Mapping | None = None) -> 'Timing | None':
    """显式开关开启才创建会话与替换函数；关闭返回 None，完全不替换目标。"""
    global _active
    if not enabled(directory, environ):
        return None
    if _active is None:
        _active = Timing(log)
        _active.refresh()
    return _active


def uninstall() -> None:
    global _active
    if _active is not None:
        _active.restore()
        _active = None


def patch(owner: Any, name: str, prefix: str) -> None:
    """给已有闭包属性安装可恢复包装；关闭时不读写目标属性。"""
    if _active is not None:
        _active.patch(owner, name, prefix)


def call(name: str, function: Callable, *args, **kwargs) -> Any:
    if _active is None:
        return function(*args, **kwargs)
    with _active.measure(name):
        return function(*args, **kwargs)


@contextmanager
def measure(name: str):
    if _active is None:
        yield
    else:
        with _active.measure(name):
            yield


def page_started() -> float | None:
    return perf_counter() if _active is not None else None


def page_finished(started: float | None, number: int, placements: int, regions: int) -> None:
    if _active is not None and started is not None:
        _active.pages.append({'page': number, 'seconds': perf_counter() - started,
                              'placements': placements, 'regions': regions})


class Timing:
    """单次 runner 任务的函数累计、最外层阶段与逐源页记录。"""

    def __init__(self, log: Callable):
        self.log = log
        self.functions = {}
        self.stages = {}
        self.pages = []
        self.patches = []
        self.lock = Lock()
        self.finished = False

    @contextmanager
    def measure(self, name: str):
        parents = _source.get()
        token = _source.set((*parents, name))
        started = perf_counter()
        try:
            yield
        finally:
            elapsed = perf_counter() - started
            _source.reset(token)
            with self.lock:
                item = self.functions.setdefault(name, {'calls': 0, 'seconds': 0.0, 'max': 0.0})
                item['calls'] += 1
                item['seconds'] += elapsed
                item['max'] = max(item['max'], elapsed)
                if not parents:
                    stage = self.stages.setdefault(name, {'calls': 0, 'seconds': 0.0, 'max': 0.0})
                    stage['calls'] += 1
                    stage['seconds'] += elapsed
                    stage['max'] = max(stage['max'], elapsed)

    def wrap(self, function: Callable, name: str) -> Callable:
        @wraps(function)
        def timed(*args, **kwargs):
            # 来源仅由固定函数名组成，不读取参数、结果或错误文本。
            label = name
            if '[' not in name:
                parents = _source.get()
                source = parents[-1].split('[', 1)[0].rsplit('.', 1)[-1] if parents else 'save'
                if name.endswith(('._filter', '._GraphicObjects.run')):
                    source = next((parent.split('[', 1)[0].rsplit('.', 1)[-1]
                                   for parent in reversed(parents)
                                   if parent.split('[', 1)[0].rsplit('.', 1)[-1] in
                                   ('analyze_source', '_layer', '_content_boxes', '_text_boxes')),
                                  source)
                label = f'{name}[{source}]'
            with self.measure(label):
                return function(*args, **kwargs)
        timed._ztf_timing_session = self
        return timed

    def patch(self, owner: Any, name: str, prefix: str) -> None:
        original = getattr(owner, name, None)
        if not callable(original) or getattr(original, '_ztf_timing_session', None) is self:
            return
        raw = vars(owner).get(name)
        wrapped = self.wrap(original, f'{prefix}.{name}')
        replacement = staticmethod(wrapped) if isinstance(raw, staticmethod) else wrapped
        setattr(owner, name, replacement)
        self.patches.append((owner, name, raw, replacement))

    def instrument(self, module: Any) -> None:
        prefix = module.__name__
        for name in _HOOKS.get(prefix, ()):
            self.patch(module, name, prefix)
        if prefix == 'ztf_split_compose':
            self.patch(module._GraphicObjects, 'run', prefix + '._GraphicObjects')
        if prefix == 'babeldoc.format.pdf.document_il.backend.pdf_creater':
            for name in ('__init__', 'subset_fonts_in_subprocess', 'save_pdf_with_timeout'):
                self.patch(module.PDFCreater, name, prefix + '.PDFCreater')

    def refresh(self) -> None:
        # 只处理已加载的模块，不为计时提前导入/初始化后端。
        for name in (*_HOOKS, 'babeldoc.format.pdf.document_il.backend.pdf_creater'):
            module = sys.modules.get(name)
            if module is not None:
                self.instrument(module)

    def restore(self) -> None:
        for owner, name, original, replacement in reversed(self.patches):
            if vars(owner).get(name) is replacement:
                setattr(owner, name, original)
        self.patches.clear()

    def emit(self, value: dict) -> None:
        try:
            self.log(value)
        except Exception:  # noqa: BLE001 - 诊断日志失败不能改变任务结果，也不能递归记录自己
            return

    def finish(self, paths: Mapping | None = None) -> None:
        """所有文件写完后输出 artifacts 与 summary；paths 键为 split/mono/variants。"""
        if self.finished:
            return
        self.finished = True
        if paths is not None:
            self.emit(artifacts(paths))
        self.emit({'event': 'timing_summary', 'stages': self.stages,
                   'functions': self.functions, 'compose_pages': self.pages})


def peak_memory() -> int | str:
    """操作系统记录的本进程生命周期峰值工作集，单位 bytes，不含子进程。"""
    try:
        import psutil
        return int(psutil.Process().memory_info().peak_wset)
    except Exception:  # noqa: BLE001 - 可选 psutil 不可用时走已核实的 Win32 接口
        return _windows_peak_memory()


def _windows_peak_memory() -> int | str:
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in (
                        'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                        'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                        'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            kernel.GetCurrentProcess.argtypes = []
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            query = psapi.GetProcessMemoryInfo
            query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            query.restype = wintypes.BOOL
            info = Counters()
            info.cb = ctypes.sizeof(info)
            if query(kernel.GetCurrentProcess(), ctypes.byref(info), info.cb):
                return int(info.PeakWorkingSetSize)
        except (AttributeError, OSError):
            return 'unavailable'
    return 'unavailable'


def embedded_fonts(document: Any) -> list[dict]:
    """字体按 xref 跨页去重，Type0 先读 DescendantFonts，再读 FontDescriptor。"""
    font_objects = {}
    for number in range(len(document)):
        for entry in document.get_page_fonts(number, full=True):
            if entry[0]:
                font_objects.setdefault(entry[0], entry[3])
    records = []
    for xref, name in font_objects.items():
        descendants = document.xref_get_key(xref, 'DescendantFonts')
        candidates = [xref]
        if descendants[0] == 'array':
            candidates = [int(value) for value in re.findall(r'(\d+)\s+\d+\s+R', descendants[1])]
        streams = set()
        for candidate in candidates:
            for key in ('FontFile', 'FontFile2', 'FontFile3'):
                kind, value = document.xref_get_key(candidate, 'FontDescriptor/' + key)
                if kind == 'xref':
                    streams.add(int(value.split()[0]))
        if streams:
            records.append({'name': name, 'font_file_bytes': sum(
                len(document.xref_stream_raw(stream) or b'') for stream in streams),
                'subset': bool(re.match(r'^[A-Z]{6}\+', name))})
    for record in records:
        record['same_name_count'] = sum(other['name'] == record['name'] for other in records)
    return records


def artifacts(paths: Mapping) -> dict:
    """只读 paths 的 split/mono/variants；返回大小、split 字体与本进程峰值内存。"""
    sizes = {}
    # 只记录固定产物角色，不记录可能带正文、账号信息的路径。
    for role in ('split', 'mono', 'variants'):
        path = paths.get(role)
        try:
            sizes[role] = Path(path).stat().st_size if path else 'unavailable'
        except (OSError, ValueError, TypeError):
            sizes[role] = 'unavailable'
    fonts = 'unavailable'
    try:
        import pymupdf
        if paths.get('split'):
            with pymupdf.open(paths['split']) as document:
                fonts = embedded_fonts(document)
    except Exception:  # noqa: BLE001 - 第三方只读检查失败只写固定 unavailable，不记录异常
        fonts = 'unavailable'
    return {'event': 'timing_artifacts', 'pdf_bytes': sizes, 'fonts': fonts,
            'embedded_font_count': len(fonts) if isinstance(fonts, list) else 'unavailable',
            'peak_process_memory_bytes': peak_memory()}
