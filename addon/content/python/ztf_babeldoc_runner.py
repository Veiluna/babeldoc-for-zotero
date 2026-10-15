"""Run BabelDOC with the API key kept out of the operating-system command line."""

from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing
import os
import sys
import traceback
from typing import Any


def _redact(value: Any, api_key: str) -> str:
    text = str(value if value is not None else "")
    return text.replace(api_key, "[已隐藏]") if api_key else text


def _emit(prefix: str, payload: dict[str, Any], api_key: str) -> None:
    line = prefix + json.dumps(payload, ensure_ascii=False, default=str)
    print(_redact(line, api_key), flush=True)


def _configure_logging(api_key: str) -> None:
    class RedactingStreamHandler(logging.StreamHandler):
        def format(self, record: logging.LogRecord) -> str:
            return _redact(super().format(record), api_key)

    handler = RedactingStreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    # BabelDOC's cli() uses basicConfig with a RichHandler that writes to stdout.
    # Configure the root first; basicConfig then leaves this sanitized stderr handler in place.
    root_logger = logging.getLogger()
    root_logger.handlers[:] = [handler]
    root_logger.setLevel(logging.INFO)

    babeldoc_logger = logging.getLogger("babeldoc")
    babeldoc_logger.handlers.clear()
    babeldoc_logger.propagate = True


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    # Remove the key before BabelDOC starts multiprocessing children so they do not inherit it.
    api_key = os.environ.pop("ZTF_OPENAI_API_KEY", "")
    if not api_key:
        print("Missing ZTF_OPENAI_API_KEY environment variable.", file=sys.stderr, flush=True)
        return 2

    # The key is added to Python's in-memory argv only. The OS-level command line contains runner.py,
    # never this value; the plugin also asserts the key is absent from Subprocess.call arguments.
    sys.argv[1:1] = ["--openai-api-key", api_key]

    state = {"error": False, "result": False}

    def create_progress_handler(config: Any, show_log: bool = False):
        del config, show_log

        def handle_event(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            if event_type in ("progress_start", "progress_update", "progress_end"):
                _emit(
                    "ZTF_PROGRESS ",
                    {
                        "overall": event.get("overall_progress", 0),
                        "stage": event.get("stage", ""),
                        "current": event.get("stage_current", 0),
                        "total": event.get("stage_total", 0),
                    },
                    api_key,
                )
            elif event_type == "error":
                state["error"] = True
                _emit(
                    "ZTF_ERROR ",
                    {"error": _redact(event.get("error", "BabelDOC reported an error."), api_key)},
                    api_key,
                )
            elif event_type == "finish":
                result = event.get("translate_result") or {}
                mono_path = ""
                if isinstance(result, dict):
                    mono_path = result.get("no_watermark_mono_pdf_path") or result.get("mono_pdf_path") or ""
                else:
                    mono_path = getattr(result, "no_watermark_mono_pdf_path", "") or getattr(
                        result, "mono_pdf_path", ""
                    )
                mono_path = str(mono_path) if mono_path else ""
                if mono_path:
                    state["result"] = True
                _emit("ZTF_RESULT ", {"mono": mono_path}, api_key)

        return contextlib.nullcontext(), handle_event

    try:
        import babeldoc.main as babeldoc_main

        babeldoc_main.create_progress_handler = create_progress_handler
        _configure_logging(api_key)

        try:
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            # Importers or test harnesses may have selected spawn already.
            if multiprocessing.get_start_method() != "spawn":
                raise

        babeldoc_main.cli()
        exit_code = 0
    except SystemExit as error:
        if error.code is None:
            exit_code = 0
        elif isinstance(error.code, int):
            exit_code = error.code
        else:
            print(_redact(error.code, api_key), file=sys.stderr, flush=True)
            exit_code = 1
    except BaseException:
        # Format before writing, then redact the entire traceback so secrets in
        # exception messages, chained causes, or source excerpts cannot escape.
        formatted_traceback = traceback.format_exc()
        print(_redact(formatted_traceback, api_key), file=sys.stderr, flush=True)
        exit_code = 1

    # BabelDOC may log a translation error and still return success. Enforce a failed
    # process status unless a finish event supplied a monolingual result path.
    if state["error"] or not state["result"]:
        return exit_code or 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
