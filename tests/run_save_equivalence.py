#!/usr/bin/env python3
"""真实 BabelDOC + 本地合成译文的保存等价性检查；非 Zotero 真机验收。

默认依次运行每个样本的 baseline、baseline-repeat、working；重复基线不一致
时不认可该样本的优化等价性。所有实验产物位于 .scratch/t082/t1，基线代码
仅用 git archive 导出到 .scratch/t082/base。需指定环境的 Python 运行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.scratch/t082/t1'
BASE = ROOT / '.scratch/t082/base'
PYTHON = Path('G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe')

# 只在实验子进程捕获返回值；不修改导出的基线或生产 runner。
CAPTURE_RUNNER = '''import functools
import json
from pathlib import Path
import runpy
import sys
def capture():
    runner, stats_path, *arguments = sys.argv[1:]
    sys.path.insert(0, str(Path(runner).parent))
    sys.argv = [runner, *arguments]
    namespace = runpy.run_path(runner, run_name="equivalence_capture")
    main = namespace["main"]
    original_load = main.__globals__["_load_helper"]
    def load(name):
        helper = original_load(name)
        if name == "ztf_split_compose" and not getattr(helper, "_equivalence_capture", False):
            original_compose = helper.compose_split_document
            @functools.wraps(original_compose)
            def compose(*args, **kwargs):
                result = original_compose(*args, **kwargs)
                Path(stats_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                return result
            helper.compose_split_document = compose
            helper._equivalence_capture = True
        return helper
    main.__globals__["_load_helper"] = load
    return main()
if __name__ == "__main__":
    raise SystemExit(capture())
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalize(value):
    """精确保留数值；xref/id 是分配身份，不是链接/目录的语义字段。"""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if k not in ('xref', 'id')}
    if isinstance(value, (tuple, list)):
        return [normalize(v) for v in value]
    if isinstance(value, bytes):
        return {'bytes': len(value), 'sha256': digest(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return [normalize(v) for v in value]  # Point / Rect


def pdf_snapshot(path):
    import pymupdf as fitz
    with fitz.open(path) as doc:
        _, info = doc.xref_get_key(-1, 'Info')
        layout = json.loads(doc.xref_get_key(int(info.split()[0]), 'ZTFSplitLayout')[1])
        pages = []
        for page in doc:
            pixels = {}
            for dpi in (72, 150):
                pix = page.get_pixmap(dpi=dpi)
                pixels[str(dpi)] = {'width': pix.width, 'height': pix.height,
                                    'stride': pix.stride, 'n': pix.n, 'alpha': pix.alpha,
                                    'sha256': digest(pix.samples)}
            pages.append({'size': list(page.rect),
                          'rawdict': normalize(page.get_text('rawdict')),
                          'pixels': pixels, 'links': normalize(page.get_links()),
                          'label': page.get_label()})
        return {'page_count': len(doc), 'pages': pages, 'layout': layout,
                'toc': normalize(doc.get_toc(simple=False)),
                'page_labels': normalize(doc.get_page_labels())}


def compare_pdfs(left, right):
    a, b = pdf_snapshot(left), pdf_snapshot(right)
    checks = {key: a[key] == b[key] for key in ('page_count', 'layout', 'toc', 'page_labels')}
    differences = [{'page': None, 'type': key} for key, same in checks.items() if not same]
    for key in ('size', 'rawdict', 'pixels_72', 'pixels_150', 'links', 'label'):
        checks[key] = len(a['pages']) == len(b['pages'])
    for i, (p, q) in enumerate(zip(a['pages'], b['pages'])):
        for key in ('size', 'rawdict', 'links', 'label'):
            if p[key] != q[key]:
                checks[key] = False
                differences.append({'page': i+1, 'type': key})
        for dpi in ('72', '150'):
            if p['pixels'][dpi] != q['pixels'][dpi]:
                checks['pixels_'+dpi] = False
                differences.append({'page': i+1, 'type': 'pixels_'+dpi})
    for i in range(min(len(a['pages']), len(b['pages'])), max(len(a['pages']), len(b['pages']))):
        differences.append({'page': i+1, 'type': 'missing_or_extra_page'})
    if a['layout'] != b['layout']:
        for i, (p, q) in enumerate(zip(a['layout']['pages'], b['layout']['pages'])):
            if p != q:
                differences.append({'page': i+1, 'type': 'layout'})
    raw_a, raw_b = Path(left).read_bytes(), Path(right).read_bytes()
    byte_comparison = {'identical': raw_a == raw_b,
                       'sha256': [digest(raw_a), digest(raw_b)]}
    if raw_a != raw_b:
        import pymupdf as fitz
        with fitz.open(left) as x, fitz.open(right) as y:
            same_objects = x.xref_length() == y.xref_length() and all(
                x.xref_object(i) == y.xref_object(i) and
                x.xref_stream_raw(i) == y.xref_stream_raw(i)
                for i in range(1, x.xref_length()))
            byte_comparison.update({'indirect_objects_and_raw_streams_identical': same_objects,
                                    'trailer_ids': [x.xref_get_key(-1, 'ID'), y.xref_get_key(-1, 'ID')],
                                    'reason': 'PDF trailer ID varies on save' if same_objects else
                                              'PDF serialization differs; see semantic checks'})
    return {'status': 'PASS' if all(checks.values()) else 'FAIL', 'checks': checks,
            'differences': differences, 'bytes': byte_comparison}


def export_baseline(commit):
    archive = OUTPUT / 'baseline.tar'
    with archive.open('wb') as handle:
        subprocess.run(['git', 'archive', commit, 'addon/content/python'], cwd=ROOT,
                       stdout=handle, check=True)
    BASE.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as handle:
        handle.extractall(BASE, filter='data')
    return BASE / 'addon/content/python/ztf_babeldoc_runner.py'


def run_translation(python, name, source, label, runner, endpoint, environment, server, run, timeout):
    directory = run / name / label
    directory.mkdir(parents=True)
    out, work = directory / 'out', directory / 'work'
    out.mkdir(); work.mkdir()
    input_pdf = directory / (name+'.pdf')
    shutil.copyfile(source, input_pdf)
    stats = directory / 'compose-stats.json'
    log = directory / 'runner.jsonl'
    env = dict(environment, ZTF_JOB_LOG_PATH=str(log), ZTF_TIMING='1')
    command = [str(python), '-B', '-X', 'utf8', str(OUTPUT / 'capture_runner.py'),
               str(runner), str(stats), '--files', str(input_pdf), '--output', str(out),
               '--working-dir', str(work), '--openai', '--openai-model', 'mock-model',
               '--openai-base-url', endpoint, '--lang-in', 'en', '--lang-out', 'zh', '--no-dual',
               '--no-auto-extract-glossary', '--disable-same-text-fallback', '--ignore-cache',
               '--watermark-output-mode', 'no_watermark', '--report-interval', '1']
    before, started = server.request_count, time.monotonic()
    print(json.dumps({'sample': name, 'run': label, 'event': 'started'}), flush=True)
    proc = subprocess.Popen(command, cwd=directory, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        raise RuntimeError('runner_timeout') from None
    events = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]
    pdfs = list(out.glob('*.dual.pdf'))
    if proc.returncode or len(pdfs) != 1 or not stats.is_file() or server.request_count <= before:
        raise RuntimeError('runner_missing_successful_output')
    summaries = [e for e in events if e.get('event') == 'timing_summary']
    if len(summaries) != 1:
        raise RuntimeError('missing_unique_timing_summary')
    result = {'directory': str(directory), 'pdf': str(pdfs[0]), 'runner_exit': proc.returncode,
              'mock_requests': server.request_count-before, 'elapsed_seconds': time.monotonic()-started,
              'stats': json.loads(stats.read_text(encoding='utf-8')), 'timing': summaries[0]}
    (directory / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'sample': name, 'run': label, 'event': 'completed',
                      'seconds': round(result['elapsed_seconds'], 2)}), flush=True)
    return result


def timing_comparison(baseline, working):
    old, new = baseline['timing']['functions'], working['timing']['functions']
    return {key: {'baseline': old.get(key), 'working': new.get(key)}
            for key in sorted(old.keys() | new.keys()) if key.startswith('ztf_split_compose.')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, default=PYTHON)
    parser.add_argument('--baseline', default='39cd986')
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--samples', nargs='+', choices=['P05', 'chen2026', 'foba'],
                        default=['P05', 'chen2026', 'foba'])
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'capture_runner.py').write_text(CAPTURE_RUNNER, encoding='utf-8')
    baseline_runner = export_baseline(args.baseline)
    working_runner = ROOT / 'addon/content/python/ztf_babeldoc_runner.py'
    run = OUTPUT / ('runs-'+time.strftime('%Y%m%dT%H%M%S')+f'-{os.getpid()}')
    run.mkdir()
    from run_split_mock_e2e import isolated_environment
    environment = isolated_environment(OUTPUT / 'runtime')
    os.environ['TIKTOKEN_CACHE_DIR'] = environment['TIKTOKEN_CACHE_DIR']
    from mock_openai_server import MockOpenAIHandler, MockOpenAIServer
    server = MockOpenAIServer(('127.0.0.1', 0), MockOpenAIHandler, synthetic=True,
                              count_file=str(run / 'mock-requests.txt'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    samples = {'P05': ROOT / '.scratch/t082/P05.pdf'}
    samples.update({n: Path('G:/ProgramData/zotero-translate-files/ztest/samples/0.8.0') / (n+'.pdf')
                    for n in ('chen2026', 'foba')})
    report = {'baseline_commit': args.baseline, 'run_directory': str(run), 'status': 'FAIL',
              'evidence_kind': '真实 BabelDOC 0.6.4 + 本地合成译文；非真实翻译/非 Zotero 真机',
              'cases': []}
    try:
        endpoint = f'http://127.0.0.1:{server.server_port}/v1'
        for name in args.samples:
            case = {'sample': name, 'status': 'FAIL'}
            report['cases'].append(case)
            try:
                old = run_translation(args.python, name, samples[name], 'baseline', baseline_runner,
                                      endpoint, environment, server, run, args.timeout)
                repeat = run_translation(args.python, name, samples[name], 'baseline-repeat', baseline_runner,
                                         endpoint, environment, server, run, args.timeout)
                case['runs'] = {'baseline': old, 'baseline_repeat': repeat}
                case['determinism'] = compare_pdfs(old['pdf'], repeat['pdf'])
                case['determinism']['checks']['compose_stats'] = old['stats'] == repeat['stats']
                if old['stats'] != repeat['stats']:
                    case['determinism']['differences'].append({'page': None, 'type': 'compose_stats'})
                if not all(case['determinism']['checks'].values()):
                    case['determinism']['status'] = 'FAIL'
                    case['error'] = 'same_code_not_deterministic'
                    continue
                new = run_translation(args.python, name, samples[name], 'working', working_runner,
                                      endpoint, environment, server, run, args.timeout)
                case['equivalence'] = compare_pdfs(old['pdf'], new['pdf'])
                case['equivalence']['checks']['compose_stats'] = old['stats'] == new['stats']
                if old['stats'] != new['stats']:
                    case['equivalence']['differences'].append({'page': None, 'type': 'compose_stats'})
                case['timing_comparison'] = timing_comparison(old, new)
                case['runs'] = {'baseline': old, 'baseline_repeat': repeat, 'working': new}
                case['status'] = 'PASS' if all(case['equivalence']['checks'].values()) else 'FAIL'
                case['equivalence']['status'] = case['status']
            except Exception as error:
                case['error'] = type(error).__name__+': '+str(error)
            finally:
                (OUTPUT / 'equivalence.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            print(json.dumps({'sample': name, 'event': 'compared', 'status': case['status']}), flush=True)
        report['status'] = 'PASS' if report['cases'] and all(c['status']=='PASS' for c in report['cases']) else 'FAIL'
    finally:
        server.shutdown(); thread.join(timeout=10); server.server_close()
        report['mock_closed'] = not thread.is_alive()
        (OUTPUT / 'equivalence.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return 0 if report['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
