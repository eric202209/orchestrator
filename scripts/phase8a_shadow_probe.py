#!/usr/bin/env python3
"""Offline Phase 8A direct-vs-OpenClaw repair prompt shadow probe."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://localhost:8000/v1"
DEFAULT_MODEL = "qwen-local"
DEFAULT_GATEWAY_NOTE = (
    "Expected local target is vLLM OpenAI-compatible chat completions. "
    "Known ai-gateway setup serves qwen-local on port 8000 with "
    "--max-num-seqs 3, so slow direct TTFT can indicate vLLM slot saturation."
)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_nested_prompt(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in (
            "repair_prompt",
            "prompt",
            "input_prompt",
            "repair_input_prompt",
            "captured_prompt",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and len(candidate.strip()) >= 50:
                return candidate
        for child in value.values():
            found = _find_nested_prompt(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_nested_prompt(child)
            if found:
                return found
    return None


def load_prompt(*, prompt_file: Path | None, bundle_dir: Path | None) -> tuple[str, str]:
    if prompt_file:
        return _read_text(prompt_file), str(prompt_file)

    if not bundle_dir:
        raise SystemExit("Provide --prompt-file or --bundle-dir.")
    if not bundle_dir.is_dir():
        raise SystemExit(f"Bundle path is not a directory: {bundle_dir}")

    preferred_names = {
        "repair_prompt.txt",
        "prompt.txt",
        "input_prompt.txt",
        "repair_prompt.json",
        "prompt.json",
        "input_prompt.json",
    }
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.name not in preferred_names:
            continue
        if path.suffix == ".json":
            try:
                found = _find_nested_prompt(json.loads(_read_text(path)))
            except json.JSONDecodeError:
                found = None
            if found:
                return found, str(path)
        else:
            text = _read_text(path)
            if text.strip():
                return text, str(path)

    for path in sorted(bundle_dir.rglob("*.json")):
        try:
            found = _find_nested_prompt(json.loads(_read_text(path)))
        except json.JSONDecodeError:
            continue
        if found:
            return found, str(path)

    raise SystemExit(
        "No full prompt artifact found in bundle. Current TE118-style bundles "
        "record prompt sizes and diagnostics, not repair prompt text. Re-run "
        "with --prompt-file, or capture a bundle that includes repair_prompt.txt."
    )


def _extract_json_array(text: str) -> Any | None:
    stripped = text.strip()
    candidates = [stripped]
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def classify_output(text: str) -> dict[str, Any]:
    stripped = text.strip()
    parsed_array = _extract_json_array(stripped)
    starts_with_array = stripped.startswith("[")
    ends_with_array = stripped.endswith("]")
    wrapper_markers = [
        "```",
        "here",
        "ready to help",
        '"status"',
        "explanation",
    ]
    return {
        "output_chars": len(stripped),
        "json_array_compliant": isinstance(parsed_array, list),
        "json_array_length": len(parsed_array) if isinstance(parsed_array, list) else None,
        "starts_with_json_array": starts_with_array,
        "ends_with_json_array": ends_with_array,
        "wrapper_contamination": bool(
            stripped
            and (
                not starts_with_array
                or not ends_with_array
                or any(marker.lower() in stripped.lower() for marker in wrapper_markers)
            )
        ),
        "preview": stripped[:500],
    }


def run_direct_chat_completion(
    prompt: str,
    *,
    base_url: str,
    model: str,
    timeout_seconds: int,
    max_tokens: int,
    temperature: float,
    api_key: str,
    disable_thinking: bool,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if disable_thinking:
        payload["enable_thinking"] = False
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    started_at = time.monotonic()
    first_event_at: float | None = None
    first_token_at: float | None = None
    first_content_at: float | None = None
    chunks: list[str] = []
    reasoning_chars = 0
    reasoning_event_count = 0
    content_event_count = 0
    event_count = 0

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            while True:
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                event_count += 1
                if first_event_at is None:
                    first_event_at = time.monotonic()
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                delta = choices[0].get("delta")
                if not isinstance(delta, dict):
                    continue
                reasoning = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("reasoning_text")
                )
                content = delta.get("content")
                token_text = content or reasoning
                if not token_text:
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic()
                if reasoning:
                    reasoning_text = str(reasoning)
                    reasoning_chars += len(reasoning_text)
                    reasoning_event_count += 1
                if content:
                    if first_content_at is None:
                        first_content_at = time.monotonic()
                    content_event_count += 1
                    chunks.append(str(content))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "backend": "direct_chat_completions",
            "http_status": exc.code,
            "error": body[:1000],
            "duration_seconds": round(time.monotonic() - started_at, 3),
        }
    except (urllib.error.URLError, TimeoutError) as exc:
        return {
            "ok": False,
            "backend": "direct_chat_completions",
            "error": str(exc),
            "duration_seconds": round(time.monotonic() - started_at, 3),
        }

    output = "".join(chunks)
    duration = time.monotonic() - started_at
    result = {
        "ok": True,
        "backend": "direct_chat_completions",
        "base_url": base_url,
        "model": model,
        "duration_seconds": round(duration, 3),
        "first_event_after_seconds": (
            None if first_event_at is None else round(first_event_at - started_at, 3)
        ),
        "first_token_after_seconds": (
            None if first_token_at is None else round(first_token_at - started_at, 3)
        ),
        "first_content_after_seconds": (
            None
            if first_content_at is None
            else round(first_content_at - started_at, 3)
        ),
        "event_count": event_count,
        "reasoning_event_count": reasoning_event_count,
        "reasoning_chars": reasoning_chars,
        "content_event_count": content_event_count,
    }
    result.update(classify_output(output))
    return result


async def run_openclaw_probe(
    prompt: str,
    *,
    cwd: Path,
    timeout_seconds: int,
    executable: str | None,
) -> dict[str, Any]:
    openclaw = executable or shutil.which("openclaw")
    if not openclaw:
        return {
            "ok": False,
            "backend": "openclaw_cli",
            "error": "openclaw executable not found in PATH; pass --openclaw-executable",
        }

    session_id = f"phase8a-shadow-openclaw-{int(time.time())}"
    cmd = [
        openclaw,
        "agent",
        "--local",
        "--session-id",
        session_id,
        "--message",
        prompt,
        "--json",
        "--timeout",
        str(timeout_seconds),
    ]
    started_at = time.monotonic()
    first_output_at: float | None = None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd.resolve()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def collect(stream: asyncio.StreamReader | None, chunks: list[str]) -> None:
        nonlocal first_output_at
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            if first_output_at is None:
                first_output_at = time.monotonic()
            chunks.append(line.decode("utf-8", errors="replace").rstrip("\n"))

    stream_task = asyncio.gather(
        collect(process.stdout, stdout_chunks),
        collect(process.stderr, stderr_chunks),
    )
    timed_out = False
    try:
        await asyncio.wait_for(stream_task, timeout=timeout_seconds + 30)
        return_code = await asyncio.wait_for(process.wait(), timeout=30)
    except asyncio.TimeoutError:
        timed_out = True
        process.kill()
        return_code = await process.wait()
        stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream_task

    duration = time.monotonic() - started_at
    stdout_text = "\n".join(stdout_chunks).strip()
    stderr_text = "\n".join(stderr_chunks).strip()
    combined = stdout_text or stderr_text
    result = {
        "ok": return_code == 0 and not timed_out,
        "backend": "openclaw_cli",
        "session_id": session_id,
        "duration_seconds": round(duration, 3),
        "first_output_after_seconds": (
            None if first_output_at is None else round(first_output_at - started_at, 3)
        ),
        "return_code": return_code,
        "timed_out": timed_out,
        "stdout_chars": len(stdout_text),
        "stderr_chars": len(stderr_text),
        "stdout_lines": len([line for line in stdout_chunks if line]),
        "stderr_lines": len([line for line in stderr_chunks if line]),
    }
    result.update(classify_output(combined))
    return result


def interpret(results: dict[str, Any]) -> str:
    direct = results.get("direct") or {}
    openclaw = results.get("openclaw") or {}
    ttft = direct.get("first_token_after_seconds")
    openclaw_first = openclaw.get("first_output_after_seconds")

    if isinstance(ttft, (int, float)) and ttft < 5:
        if openclaw and openclaw_first is None:
            return (
                "direct_ttft_fast_openclaw_silent: direct model emitted quickly, "
                "while OpenClaw emitted no output. This points to OpenClaw "
                "pre-inference/session/gateway behavior."
            )
        return (
            "direct_ttft_fast: direct model emitted quickly. If live OpenClaw "
            "repair stalls on the same prompt, suspect OpenClaw pre-inference "
            "or gateway routing."
        )
    if isinstance(ttft, (int, float)) and ttft >= 100:
        return (
            "direct_ttft_slow: direct model first token was also slow. This "
            "supports vLLM slot saturation or backend scheduling as the cause."
        )
    if direct.get("ok") is False:
        return "direct_probe_failed: inspect direct.error before drawing boundary conclusions."
    return "inconclusive: direct probe produced no measured first token."


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline Phase 8A shadow probe for repair prompts. Sends a captured "
            "prompt directly to an OpenAI-compatible /chat/completions endpoint "
            "and optionally compares the OpenClaw CLI path."
        )
    )
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--base-url", default=os.getenv("VLLM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.getenv("VLLM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api-key", default=os.getenv("VLLM_API_KEY", ""))
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--no-think-prefix",
        action="store_true",
        help="Prefix the prompt with /no_think for Qwen3 no-thinking probes.",
    )
    parser.add_argument(
        "--disable-thinking",
        action="store_true",
        help=(
            "Pass enable_thinking=false and "
            "chat_template_kwargs.enable_thinking=false to vLLM."
        ),
    )
    parser.add_argument("--run-openclaw", action="store_true")
    parser.add_argument("--openclaw-timeout", type=int, default=240)
    parser.add_argument("--openclaw-executable")
    parser.add_argument("--cwd", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    prompt, prompt_source = load_prompt(
        prompt_file=args.prompt_file,
        bundle_dir=args.bundle_dir,
    )
    prompt_transforms: list[str] = []
    if args.no_think_prefix:
        prompt = "/no_think\n" + prompt
        prompt_transforms.append("no_think_prefix")
    if args.disable_thinking:
        prompt_transforms.append("disable_thinking_request")
    results: dict[str, Any] = {
        "schema_version": 1,
        "note": DEFAULT_GATEWAY_NOTE,
        "prompt_source": prompt_source,
        "prompt_transforms": prompt_transforms,
        "prompt_chars": len(prompt),
        "direct": run_direct_chat_completion(
            prompt,
            base_url=args.base_url,
            model=args.model,
            timeout_seconds=args.timeout,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            api_key=args.api_key,
            disable_thinking=args.disable_thinking,
        ),
    }
    if args.run_openclaw:
        results["openclaw"] = asyncio.run(
            run_openclaw_probe(
                prompt,
                cwd=args.cwd,
                timeout_seconds=args.openclaw_timeout,
                executable=args.openclaw_executable,
            )
        )
    results["interpretation"] = interpret(results)

    output = _json_dumps(results)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
