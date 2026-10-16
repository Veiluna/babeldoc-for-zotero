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

try:
    import tiktoken

    _TOKENIZER = tiktoken.encoding_for_model("gpt-4o")
except (ImportError, KeyError):
    _TOKENIZER = None


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
    ) -> None:
        super().__init__(address, handler)
        self.response_status = status
        self.count_file = Path(count_file) if count_file else None
        self.request_count = 0

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

        outputs = [
            {
                "id": item.get("id", index),
                "output": "译：" + str(item.get("input", "")),
            }
            for index, item in enumerate(items)
        ]
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
    args = parser.parse_args()

    server = MockOpenAIServer(
        (args.host, args.port),
        MockOpenAIHandler,
        status=args.status,
        count_file=args.count_file,
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
