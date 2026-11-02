"""串行运行正式 runner 和本地 mock，核对图表像素、重复词、体积及耗时。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path("C:/Users/A/AppData/Local/babeldoc-for-zotero/env/Scripts/python.exe")
SAMPLES = Path("G:/ProgramData/zotero-translate-files/ztest/samples/0.8.0")
OUTPUT = ROOT / "build/t081/c"
REGIONS = {
    "p05-5": (53.0, 83.0, 558.0, 483.0),
    "foba-10": (48.0, 94.0, 562.0, 252.0),
}


def _extract_page(source: Path, page_number: int, target: Path) -> None:
    with pymupdf.open(source) as document, pymupdf.open() as one_page:
        one_page.insert_pdf(document, from_page=page_number - 1, to_page=page_number - 1)
        one_page.save(target)


def _same_position_duplicates(source_page, output_page, clip) -> int:
    source_words = source_page.get_text("words", clip=pymupdf.Rect(*clip))
    output_words = output_page.get_text("words", clip=pymupdf.Rect(*clip))
    source_keys = [(round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1), w[4])
                   for w in source_words]
    output_keys = [(round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1), w[4])
                   for w in output_words]
    return sum(max(0, output_keys.count(key) - source_keys.count(key)) for key in set(output_keys))


def _pixel_equal(source_page, output_page, clip) -> bool:
    rect = pymupdf.Rect(*clip)
    return source_page.get_pixmap(dpi=150, clip=rect, alpha=False).samples == output_page.get_pixmap(
        dpi=150, clip=rect, alpha=False
    ).samples


def run_case(name: str, split: bool, server, run_root: Path) -> dict:
    sample_name = "P05.pdf" if name.startswith("p05") else "foba.pdf"
    page_number = 5 if name.startswith("p05") else 10
    case = run_root / f"{name}-split-{int(split)}"
    out, work = case / "out", case / "work"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    source = case / "input.pdf"
    _extract_page(SAMPLES / sample_name, page_number, source)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    cache = case / "profile" / ".cache" / "babeldoc"
    original_cache = Path("C:/Users/A/.cache/babeldoc")
    for cache_name in ("fonts", "models", "cmap", "tiktoken"):
        shutil.copytree(original_cache / cache_name, cache / cache_name, dirs_exist_ok=True)
    environment = {
        "SystemRoot": "C:/Windows",
        "WINDIR": "C:/Windows",
        "PATH": "C:/Windows/System32;C:/Program Files/nodejs",
        "USERPROFILE": str(case / "profile"),
        "TMP": str(case / "tmp"),
        "TEMP": str(case / "tmp"),
        "TIKTOKEN_CACHE_DIR": str(cache / "tiktoken"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "ZTF_OPENAI_API_KEY": "mock-test-key",
        "ZTF_SPLIT_DUAL": "1" if split else "0",
        "ZTF_MAX_OUTPUT_TOKENS": "8192",
        "ZTF_MAX_REQUESTS": "200",
        "ZTF_REPAIR_FAILED": "1",
        "ZTF_TRANSLATE_REFERENCES": "0",
        "ZTF_TOKEN_PARAM": "max_tokens",
        "ZTF_JOB_LOG_PATH": str(case / "runner.jsonl"),
    }
    case.mkdir(parents=True, exist_ok=True)
    (case / "profile").mkdir(exist_ok=True)
    (case / "tmp").mkdir(exist_ok=True)
    command = [str(PYTHON), "-B", "-X", "utf8", str(ROOT / "addon/content/python/ztf_babeldoc_runner.py"),
               "--files", str(source), "--output", str(out), "--working-dir", str(work), "--openai",
               "--openai-model", "mock-model", "--openai-base-url", endpoint, "--lang-in", "en",
               "--lang-out", "zh", "--no-auto-extract-glossary", "--disable-same-text-fallback",
               "--ignore-cache", "--watermark-output-mode", "no_watermark", "--report-interval", "1",
               "--no-dual"]
    started = time.monotonic()
    process = subprocess.run(command, cwd=case, env=environment, capture_output=True,
                             text=True, encoding="utf-8", timeout=900)
    elapsed = round(time.monotonic() - started, 2)
    monos = sorted(out.glob("*.mono.pdf"))
    duals = sorted(out.glob("*.dual.pdf"))
    result = {"name": name, "split": split, "elapsed_seconds": elapsed,
              "returncode": process.returncode, "mono_bytes": monos[0].stat().st_size if monos else 0,
              "mono": str(monos[0]) if monos else None, "dual": str(duals[0]) if duals else None}
    if process.returncode != 0 or len(monos) != 1:
        result["error"] = (process.stderr or process.stdout)[-2000:]
        return result
    clip = REGIONS[name]
    with pymupdf.open(source) as original, pymupdf.open(monos[0]) as mono:
        result["mono_figure_pixels_equal"] = _pixel_equal(original[0], mono[0], clip)
        result["mono_duplicate_words"] = _same_position_duplicates(original[0], mono[0], clip)
    if split:
        if len(duals) != 1:
            result["error"] = "missing dual output"
            return result
        with pymupdf.open(source) as original, pymupdf.open(duals[0]) as dual:
            result["dual_figure_pixels_equal"] = all(_pixel_equal(original[0], page, clip)
                                                      for page in dual)
            result["dual_duplicate_words"] = sum(_same_position_duplicates(original[0], page, clip)
                                                   for page in dual)
    result["status"] = "PASS" if result.get("mono_figure_pixels_equal") and not result.get("mono_duplicate_words") \
        and (not split or (result.get("dual_figure_pixels_equal") and not result.get("dual_duplicate_words"))) else "FAIL"
    return result


def main() -> int:
    sys.path.insert(0, str(ROOT / "tests"))
    from mock_openai_server import MockOpenAIHandler, MockOpenAIServer

    OUTPUT.mkdir(parents=True, exist_ok=True)
    run_root = OUTPUT / time.strftime("%Y%m%dT%H%M%S")
    run_root.mkdir(parents=True, exist_ok=True)
    server = MockOpenAIServer(("127.0.0.1", 0), MockOpenAIHandler, synthetic=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        for name in ("p05-5", "foba-10"):
            for split in (True, False):
                results.append(run_case(name, split, server, run_root))
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
    summary = {"results": results, "status": "PASS" if all(r.get("status") == "PASS" for r in results) else "FAIL"}
    (run_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT / "latest-c.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
