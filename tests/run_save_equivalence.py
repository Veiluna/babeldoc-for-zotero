#!/usr/bin/env python3
"""真实 BabelDOC + 本地合成译文的保存等价性检查；非 Zotero 真机验收。

默认依次运行每个样本的 baseline、baseline-repeat、working；重复基线不一致
时不认可该样本的优化等价性。同时检查 variants 与 mono 的文本和像素。
所有实验产物位于 .scratch/t082/t3，基线代码仅用 git archive 导出。
需指定环境的 Python 运行。
"""
from __future__ import annotations

import argparse
from datetime import datetime
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
OUTPUT = ROOT / '.scratch/t082/t3'
BASE = OUTPUT / 'base'
PYTHON = Path('G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe')

# 只在实验子进程捕获返回值；不修改导出的基线或生产 runner。
CAPTURE_RUNNER = '''import functools
import json
from pathlib import Path
import runpy
import sys
def stable_link_input(value):
    # runner 的段落 ID 来自 id(paragraph)，跨进程不可比较。只在实验入口
    # 给 restore_links 相同的身份输入；不改生产代码，不在比较时忽略 ID。
    if value is None:
        return None
    paragraphs = value.get("paragraphs", []) if isinstance(value, dict) else value
    aliases = {}
    result = []
    for paragraph in paragraphs:
        item = dict(paragraph)
        identity = item.get("id")
        if isinstance(identity, int):
            item["id"] = aliases.setdefault(identity, len(aliases) + 1)
        result.append(item)
    return dict(value, paragraphs=result) if isinstance(value, dict) else result

def capture():
    runner, stats_path, *arguments = sys.argv[1:]
    sys.path.insert(0, str(Path(runner).parent))
    sys.argv = [runner, *arguments]
    namespace = runpy.run_path(runner, run_name="equivalence_capture")
    main = namespace["main"]
    original_load = main.__globals__["_load_helper"]
    def load(name):
        helper = original_load(name)
        if name == "ztf_links" and not getattr(helper, "_equivalence_link_inputs", False):
            original_restore = helper.restore_links
            @functools.wraps(original_restore)
            def restore(source, output, paragraph_map=None, *args, **kwargs):
                return original_restore(source, output, stable_link_input(paragraph_map), *args, **kwargs)
            helper.restore_links = restore
            helper._equivalence_link_inputs = True
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


def pdf_snapshot(path, *, include_layout=True):
    import pymupdf as fitz
    with fitz.open(path) as doc:
        layout = None
        if include_layout:
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


def compare_pdfs(left, right, *, include_layout=True):
    a, b = (pdf_snapshot(path, include_layout=include_layout) for path in (left, right))
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


def compare_runs(left, right):
    result = compare_pdfs(left['pdf'], right['pdf'])
    result['artifacts'] = {}
    for role in ('mono', 'variants'):
        comparison = compare_pdfs(left[role], right[role], include_layout=False)
        result['artifacts'][role] = comparison
        result['checks'][role] = comparison['status'] == 'PASS'
        result['differences'].extend(dict(item, artifact=role) for item in comparison['differences'])
    result['checks']['compose_stats'] = left['stats'] == right['stats']
    result['checks']['links_mapping'] = compare_link_mappings(
        str(left['mono'])+'.links.json', str(right['mono'])+'.links.json')
    if not result['checks']['links_mapping']:
        result['differences'].append({'page': None, 'type': 'links_mapping'})
    if not result['checks']['compose_stats']:
        result['differences'].append({'page': None, 'type': 'compose_stats'})
    result['status'] = 'PASS' if all(result['checks'].values()) else 'FAIL'
    return result


def compare_link_mappings(left, right):
    """逐条精确比较映射；只忽略顶层 source/output 的运行目录差异。

    不使用 PDF 的 normalize：映射中的 id、xref、列表顺序均必须相同。
    当前 mapping schema 没有时间戳字段，因此不忽略任何时间戳。
    """
    def read(path):
        if not Path(path).is_file():
            return None
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return {key: item for key, item in value.items() if key not in ('source', 'output')}
    left_value, right_value = read(left), read(right)
    return left_value is not None and right_value is not None and left_value == right_value


def export_baseline(commit):
    archive = OUTPUT / 'baseline.tar'
    with archive.open('wb') as handle:
        subprocess.run(['git', 'archive', commit, 'addon/content/python'], cwd=ROOT,
                       stdout=handle, check=True)
    BASE.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as handle:
        handle.extractall(BASE, filter='data')
    return BASE / 'addon/content/python/ztf_babeldoc_runner.py'


def run_translation(python, name, source, label, runner, endpoint, environment, server, run, timeout, mock_delay=0):
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
    server.delay_next_request_seconds = mock_delay
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
    monos, variants = list(out.glob('*.mono.pdf')), list(out.glob('*.variants.pdf'))
    if (proc.returncode or len(pdfs) != 1 or len(monos) != 1 or len(variants) != 1
            or not stats.is_file() or server.request_count <= before):
        raise RuntimeError('runner_missing_successful_output')
    summaries = [e for e in events if e.get('event') == 'timing_summary']
    if len(summaries) != 1:
        raise RuntimeError('missing_unique_timing_summary')
    writer = next(e for e in events if e.get('event')=='split_variants')
    saved = next(e for e in events if e.get('event')=='split_dual' and 'output_pages' in e)
    write_time = summaries[0]['functions']['ztf_split_variants.write[save]']['seconds']
    saving_wall = (datetime.fromisoformat(saved['timestamp']) - datetime.fromisoformat(writer['timestamp'])).total_seconds() + write_time
    result = {'saving_wall_seconds':saving_wall, 'directory': str(directory), 'pdf': str(pdfs[0]),
              'mono': str(monos[0]), 'variants': str(variants[0]), 'runner_exit': proc.returncode,
              'mock_requests': server.request_count-before, 'elapsed_seconds': time.monotonic()-started,
              'stats': json.loads(stats.read_text(encoding='utf-8')), 'timing': summaries[0]}
    (directory / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'sample': name, 'run': label, 'event': 'completed',
                      'seconds': round(result['elapsed_seconds'], 2)}), flush=True)
    return result


def timing_comparison(baseline, working):
    old, new = baseline['timing']['functions'], working['timing']['functions']
    return {key: {'baseline': old.get(key), 'working': new.get(key)}
            for key in sorted(old.keys() | new.keys())
            if key.startswith(('ztf_split_compose.', 'ztf_split_variants.', 'ztf_links.'))}


def main():
    global OUTPUT, BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, default=PYTHON)
    parser.add_argument('--baseline', default='6476397')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--mock-delay', type=float, default=0, help='每次翻译首次模拟请求延迟秒数')
    parser.add_argument('--include-disabled', action='store_true', help='每样本额外运行关闭预计算的工作树')
    parser.add_argument('--reuse-baseline-run', type=Path, help='复用该 run 目录下已完成的 baseline 结果')
    parser.add_argument('--skip-baseline-repeat', action='store_true', help='复用既有确定性证据，省略重复基线')
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--samples', nargs='+', choices=['P05', 'chen2026', 'foba'],
                        default=['P05', 'chen2026', 'foba'])
    args = parser.parse_args()
    OUTPUT = args.output.resolve()
    BASE = OUTPUT / "base"
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
    report = {'baseline_commit': args.baseline, 'mock_delay_seconds':args.mock_delay,
              'baseline_repeat_skipped':args.skip_baseline_repeat, 'run_directory': str(run), 'status': 'FAIL',
              'evidence_kind': '真实 BabelDOC 0.6.4 + 本地合成译文；非真实翻译/非 Zotero 真机',
              'link_input_ids': '实验入口按首次出现顺序重编号，保持身份等价关系；比较时不忽略 id',
              'cases': []}
    try:
        endpoint = f'http://127.0.0.1:{server.server_port}/v1'
        for name in args.samples:
            case = {'sample': name, 'status': 'FAIL'}
            report['cases'].append(case)
            try:
                old = (json.loads((args.reuse_baseline_run/name/'baseline/result.json').read_text(encoding='utf-8'))
                    if args.reuse_baseline_run else run_translation(
                        args.python, name, samples[name], 'baseline', baseline_runner,
                        endpoint, environment, server, run, args.timeout, args.mock_delay))
                repeat = old if args.skip_baseline_repeat else run_translation(
                    args.python, name, samples[name], 'baseline-repeat', baseline_runner,
                    endpoint, environment, server, run, args.timeout, args.mock_delay)
                case['runs'] = {'baseline': old, 'baseline_repeat': repeat}
                case['determinism'] = ({'status':'NOT_RERUN', 'checks':{}} if args.skip_baseline_repeat
                                       else compare_runs(old, repeat))
                if not all(case['determinism']['checks'].values()):
                    case['determinism']['status'] = 'FAIL'
                    case['error'] = 'same_code_not_deterministic'
                    continue
                new = run_translation(args.python, name, samples[name], 'working', working_runner,
                                      endpoint, environment, server, run, args.timeout, args.mock_delay)
                case['equivalence'] = compare_runs(old, new)
                case['timing_comparison'] = timing_comparison(old, new)
                case['runs'] = {'baseline': old, 'baseline_repeat': repeat, 'working': new}
                case['status'] = 'PASS' if all(case['equivalence']['checks'].values()) else 'FAIL'
                case['equivalence']['status'] = case['status']
                if args.include_disabled:
                    disabled = run_translation(args.python, name, samples[name], 'working-disabled', working_runner,
                        endpoint, dict(environment, ZTF_SPLIT_PRECOMPUTE='0'), server, run, args.timeout, args.mock_delay)
                    case['runs']['working_disabled'] = disabled
                    case['disabled_equivalence'] = compare_runs(old, disabled)
                    if not all(case['disabled_equivalence']['checks'].values()):
                        case['status'] = 'FAIL'

            except Exception as error:
                case['status'] = 'FAIL'
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
