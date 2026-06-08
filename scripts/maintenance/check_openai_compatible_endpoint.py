#!/usr/bin/env python3
"""Check an OpenAI-compatible local model endpoint before Orchestrator smoke."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib import error, request


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8", errors="replace")
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{url} returned non-object JSON")
    return decoded


def _chat_text(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _responses_text(body: dict[str, Any]) -> str:
    output_text = body.get("output_text")
    if isinstance(output_text, str):
        return output_text
    parts: list[str] = []
    output = body.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if isinstance(content_item, dict) and isinstance(
                content_item.get("text"), str
            ):
                parts.append(content_item["text"])
    return "".join(parts)


def _check_chat(base_url: str, model: str, api_key: str, timeout_seconds: float) -> str:
    body = _post_json(
        f"{base_url}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Return only the word ok."},
                {"role": "user", "content": "Reply with ok."},
            ],
            "temperature": 0,
            "stream": False,
        },
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    text = _chat_text(body).strip()
    if not text:
        raise RuntimeError("/chat/completions returned no assistant content")
    return text[:200]


def _check_responses(
    base_url: str, model: str, api_key: str, timeout_seconds: float
) -> str:
    body = _post_json(
        f"{base_url}/responses",
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Reply with ok."}],
                }
            ],
        },
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    text = _responses_text(body).strip()
    if not text:
        raise RuntimeError("/responses returned no output text")
    return text[:200]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check llama.cpp or another OpenAI-compatible endpoint."
    )
    parser.add_argument(
        "--base-url",
        default=(
            _env("OPENAI_CHAT_COMPLETIONS_BASE_URL")
            or _env("OPENAI_BASE_URL")
            or "http://localhost:8001/v1"
        ),
        help="Base URL ending in /v1.",
    )
    parser.add_argument(
        "--model",
        default=(
            _env("OPENAI_CHAT_COMPLETIONS_MODEL") or _env("PLANNER_MODEL") or "local"
        ),
        help="Model id to send in the request.",
    )
    parser.add_argument(
        "--api-key",
        default=(
            _env("OPENAI_CHAT_COMPLETIONS_API_KEY")
            or _env("OPENAI_API_KEY")
            or "dummy"
        ),
        help="Bearer token. Use dummy for local endpoints that ignore auth.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--check-responses",
        action="store_true",
        help="Also check /responses for openai_responses_api compatibility.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = str(args.base_url).rstrip("/")
    summary: dict[str, Any] = {
        "base_url": base_url,
        "model": args.model,
        "chat_completions": {"ok": False},
        "responses": {"checked": bool(args.check_responses), "ok": None},
    }

    try:
        summary["chat_completions"] = {
            "ok": True,
            "sample": _check_chat(base_url, args.model, args.api_key, args.timeout),
        }
        if args.check_responses:
            summary["responses"] = {
                "checked": True,
                "ok": True,
                "sample": _check_responses(
                    base_url, args.model, args.api_key, args.timeout
                ),
            }
    except (error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
