"""翻译等待期间的单进程源侧预计算；失败仅导致现有串行路径重算。"""
import atexit
import hashlib
import json
import os
from pathlib import Path
import pickle
import shutil
import subprocess
import sys
import time

_active = None
MIN_MEMORY = 4 * 1024**3


def available_memory():
    import psutil
    return psutil.virtual_memory().available


def source_inputs(layout, entries):
    """只保留源侧算法实际读取的字段，禁止带入译文 output_box。"""
    return {
        'layout': {'pages': [
            {'index': p['index'],
             'paragraphs': [{'index': v.get('index', i), 'box': v['box']}
                            for i, v in enumerate(p.get('paragraphs', ()))],
             'layouts': [{'class': v.get('class'), 'box': v['box']}
                         for v in p.get('layouts', ())]}
            for p in layout.get('pages', ())]},
        'entries': [{k: p[k] for k in ('source_page', 'columns', 'basis', 'gutters', 'paragraph_column')
                     if k in p} for p in entries],
    }


def fingerprint(source, inputs):
    code = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('ztf_*.py')):
        code.update(path.name.encode()); code.update(path.read_bytes())
    return {'source': hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            'layout': hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':'),
                                                allow_nan=False).encode()).hexdigest(),
            'code': code.hexdigest()}


class Precompute:
    def __init__(self, source, layout, entries, working, log=None):
        self.directory = Path(working) / '.split-precompute'
        self.process = None
        self.log = log
        self.reason = 'not_started'
        self.wait_seconds = min(10.0, max(1.0, len(layout.get('pages', ()))*0.5))
        self.inputs = source_inputs(layout, entries)
        self.started = time.perf_counter()
        self.worker_seconds = None
        try:
            if os.environ.get('ZTF_SPLIT_PRECOMPUTE') == '0':
                self.reason = 'disabled'
            elif available_memory() < MIN_MEMORY:
                self.reason = 'low_memory'
            else:
                self.directory.mkdir(parents=True, exist_ok=True)
                payload = {'source': str(Path(source).resolve()), 'inputs': self.inputs,
                           'fingerprint': fingerprint(source, self.inputs)}
                (self.directory / 'input.json').write_text(json.dumps(payload, allow_nan=False), encoding='utf-8')
                flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                         if os.name == 'nt' else 0)
                environment = {k:v for k,v in os.environ.items()
                               if k not in ('ZTF_OPENAI_API_KEY', 'OPENAI_API_KEY')}
                self.process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8',
                    str(Path(__file__).resolve()), str(self.directory.resolve())],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags, env=environment)
                self.reason = 'started'
        except Exception:
            self.reason = 'start_failed'
            self.stop()
        self.emit('split_precompute_start', enabled=self.process is not None, reason=self.reason)

    def emit(self, event, **fields):
        import ztf_timing as timing
        if timing.enabled() and self.log:
            self.log({'event': event, **fields})

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            if self.process.stdin:
                self.process.stdin.close()

    def consume(self, source, layout, entries):
        started = time.perf_counter()
        value = None
        reason = self.reason
        try:
            if self.process is not None:
                result = self.directory / 'result.pickle'
                deadline = time.perf_counter() + self.wait_seconds
                while self.process.poll() is None and not result.is_file():
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        reason = 'timeout'
                        self.stop()
                        break
                    time.sleep(min(.02, remaining))
                if reason != 'timeout':
                    if self.process.poll() is not None and self.process.returncode:
                        reason = 'worker_failed'
                    elif not result.is_file():
                        reason = 'result_missing'
                    else:
                        with result.open('rb') as handle:
                            candidate = pickle.load(handle)
                        self.worker_seconds = candidate['seconds']
                        if candidate['fingerprint'] != fingerprint(source, source_inputs(layout, entries)):
                            reason = 'fingerprint_mismatch'
                        else:
                            value = candidate
                            reason = 'hit'
        except Exception:
            reason = 'result_invalid'
        self.emit('split_precompute_result', hit=value is not None, reason=reason,
                  worker_seconds=self.worker_seconds, wait_seconds=round(time.perf_counter()-started, 4))
        return value

    def close(self):
        self.stop()
        if self.directory.exists():
            shutil.rmtree(self.directory)


def start(source, layout, entries, working, *, log=None):
    global _active
    cleanup()
    _active = Precompute(source, layout, entries, working, log)
    return _active


def consume(source, layout, entries):
    return _active.consume(source, layout, entries) if _active else None


def cleanup():
    global _active
    if _active:
        _active.close()
        _active = None


def worker(directory, cancelled):
    def check_cancelled():
        if cancelled.is_set():
            raise InterruptedError('parent_closed')
    if os.name != 'nt':
        os.nice(5)
    import pymupdf as fitz
    import ztf_split_compose as compose
    import ztf_figure_background as background
    from ztf_split_types import MARGIN, SPANNING
    from ztf_split_variants import object_region
    started = time.perf_counter()
    payload = json.loads((directory / 'input.json').read_text(encoding='utf-8'))
    layout = payload['inputs']['layout']
    entries = {p['source_page']: p for p in payload['inputs']['entries']}
    pages = {p['index']: p for p in layout['pages']}
    with fitz.open(payload['source']) as source:
        analysis = compose.analyze_source(source, layout)
        check_cancelled()
        origins, missing = compose.column_origin_status(source, analysis, entries)
        layers = {}
        for number, page in enumerate(source):
            entry = entries.get(number, {})
            if (number in analysis[4] or entry.get('columns') != 2 or missing or page.rotation):
                continue
            width, height = page.rect.width, page.rect.height
            owners = compose._owners(number, entry, analysis, width, height)
            gutters = tuple(tuple(pair) for pair in entry.get('gutters', ()))
            clips = tuple(tuple(v['box']) for v in pages.get(number, {}).get('layouts', ())
                          if v.get('class') in background.FIGURE_CLASSES
                          and background._owner(tuple(v['box']), (), gutters) == SPANNING)
            graphic_cache = {}
            saved = {}
            for kind, owner in (('hf', MARGIN), ('en0', 0), ('en1', 1)):
                check_cancelled()
                selected = {i for i, value in owners.items() if value == owner}
                graphic = ((lambda box: False) if owner == MARGIN else
                    (lambda box, c=owner: object_region(tuple(box), analysis[2][number],
                                                       ((width/2, width/2),)) == c))
                with compose._layer(source, number, selected, graphic, graphic_cache=graphic_cache) as doc:
                    background.clip_source_layer_outside_figures(doc[0], clips)
                    filename = f'{number}-{kind}.pdf'
                    selections = [tuple(box) for box in compose._text_boxes(doc[0])]
                    boxes = [tuple(box) for box in compose._content_boxes(doc[0])] if owner != MARGIN else []
                    doc.save(directory / filename)
                    saved[kind] = {'file': filename, 'boxes': boxes, 'selections': selections}
            layers[number] = saved
    value = {'fingerprint': payload['fingerprint'], 'analysis': analysis, 'origins': origins,
             'origin_missing': missing, 'layers': layers, 'seconds': time.perf_counter()-started}
    check_cancelled()
    with (directory / 'result.pickle.tmp').open('wb') as handle:
        pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(directory / 'result.pickle.tmp', directory / 'result.pickle')


atexit.register(cleanup)
if __name__ == '__main__':
    import threading
    directory = Path(sys.argv[1])
    cancelled = threading.Event()
    def parent_closed():
        os.read(sys.stdin.fileno(), 1)
        cancelled.set()
    threading.Thread(target=parent_closed, daemon=True).start()
    exit_code = 0
    try:
        worker(directory, cancelled)
        cancelled.wait()
    except Exception:
        exit_code = 1
    finally:
        if cancelled.is_set():
            shutil.rmtree(directory, ignore_errors=True)
    os._exit(exit_code)
