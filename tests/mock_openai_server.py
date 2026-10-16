"""Small stdlib-only OpenAI-compatible server for local runner checks.

Run with: python tests/mock_openai_server.py --port 8765 [--status 429]
POST /v1/chat/completions increments the request count. Use --count-file to
persist that count for an end-to-end BabelDOC run.
"""

from __future__ import annotations

import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import re
from typing import Any
import zlib

try:
    import tiktoken

    _TOKENIZER = tiktoken.encoding_for_model("gpt-4o")
except (ImportError, KeyError):
    _TOKENIZER = None


_MOCK_PLACEHOLDER_TOKEN_RE = re.compile(
    r"<\s*style\b(?=[^>]*\bid\s*=)[^>]*>|<\s*/\s*style\s*>|"
    r"\{\s*v\s*\d+\s*\}|%[sdif]|\[\[[^\]\r\n]+\]\]|%%[^%\r\n]+%%",
    re.I,
)
# Common characters are single tokens for the gpt-4o tokenizer, so a synthetic
# translation's token count tracks the source like a real one (rare characters
# split into several tokens and tripped BabelDOC's 0.3–3x length-ratio check).
_MOCK_CHINESE_CYCLES = (
    "的一是在不了有和人这",
    "中大为上个国我以要他",
    "时来用们生到作地于出",
    "就分对成会可主发年动",
)


def _fragment_part(item: dict) -> str | None:
    """Which half of a split sentence an item is ("first"/"second"), if any.

    continues_in / continued_from hold the sibling's item id, which may be 0,
    so presence (not truthiness) decides when fragment_part is absent.
    """
    part = item.get("fragment_part")
    if part in ("first", "second"):
        return part
    if "continues_in" in item:
        return "first"
    if "continued_from" in item:
        return "second"
    return None


def _synthetic_translation(source: str, seed: int = 0) -> str:
    markers = _MOCK_PLACEHOLDER_TOKEN_RE.findall(source)
    plain = _MOCK_PLACEHOLDER_TOKEN_RE.sub("", source)
    source_tokens = _encode_tokens(plain)
    # About 1.2 Chinese tokens per English token, like real en→zh output.
    count = max(2, int((len(source_tokens) if source_tokens is not None else len(plain) / 4) * 1.2))
    # Each item gets its own character sequence (seeded by its text and seed),
    # so tests can find a specific paragraph's translation in the output PDF.
    pool = "".join(_MOCK_CHINESE_CYCLES)
    state = zlib.crc32(f"{seed}:{plain}".encode("utf-8"))
    characters = []
    for _ in range(count):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        characters.append(pool[state % len(pool)])
    return "".join(characters) + "".join(markers)


def _encode_tokens(text: str) -> list[int] | None:
    if _TOKENIZER is None:
        return None
    return _TOKENIZER.encode(text, disallowed_special=())


def _token_count(text: str) -> int:
    tokens = _encode_tokens(text)
    if tokens is not None:
        return len(tokens)
    # Modest fallback for a regular Python without tiktoken: English averages
    # several characters per token, while CJK characters are close to one each.
    cjk_count = sum("\u3400" <= character <= "\u9fff" for character in text)
    non_cjk_count = len(text) - cjk_count
    return cjk_count + math.ceil(non_cjk_count / 4)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    tokens = _encode_tokens(text)
    if tokens is not None:
        return _TOKENIZER.decode(tokens[:max_tokens])
    # Find the longest prefix using exactly the same CJK/ASCII estimate as
    # _token_count(). A fixed character budget can exceed the limit for CJK.
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _token_count(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _prompt_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages") or []
    content = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict)
    )
    # Only accept the prompt template's standalone heading, never the same
    # phrase when it occurs in a paragraph encoded inside the input JSON.
    headings = list(re.finditer(r"(?m)^## Here is the input:[ \t]*$", content))
    if headings:
        array_start = content.find("[", headings[-1].end())
        if array_start >= 0:
            try:
                items = json.loads(content[array_start:])
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass
    raise ValueError("Could not find the input JSON array in the prompt")


class MockOpenAIServer(HTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        status: int = 200,
        count_file: str | None = None,
        bad_fragments: bool = False,
        bad_fragment_repairs: bool = False,
        long_short_labels: bool = False,
    ) -> None:
        super().__init__(address, handler)
        self.response_status = status
        self.count_file = Path(count_file) if count_file else None
        self.request_count = 0
        self.bad_fragments = bad_fragments
        self.bad_fragment_repairs = bad_fragment_repairs
        # Translate inputs of <= 4 tokens at exactly 3x their token count, like
        # "Recall" -> "召回率": valid for the runner, rejected by BabelDOC's own
        # 0.3 < ratio < 3 check, so it exercises the runner's override path.
        self.long_short_labels = long_short_labels
        self.long_short_label_count = 0
        self.bad_fragment_groups: set[str] = set()
        self.bad_fragment_group: str | None = None
        self.bad_fragment_sources: dict[str, dict[str, str]] = {}

    def count_request(self) -> None:
        self.request_count += 1
        if self.count_file:
            self.count_file.parent.mkdir(parents=True, exist_ok=True)
            self.count_file.write_text(str(self.request_count), encoding="utf-8")


class MockOpenAIHandler(BaseHTTPRequestHandler):
    server: MockOpenAIServer

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/__stats":
            self._send_json(404, {"error": {"message": "Not found"}})
            return
        self._send_json(200, {"requests": self.server.request_count})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "Not found"}})
            return

        self.server.count_request()
        if self.server.response_status == 429:
            self._send_json(
                429,
                {
                    "error": {
                        "message": "Too Many Requests",
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                },
            )
            return

        try:
            request_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(request_length))
            items = _prompt_items(payload)
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": {"message": str(error)}})
            return

        is_repair = any(item.get("repair_reason") for item in items)
        grouped_source: dict[str, str] = {}
        if self.server.bad_fragments:
            for item in items:
                group = item.get("fragment_id")
                if group:
                    group_id = str(group)
                    source_text = str(item.get("input", ""))
                    roles = self.server.bad_fragment_sources.setdefault(group_id, {})
                    if _fragment_part(item) == "first":
                        roles["first"] = source_text
                    elif _fragment_part(item) == "second":
                        roles["second"] = source_text
                    grouped_source[group_id] = roles.get("first", "") + roles.get(
                        "second", ""
                    )
                    if self.server.bad_fragment_group is None and _fragment_part(item) == "first":
                        self.server.bad_fragment_group = group_id
                        self.server.bad_fragment_groups.add(group_id)

        outputs = []
        for index, item in enumerate(items):
            source_text = str(item.get("input", ""))
            fragment_group = str(item.get("fragment_id", ""))
            if (
                is_repair
                and self.server.bad_fragment_repairs
                and fragment_group == self.server.bad_fragment_group
                and _fragment_part(item) == "first"
            ):
                # Repeat the merged-response bug during repair. The sibling is
                # empty below, so neither output may be applied to its paragraph.
                full_source = grouped_source.get(fragment_group, source_text)
                translated = _synthetic_translation(full_source, index)
            elif (
                is_repair
                and self.server.bad_fragment_repairs
                and fragment_group == self.server.bad_fragment_group
                and _fragment_part(item) == "second"
            ):
                translated = ""
            elif is_repair:
                translated = _synthetic_translation(source_text, index)
            elif (
                self.server.bad_fragments
                and fragment_group == self.server.bad_fragment_group
                and _fragment_part(item) == "first"
            ):
                # Deliberately merge both fragment translations into the first item.
                full_source = grouped_source.get(fragment_group, source_text)
                translated = _synthetic_translation(full_source, index)
            elif (
                self.server.bad_fragments
                and fragment_group == self.server.bad_fragment_group
                and _fragment_part(item) == "second"
            ):
                # Deliberately omit the continuation; the runner should repair it in one packed pass.
                translated = ""
            elif self.server.bad_fragments:
                translated = _synthetic_translation(source_text, index)
            else:
                translated = "\u8bd1\uff1a" + source_text
            if (
                self.server.long_short_labels
                and not is_repair
                and translated
                and not _MOCK_PLACEHOLDER_TOKEN_RE.search(source_text)
            ):
                source_tokens = _encode_tokens(source_text)
                if source_tokens is not None and 1 <= len(source_tokens) <= 4:
                    base = _synthetic_translation(source_text, index + 7)
                    translated = base[:1] * (3 * len(source_tokens))
                    self.server.long_short_label_count += 1
            outputs.append({"id": item.get("id", index), "output": translated})
        content = json.dumps(outputs, ensure_ascii=False)
        max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens") or 2048
        try:
            max_tokens = max(1, int(max_tokens))
        except (TypeError, ValueError):
            max_tokens = 2048

        # Match BabelDOC's gpt-4o tokenizer when tiktoken is installed in
        # its environment. Estimate the complete JSON completion, including its
        # array syntax, so mock truncation follows the API's output-token budget.
        estimated_output_tokens = _token_count(content)
        finish_reason = "stop"
        if estimated_output_tokens > max_tokens:
            content = _truncate_to_tokens(content, max_tokens)
            finish_reason = "length"

        self._send_json(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 0,
                "model": payload.get("model", "mock-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": min(estimated_output_tokens, max_tokens),
                    "total_tokens": 1 + min(estimated_output_tokens, max_tokens),
                },
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible mock server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--status", type=int, choices=(200, 429), default=200)
    parser.add_argument("--count-file")
    parser.add_argument(
        "--bad-fragments",
        action="store_true",
        help="return a merged translation in the first fragment and an empty continuation",
    )
    parser.add_argument(
        "--long-short-labels",
        action="store_true",
        help="translate inputs of <= 4 tokens at exactly 3x their token count (BabelDOC rejects these)",
    )
    args = parser.parse_args()

    server = MockOpenAIServer(
        (args.host, args.port),
        MockOpenAIHandler,
        status=args.status,
        count_file=args.count_file,
        bad_fragments=args.bad_fragments,
        long_short_labels=args.long_short_labels,
    )
    print(f"Mock OpenAI server listening at http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
