"""One-off PR05-R1 provider dispatch gate and evidence recorder.

This module is evaluation tooling. It does not participate in normal
application routing. The live harness installs it around the existing
``httpx`` dispatch boundary and scopes each call to one fixed case/stage.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit


class CallGateRefusal(RuntimeError):
    """An outbound request was refused before provider dispatch."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"PR05-R1 provider call gate refused dispatch: {reason}")


class EvidenceCaptureFailure(RuntimeError):
    """A dispatched request could not be durably captured."""


@dataclass(frozen=True)
class DispatchReceipt:
    case_id: str
    sequence: int
    stage: str
    endpoint: str
    model: str
    request_payload: Mapping[str, Any]
    prompt_sha256: str
    prompt_text: str
    started_at: float
    request_dir: Path | None


_ACTIVE_SCOPE: ContextVar[tuple["ProviderCallGate", str, str] | None] = ContextVar(
    "pr05_r1_provider_call_scope", default=None
)


def _normal_endpoint(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_payload(item)
            for key, item in value.items()
            if not _is_sensitive_payload_key(str(key))
        }
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def _is_sensitive_payload_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return (
        normalized in {"token", "access_token", "refresh_token", "client_secret"}
        or "authorization" in normalized
        or "api_key" in normalized
        or normalized.endswith("_secret")
    )


def _prompt_from_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return "", ""
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False, sort_keys=True)
        rendered.append(f"[{role}]\n{text}")
    prompt_text = "\n\n".join(rendered)
    prompt_digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    return prompt_text, prompt_digest


def _parse_provider_response(
    response_body: bytes,
) -> tuple[Any, str | None, Any, str]:
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except Exception:
        decoded = None
    if isinstance(decoded, dict):
        choices = decoded.get("choices", [])
        first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first_choice.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        status = (
            "assistant_content_received"
            if isinstance(content, str)
            else "provider_envelope_valid_no_text_content"
        )
        return (
            decoded,
            content if isinstance(content, str) else None,
            decoded.get("usage"),
            status,
        )

    chunks: list[str] = []
    stream_events: list[dict[str, Any]] = []
    try:
        for line in response_body.decode("utf-8").splitlines():
            if not line.startswith("data:"):
                continue
            raw_data = line[5:].strip()
            if not raw_data or raw_data == "[DONE]":
                continue
            event = json.loads(raw_data)
            if not isinstance(event, dict):
                continue
            stream_events.append(event)
            choices = event.get("choices", [])
            first_choice = (
                choices[0] if choices and isinstance(choices[0], dict) else {}
            )
            delta = first_choice.get("delta", {})
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                chunks.append(content)
        if stream_events:
            usage = next(
                (
                    event["usage"]
                    for event in reversed(stream_events)
                    if "usage" in event
                ),
                None,
            )
            content = "".join(chunks)
            status = (
                "sse_assistant_content_received"
                if content
                else "sse_provider_envelope_valid_no_text_content"
            )
            return stream_events, content, usage, status
    except Exception:
        pass
    return None, None, None, "provider_envelope_parse_failed"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o777)
    os.chmod(path.parent, 0o777)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o666)
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


class ProviderCallGate:
    """Enforce one Planning and one repair dispatch per fixed case, four total."""

    ALLOWED_STAGES = frozenset({"planning", "repair"})

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        case_ids: tuple[str, ...],
        evidence_root: Path | None = None,
        permitted_read_urls: tuple[str, ...] = (),
        per_case_ceiling: int = 2,
        total_ceiling: int = 4,
    ) -> None:
        self.endpoint = _normal_endpoint(endpoint)
        if not self.endpoint:
            raise ValueError("endpoint must be a safe absolute HTTP(S) base URL")
        self.generation_url = f"{self.endpoint}/chat/completions"
        self.models_url = f"{self.endpoint}/models"
        self.permitted_read_urls = {
            _normal_endpoint(url)
            for url in permitted_read_urls
            if _normal_endpoint(url)
        }
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("model identifier is required")
        self.case_ids = tuple(case_ids)
        if not self.case_ids or len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("case_ids must be non-empty and unique")
        if per_case_ceiling != 2 or total_ceiling != 4:
            raise ValueError("PR05-R1 ceilings are fixed at 2 per case and 4 total")
        self.per_case_ceiling = per_case_ceiling
        self.total_ceiling = total_ceiling
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self.total_dispatched = 0
        self._reserved_total = 0
        self._reserved_counts = {
            case_id: {"planning": 0, "repair": 0} for case_id in self.case_ids
        }
        self._gate_lock = threading.Lock()
        self.counts = {
            case_id: {"planning": 0, "repair": 0} for case_id in self.case_ids
        }
        self.receipts: list[DispatchReceipt] = []
        self.refusals: list[dict[str, Any]] = []
        self.permitted_non_generation_requests: list[dict[str, str]] = []
        self.evidence_failed = False

    @property
    def per_case_counts(self) -> dict[str, int]:
        return {
            case_id: sum(stages.values()) for case_id, stages in self.counts.items()
        }

    @contextmanager
    def scope(self, case_id: str, stage: str) -> Iterator[None]:
        if case_id not in self.counts:
            raise ValueError(f"unknown evaluation case: {case_id}")
        if stage not in self.ALLOWED_STAGES:
            raise ValueError(f"unsupported evaluation stage: {stage}")
        token = _ACTIVE_SCOPE.set((self, case_id, stage))
        try:
            yield
        finally:
            _ACTIVE_SCOPE.reset(token)

    def _refuse(self, reason: str, *, method: str, url: str, model: str = "") -> None:
        active = _ACTIVE_SCOPE.get()
        record: dict[str, Any] = {
            "refused_at_monotonic": time.monotonic(),
            "reason": reason,
            "method": method.upper(),
            "endpoint": _normal_endpoint(url),
            "model": model,
            "dispatched": False,
            "total_dispatched": self.total_dispatched,
            "per_case_dispatched": self.per_case_counts,
        }
        if active is not None and active[0] is self:
            record.update({"case_id": active[1], "stage": active[2]})
        self.refusals.append(record)
        self._write_refusal(record)
        raise CallGateRefusal(reason)

    def _write_refusal(self, record: Mapping[str, Any]) -> None:
        if self.evidence_root is None:
            return
        try:
            path = self.evidence_root / "call-gate-refusals.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o777)
            os.chmod(path.parent, 0o777)
            with path.open("ab") as stream:
                stream.write((json.dumps(record, sort_keys=True) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o666)
        except OSError as exc:
            self.evidence_failed = True
            raise EvidenceCaptureFailure(
                "refusal evidence could not be persisted"
            ) from exc

    def refuse_policy(
        self, *, case_id: str, stage: str, route: str, reason: str
    ) -> None:
        record = {
            "refused_at_monotonic": time.monotonic(),
            "reason": reason,
            "route": route,
            "case_id": case_id,
            "stage": stage,
            "dispatched": False,
            "total_dispatched": self.total_dispatched,
            "per_case_dispatched": self.per_case_counts,
        }
        self.refusals.append(record)
        self._write_refusal(record)
        raise CallGateRefusal(reason)

    def authorize(
        self,
        *,
        method: str,
        url: str,
        body: bytes,
    ) -> DispatchReceipt | None:
        method = method.upper()
        normalized_url = _normal_endpoint(url)
        if self.evidence_failed:
            self._refuse("evidence_capture_failed", method=method, url=url)
        if method == "GET" and normalized_url in {
            self.models_url,
            *self.permitted_read_urls,
        }:
            self.permitted_non_generation_requests.append(
                {
                    "method": method,
                    "endpoint": normalized_url,
                    "purpose": (
                        "provider_model_list"
                        if normalized_url == self.models_url
                        else "allowlisted_non_generation_metadata"
                    ),
                }
            )
            return None
        if method != "POST":
            self._refuse("unexpected_http_method", method=method, url=url)
        if normalized_url != self.generation_url:
            self._refuse("alternate_endpoint_or_path", method=method, url=url)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._refuse("invalid_generation_json", method=method, url=url)
        if not isinstance(payload, dict):
            self._refuse("invalid_generation_payload", method=method, url=url)
        payload, body = self._normalize_reasoning_control(payload)
        request_model = str(payload.get("model") or "")
        if request_model != self.model:
            self._refuse("alternate_model", method=method, url=url, model=request_model)
        active = _ACTIVE_SCOPE.get()
        if active is None or active[0] is not self:
            self._refuse("unscoped_generation_request", method=method, url=url)
        _, case_id, stage = active
        if stage not in self.ALLOWED_STAGES or case_id not in self.counts:
            self._refuse("unexpected_case_or_stage", method=method, url=url)
        with self._gate_lock:
            self._check_budget(case_id, stage, method=method, url=url)
            sequence = (
                sum(self.counts[case_id].values())
                + sum(self._reserved_counts[case_id].values())
                + 1
            )
            self._reserved_total += 1
            self._reserved_counts[case_id][stage] += 1
        safe_payload = _safe_payload(payload)
        prompt_text, prompt_sha256 = _prompt_from_payload(safe_payload)
        request_dir = (
            self.evidence_root / "requests" / case_id / f"request-{sequence:02d}"
            if self.evidence_root is not None
            else None
        )
        receipt = DispatchReceipt(
            case_id=case_id,
            sequence=sequence,
            stage=stage,
            endpoint=self.generation_url,
            model=self.model,
            request_payload=safe_payload,
            prompt_sha256=prompt_sha256,
            prompt_text=prompt_text,
            started_at=time.monotonic(),
            request_dir=request_dir,
        )
        if request_dir is not None:
            try:
                request_dir.mkdir(parents=True, exist_ok=False, mode=0o777)
                os.chmod(request_dir, 0o777)
                _atomic_write(
                    request_dir / "request.json",
                    (
                        json.dumps(
                            {
                                "case_id": case_id,
                                "sequence": sequence,
                                "stage": stage,
                                "provider_adapter": "openai_chat_completions",
                                "endpoint": self.generation_url,
                                "model": self.model,
                                "prompt_sha256": prompt_sha256,
                                "request_payload": safe_payload,
                                "request_payload_sha256": hashlib.sha256(
                                    body
                                ).hexdigest(),
                                "authorization_header_retained": False,
                                "dispatch_started": False,
                            },
                            indent=2,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                _atomic_write(request_dir / "prompt.txt", prompt_text.encode("utf-8"))
            except OSError as exc:
                with self._gate_lock:
                    self._release_reservation(receipt)
                    self.evidence_failed = True
                raise EvidenceCaptureFailure(
                    "request evidence could not be persisted before provider dispatch"
                ) from exc
        return receipt

    def _normalize_reasoning_control(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        """Translate disabled-thinking flags to Ollama's recognized field."""

        normalized = dict(payload)
        template_options = normalized.pop("chat_template_kwargs", {})
        if not isinstance(template_options, dict) or any(
            key != "enable_thinking" or value is not False
            for key, value in template_options.items()
        ):
            self._refuse(
                "unsupported_chat_template_options",
                method="POST",
                url=self.generation_url,
                model=str(normalized.get("model") or ""),
            )
        for key in ("think", "enable_thinking"):
            if key in normalized and normalized[key] is not False:
                self._refuse(
                    "reasoning_must_be_disabled",
                    method="POST",
                    url=self.generation_url,
                    model=str(normalized.get("model") or ""),
                )
            normalized.pop(key, None)
        if normalized.get("reasoning_effort") not in (None, "none"):
            self._refuse(
                "reasoning_must_be_disabled",
                method="POST",
                url=self.generation_url,
                model=str(normalized.get("model") or ""),
            )
        normalized["reasoning_effort"] = "none"
        encoded = json.dumps(
            normalized, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return normalized, encoded

    def prepare_outbound_body(self, *, method: str, url: str, body: bytes) -> bytes:
        if method.upper() != "POST" or _normal_endpoint(url) != self.generation_url:
            return body
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return body
        if not isinstance(payload, dict):
            return body
        return self._normalize_reasoning_control(payload)[1]

    def _check_budget(self, case_id: str, stage: str, *, method: str, url: str) -> None:
        if self.total_dispatched + self._reserved_total >= self.total_ceiling:
            self._refuse("total_ceiling_exhausted", method=method, url=url)
        case_total = sum(self.counts[case_id].values()) + sum(
            self._reserved_counts[case_id].values()
        )
        if case_total >= self.per_case_ceiling:
            self._refuse("case_ceiling_exhausted", method=method, url=url)
        if self.counts[case_id][stage] + self._reserved_counts[case_id][stage] >= 1:
            self._refuse("stage_dispatch_already_used", method=method, url=url)

    def _release_reservation(self, receipt: DispatchReceipt) -> None:
        self._reserved_total = max(0, self._reserved_total - 1)
        self._reserved_counts[receipt.case_id][receipt.stage] = max(
            0, self._reserved_counts[receipt.case_id][receipt.stage] - 1
        )

    def begin_dispatch(self, receipt: DispatchReceipt | None) -> None:
        """Persist dispatch evidence, then count immediately before HTTPX send."""

        if receipt is None:
            return
        if receipt.request_dir is not None:
            record = {
                "case_id": receipt.case_id,
                "sequence": receipt.sequence,
                "stage": receipt.stage,
                "provider_adapter": "openai_chat_completions",
                "endpoint": receipt.endpoint,
                "model": receipt.model,
                "dispatch_started": True,
                "actual_request_count_before": self.total_dispatched,
                "actual_request_count_after": self.total_dispatched + 1,
            }
            try:
                _atomic_write(
                    receipt.request_dir / "dispatch.json",
                    (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(),
                )
            except OSError as exc:
                with self._gate_lock:
                    self._release_reservation(receipt)
                    self.evidence_failed = True
                raise EvidenceCaptureFailure(
                    "dispatch evidence could not be persisted before provider send"
                ) from exc
        with self._gate_lock:
            self._release_reservation(receipt)
            self.total_dispatched += 1
            self.counts[receipt.case_id][receipt.stage] += 1
            self.receipts.append(receipt)

    def record_response(
        self,
        receipt: DispatchReceipt | None,
        *,
        status_code: int,
        response_body: bytes,
        latency_seconds: float,
    ) -> None:
        if receipt is None:
            return
        response_hash = hashlib.sha256(response_body).hexdigest()
        decoded, assistant_content, usage, parse_status = _parse_provider_response(
            response_body
        )
        if receipt.request_dir is not None:
            try:
                _atomic_write(receipt.request_dir / "response.raw", response_body)
                record = {
                    "case_id": receipt.case_id,
                    "sequence": receipt.sequence,
                    "stage": receipt.stage,
                    "provider_adapter": "openai_chat_completions",
                    "endpoint": receipt.endpoint,
                    "model": receipt.model,
                    "prompt_sha256": receipt.prompt_sha256,
                    "response_sha256": response_hash,
                    "response_parse_status": parse_status,
                    "assistant_content_sha256": (
                        hashlib.sha256(assistant_content.encode("utf-8")).hexdigest()
                        if isinstance(assistant_content, str)
                        else None
                    ),
                    "token_usage": usage if isinstance(usage, dict) else None,
                    "latency_seconds": round(latency_seconds, 3),
                    "http_status": status_code,
                    "dispatch_started": True,
                    "provider_response_received": True,
                    "response_bytes": len(response_body),
                }
                _atomic_write(
                    receipt.request_dir / "record.json",
                    (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(
                        "utf-8"
                    ),
                )
            except OSError as exc:
                self.evidence_failed = True
                raise EvidenceCaptureFailure(
                    "provider response evidence could not be persisted"
                ) from exc

    def record_transport_settings(self, receipt: DispatchReceipt | None, client: Any):
        """Persist request timeout and redirect settings before network I/O."""

        if receipt is None:
            return
        transport = getattr(client, "_transport", None)
        retries = getattr(transport, "retries", 0)
        if retries not in (None, 0):
            with self._gate_lock:
                self._release_reservation(receipt)
            self._refuse(
                "automatic_transport_retries_not_permitted",
                method="POST",
                url=receipt.endpoint,
                model=receipt.model,
            )
        if receipt.request_dir is None:
            return
        timeout = getattr(client, "timeout", None)
        timeout_fields = ("connect", "read", "write", "pool")
        timeout_payload = (
            {name: getattr(timeout, name, None) for name in timeout_fields}
            if timeout is not None
            else None
        )
        try:
            _atomic_write(
                receipt.request_dir / "transport.json",
                (
                    json.dumps(
                        {
                            "http_client": type(client).__name__,
                            "transport": (
                                type(transport).__name__ if transport else None
                            ),
                            "transport_retries": 0,
                            "timeout_seconds": timeout_payload,
                            "follow_redirects": bool(
                                getattr(client, "follow_redirects", False)
                            ),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        except OSError as exc:
            with self._gate_lock:
                self._release_reservation(receipt)
            self.evidence_failed = True
            raise EvidenceCaptureFailure(
                "transport settings evidence could not be persisted before provider dispatch"
            ) from exc

    def record_error(
        self,
        receipt: DispatchReceipt | None,
        *,
        error: BaseException,
        latency_seconds: float,
    ) -> None:
        if receipt is None:
            return
        if receipt.request_dir is not None:
            try:
                record = {
                    "case_id": receipt.case_id,
                    "sequence": receipt.sequence,
                    "stage": receipt.stage,
                    "provider_adapter": "openai_chat_completions",
                    "endpoint": receipt.endpoint,
                    "model": receipt.model,
                    "prompt_sha256": receipt.prompt_sha256,
                    "latency_seconds": round(latency_seconds, 3),
                    "provider_error_type": type(error).__name__,
                    "provider_error": str(error)[:500],
                    "dispatch_started": True,
                    "provider_response_received": False,
                    "budget_consumed": True,
                }
                _atomic_write(
                    receipt.request_dir / "record.json",
                    (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(
                        "utf-8"
                    ),
                )
            except OSError:
                self.evidence_failed = True

    def inventory(self) -> dict[str, Any]:
        return {
            "provider_adapter": "openai_chat_completions",
            "endpoint": self.generation_url,
            "model": self.model,
            "per_case_ceiling": self.per_case_ceiling,
            "total_ceiling": self.total_ceiling,
            "total_dispatched": self.total_dispatched,
            "per_case_dispatched": self.per_case_counts,
            "phase_dispatched": self.counts,
            "permitted_non_generation_requests": list(
                self.permitted_non_generation_requests
            ),
            "refusals": list(self.refusals),
            "evidence_capture_failed": self.evidence_failed,
        }


def install_httpx_boundary(gate: ProviderCallGate):
    """Install a fail-closed gate on all HTTPX sends and disable transport retries."""

    import httpx
    import inspect

    originals: list[tuple[Any, str, Any]] = []

    def patch_transport(transport_type: type) -> None:
        original_init = transport_type.__init__
        signature = inspect.signature(original_init)

        def no_retry_init(self, *args, **kwargs):
            try:
                bound = signature.bind_partial(self, *args, **kwargs)
                retries = bound.arguments.get("retries", 0)
            except TypeError:
                retries = kwargs.get("retries", 0)
            if retries not in (None, 0):
                gate._refuse(
                    "automatic_transport_retries_not_permitted",
                    method="POST",
                    url=gate.generation_url,
                )
            return original_init(self, *args, **kwargs)

        transport_type.__init__ = no_retry_init
        originals.append((transport_type, "__init__", original_init))

    patch_transport(httpx.AsyncHTTPTransport)
    patch_transport(httpx.HTTPTransport)

    def prepare_request(request, body: bytes):
        prepared_body = gate.prepare_outbound_body(
            method=request.method, url=str(request.url), body=body
        )
        if prepared_body == body:
            return request, prepared_body
        headers = dict(request.headers)
        headers.pop("content-length", None)
        headers.pop("transfer-encoding", None)
        return (
            httpx.Request(
                request.method,
                request.url,
                headers=headers,
                content=prepared_body,
                extensions=dict(request.extensions),
            ),
            prepared_body,
        )

    original_async_send = httpx.AsyncClient.send

    def redirects_enabled(client, kwargs) -> bool:
        setting = kwargs.get("follow_redirects", httpx.USE_CLIENT_DEFAULT)
        if setting is httpx.USE_CLIENT_DEFAULT or setting is None:
            setting = client.follow_redirects
        return bool(setting)

    async def gated_async_send(self, request, *args, **kwargs):
        body = await request.aread()
        request, body = prepare_request(request, body)
        if redirects_enabled(self, kwargs):
            gate._refuse(
                "http_redirects_not_permitted",
                method=request.method,
                url=str(request.url),
                model=(
                    str(json.loads(body.decode("utf-8")).get("model") or "")
                    if request.method.upper() == "POST"
                    else ""
                ),
            )
        receipt = gate.authorize(method=request.method, url=str(request.url), body=body)
        gate.record_transport_settings(receipt, self)
        gate.begin_dispatch(receipt)
        started_at = time.monotonic()
        try:
            response = await original_async_send(self, request, *args, **kwargs)
        except BaseException as exc:
            gate.record_error(
                receipt,
                error=exc,
                latency_seconds=time.monotonic() - started_at,
            )
            raise
        if receipt is not None:
            try:
                response_body = await response.aread()
                gate.record_response(
                    receipt,
                    status_code=int(response.status_code),
                    response_body=response_body,
                    latency_seconds=time.monotonic() - started_at,
                )
            except EvidenceCaptureFailure:
                raise
            except BaseException as exc:
                gate.record_error(
                    receipt,
                    error=exc,
                    latency_seconds=time.monotonic() - started_at,
                )
                raise
        return response

    httpx.AsyncClient.send = gated_async_send
    originals.append((httpx.AsyncClient, "send", original_async_send))

    original_sync_send = httpx.Client.send

    def gated_sync_send(self, request, *args, **kwargs):
        body = request.read()
        request, body = prepare_request(request, body)
        if redirects_enabled(self, kwargs):
            gate._refuse(
                "http_redirects_not_permitted",
                method=request.method,
                url=str(request.url),
                model=(
                    str(json.loads(body.decode("utf-8")).get("model") or "")
                    if request.method.upper() == "POST"
                    else ""
                ),
            )
        receipt = gate.authorize(method=request.method, url=str(request.url), body=body)
        gate.record_transport_settings(receipt, self)
        gate.begin_dispatch(receipt)
        started_at = time.monotonic()
        try:
            response = original_sync_send(self, request, *args, **kwargs)
        except BaseException as exc:
            gate.record_error(
                receipt,
                error=exc,
                latency_seconds=time.monotonic() - started_at,
            )
            raise
        if receipt is not None:
            try:
                response_body = response.read()
                gate.record_response(
                    receipt,
                    status_code=int(response.status_code),
                    response_body=response_body,
                    latency_seconds=time.monotonic() - started_at,
                )
            except EvidenceCaptureFailure:
                raise
            except BaseException as exc:
                gate.record_error(
                    receipt,
                    error=exc,
                    latency_seconds=time.monotonic() - started_at,
                )
                raise
        return response

    httpx.Client.send = gated_sync_send
    originals.append((httpx.Client, "send", original_sync_send))

    @contextmanager
    def installed() -> Iterator[None]:
        try:
            yield
        finally:
            for owner, name, original in reversed(originals):
                setattr(owner, name, original)

    return installed()


__all__ = [
    "CallGateRefusal",
    "DispatchReceipt",
    "EvidenceCaptureFailure",
    "ProviderCallGate",
    "install_httpx_boundary",
]
