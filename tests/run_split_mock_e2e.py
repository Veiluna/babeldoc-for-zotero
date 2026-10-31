#!/usr/bin/env python3
"""Chen/FoBa 的真实 BabelDOC + 本地假译文端到端；全部产物位于 build/t5d。

不读取 Zotero 配置，端点固定为 tests/mock_openai_server.py 的环回随机端口。
运行：<babeldoc-env>/python.exe -B -X utf8 tests/run_split_mock_e2e.py
质量失败保留非零退出码；不会把 mock 结果当作真实翻译或真机验收。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build/t5d/mock"
RUNNER = ROOT / "addon/content/python/ztf_babeldoc_runner.py"
DEFAULT_PYTHON = Path("G:/ProgramData/zotero-translate-files/babeldoc-env/python.exe")


def log_event(event, reason, count=1, *, path=None, **fields):
    """只输出受控字段的 JSONL；调用方不得传入正文或原始异常消息。"""
    record = json.dumps({"event": event, "reason": reason, "count": count, **fields}, ensure_ascii=False)
    if path is not None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")
    print(record, flush=True)


def find_samples():
    directory = Path("G:/ProgramData/zotero-translate-files/ztest/samples/0.8.0")
    return [(name, directory / (name + ".pdf")) for name in ("chen2026", "foba")]


def isolated_environment(directory):
    """复制已安装资源，避免 BabelDOC 缓存、字节码或临时文件写到许可范围外。"""
    original_cache = Path("C:/Users/A/.cache/babeldoc")
    profile = directory / "profile"
    cache = profile / ".cache/babeldoc"
    for name in ("fonts", "models", "cmap", "tiktoken"):
        if not (original_cache / name).is_dir():
            raise FileNotFoundError(f"缺少本机离线 BabelDOC 资源：{original_cache / name}")
        shutil.copytree(original_cache / name, cache / name, dirs_exist_ok=True)
    temp = directory / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    # 不查询、继承或持久化宿主环境值；子进程只收到这里构造的本地 mock 配置。
    environment = {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows",
                   "PATH": "C:/Windows/System32;C:/Program Files/nodejs"}
    environment.update({"USERPROFILE": str(profile), "TMP": str(temp), "TEMP": str(temp),
                        "TIKTOKEN_CACHE_DIR": str(cache / "tiktoken"),
                        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                        "NO_COLOR": "1", "NO_PROXY": "127.0.0.1,localhost",
                        "ZTF_OPENAI_API_KEY": "mock-test-key", "ZTF_SPLIT_DUAL": "1",
                        "ZTF_MAX_OUTPUT_TOKENS": "8192", "ZTF_MAX_REQUESTS": "200",
                        "ZTF_REPAIR_FAILED": "1", "ZTF_TRANSLATE_REFERENCES": "0",
                        "ZTF_TOKEN_PARAM": "max_tokens"})
    return environment


def run_case(python, name, source, endpoint, environment, server, timeout, run_dir):
    directory = run_dir / name
    out, work = directory / "out", directory / "work"
    out.mkdir(parents=True)
    work.mkdir()
    input_pdf = directory / f"{name}.pdf"
    shutil.copyfile(source, input_pdf)
    environment = dict(environment, ZTF_JOB_LOG_PATH=str(directory / "runner.jsonl"))
    command = [str(python), "-B", "-X", "utf8", str(RUNNER), "--files", str(input_pdf),
               "--output", str(out), "--working-dir", str(work), "--openai", "--openai-model", "mock-model",
               "--openai-base-url", endpoint, "--lang-in", "en", "--lang-out", "zh", "--no-dual",
               "--no-auto-extract-glossary", "--disable-same-text-fallback", "--ignore-cache",
               "--watermark-output-mode", "no_watermark", "--report-interval", "1"]
    before = server.request_count
    started = time.monotonic()
    timed_out = False
    event_path = directory / "runner-events.jsonl"
    log_event("case_started", "local_mock", path=event_path, sample=name)
    # runner 自身的 runner.jsonl 保留诊断；未经审计的 stdout/stderr 不落盘。
    proc = subprocess.Popen(command, cwd=directory, env=environment,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        while proc.poll() is None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                log_event("case_progress", "runner_pending", server.request_count - before,
                          path=event_path, sample=name, elapsed_seconds=round(time.monotonic() - started, 1))
                if time.monotonic() - started > timeout:
                    timed_out = True
                    log_event("case_timeout", "deadline_exceeded", path=event_path, sample=name)
                    proc.terminate()
                    break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    events = []
    log = directory / "runner.jsonl"
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    result = {"sample": name, "source": str(source), "runner_exit": proc.returncode, "timed_out": timed_out,
              "elapsed_seconds": round(time.monotonic() - started, 1), "mock_requests": server.request_count - before,
              "composition_events": [e for e in events if e.get("event", "").startswith(("split_", "links_"))],
              "directory": str(directory), "status": "FAIL"}
    monos, duals = sorted(out.glob("*.mono.pdf")), sorted(out.glob("*.dual.pdf"))
    result["pdfs"] = [str(p) for p in sorted(out.glob("*.pdf"))]
    if len(monos) == 1 and len(duals) == 1:
        quality = subprocess.run([str(python), "-B", "-X", "utf8", str(ROOT / "tests/split_quality_check.py"),
                                  "--source", str(source), "--dual", str(duals[0]), "--mono", str(monos[0]), "--json"],
                                 env=environment, cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=180)
        if quality.stderr:
            log_event("quality_diagnostics", "stderr_suppressed", len(quality.stderr.splitlines()),
                      path=directory / "quality-events.jsonl", sample=name)
        report = json.loads(quality.stdout)
        (directory / "quality.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # 驱动脚本自身仅需标准库；PyMuPDF 留在指定的 BabelDOC Python 中。
        displayed = []
        for check in report["checks"]:
            displayed.append(f"{check['status']} {check['id']} — {check['name']}")
            evidence = json.dumps(check["evidence"], ensure_ascii=False)
            displayed.append("  " + (evidence[:2200] + " …（完整证据见 quality.json）" if len(evidence) > 2200 else evidence))
        displayed.append("总结果：" + report["status"])
        text = "\n".join(displayed)
        (directory / "quality.txt").write_text(text, encoding="utf-8")
        result["checks"] = [{"id": c["id"], "status": c["status"]} for c in report["checks"]]
        result["quality_exit"] = quality.returncode
        log_event("quality_completed", "checks_evaluated", len(report["checks"]),
                  path=directory / "quality-events.jsonl", sample=name, status=report["status"],
                  returncode=quality.returncode)
        if proc.returncode == 0 and not timed_out and quality.returncode == 0 and result["mock_requests"] > 0:
            result["status"] = "PASS"
    else:
        result["error"] = "未得到唯一 mono 和 dual；不拿旧文件或独立重合成冒充 runner 成功"
        log_event("quality_skipped", "missing_unique_outputs", path=event_path, sample=name,
                  mono_count=len(monos), dual_count=len(duals))
    log_event("case_completed", "runner_result", result["mock_requests"], path=event_path,
              sample=name, status=result["status"], returncode=proc.returncode)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Chen/FoBa 本地 mock 分栏双语端到端")
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON, help="BabelDOC 0.6.4 Python")
    parser.add_argument("--timeout", type=int, default=1800, help="单份样本超时秒数")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    run_dir = OUTPUT / (time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}")
    run_dir.mkdir()
    summary = {"evidence_kind": "真实 BabelDOC + tests/mock_openai_server.py 合成中文；非真实翻译/非 Zotero 真机验收",
               "directory": str(run_dir), "cases": [], "status": "FAIL", "mock_closed": False}
    event_path = run_dir / "events.jsonl"
    server = thread = None
    try:
        if not args.python.is_file():
            raise FileNotFoundError(f"BabelDOC Python 不存在：{args.python}")
        environment = isolated_environment(OUTPUT / "runtime")
        # 在导入 mock 的 tiktoken 之前指定复制后的缓存。
        os.environ["TIKTOKEN_CACHE_DIR"] = environment["TIKTOKEN_CACHE_DIR"]
        from mock_openai_server import MockOpenAIHandler, MockOpenAIServer
        server = MockOpenAIServer(("127.0.0.1", 0), MockOpenAIHandler,
                                  count_file=str(run_dir / "mock-requests.txt"), synthetic=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        samples = find_samples()
        if len(samples) == 1:
            summary["skipped_samples"] = [{"sample": "P01", "reason": "sample_not_found"}]
            log_event("sample_skipped", "sample_not_found", path=event_path, sample="P01")
        for name, source in samples:
            try:
                summary["cases"].append(run_case(args.python, name, source, endpoint, environment, server, args.timeout, run_dir))
            except Exception as error:
                summary["cases"].append({"sample": name, "status": "FAIL", "error": type(error).__name__})
                log_event("case_failed", type(error).__name__, path=event_path, sample=name)
        summary["status"] = "PASS" if summary["cases"] and all(c["status"] == "PASS" for c in summary["cases"]) else "FAIL"
    except Exception as error:
        summary["error"] = type(error).__name__
        log_event("run_failed", type(error).__name__, path=event_path)
    finally:
        if server is not None:
            if thread is not None:
                server.shutdown()
                thread.join(timeout=10)
            server.server_close()
            summary["mock_closed"] = thread is None or not thread.is_alive()
            summary["mock_requests"] = server.request_count
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT / "latest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log_event("run_completed", "mock_result", len(summary["cases"]), path=event_path,
                  status=summary["status"], mock_closed=summary["mock_closed"])
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
