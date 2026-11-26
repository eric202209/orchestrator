"""P43-PRODUCT-23C: bounded correction for one real-provider evaluation (offline).

Evidence classes (see the PRODUCT-23C report):

* B1 retry refusal: the real ``FailureCoordinator`` with Celery task doubles
  and the real ``EnhancedErrorHandler`` retry classification;
* B2 Discovery search: the real ``search_text`` executor and the local ``rg``
  (the Worker image build itself is live-only);
* B3 relay: the real relay process logic over loopback against a local stub
  upstream (no model, no real provider);
* model submission, role routing, evaluation integrity and contamination:
  real submitter/runtime-resolution code with stubbed queueing.

The frozen PRODUCT-18 material is read from the local, git-ignored evidence
tree and never vendored; tests needing it skip when it is absent (CI).
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import settings
from app.models import Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.orchestration.coordinators.failure_coordinator import (
    FailureCoordinator,
)
from app.services.orchestration.error_handler import EnhancedErrorHandler
from app.services.orchestration.planning import read_only_discovery
from app.services.orchestration.worker_boundary import (
    disposable_c2_execution_mode,
    validate_structured_http_provider_contract,
)
from app.tests import test_p43_product22_c2_c3b_live_execution as c3b
from app.tests import test_p43_product23b_trusted_controls as c23b
from app.tests import test_phase14b2_failure_coordinator as fc
from scripts.maintenance import p43_c2_provider_relay as relay_module
from scripts.maintenance import p43_c2_trusted_submitter as submitter

c2_env = c3b.c2_env
REPO = c23b.REPO
LIVE_RUN = c23b.LIVE_RUN
DOCKERFILE = REPO / "Dockerfile.worker"
TEMPLATE = REPO / "config" / "worker-c2-live.env.example"
PROVIDER_DELTA = REPO / "config" / "worker-c2-provider.env.example"
RELAY_BASE = "http://model-provider:8000/v1"
MODEL = "qwen-local"

C2_MODE = {
    "WORKER_ISOLATION_MODE": "disposable",
    "WORKER_ENVIRONMENT": "c2-c1-test",
    "WORKER_BRIDGE_MODE": "c2b-sandbox",
    "C2_DISPOSABLE_EXECUTION_ADMISSION": "c2-c3-single-sandbox-slice",
}


# --- B1: no automatic same-attempt retry in C2 mode --------------------------

# Product failure texts of each class. The real error handler classifies all
# three as transient, so outside C2 mode each is Celery-retried (max 3).
FAILURES = {
    "verification": "Step 2 verification failed: python -m pytest tests/test_ranges.py"
    " -q exited with status 1",
    "acceptance": "Completion acceptance failed: python -m pytest"
    " tests/test_rangekit_acceptance.py -q exited with status 1",
    "provider_transport": "OpenAI-compatible chat request failed: HTTP 503 from"
    " http://model-provider:8000/v1/chat/completions",
}


@pytest.fixture
def c2_mode(monkeypatch):
    for name, value in C2_MODE.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def normal_mode(monkeypatch):
    for name in C2_MODE:
        monkeypatch.delenv(name, raising=False)


def test_c2_execution_mode_is_exact():
    assert disposable_c2_execution_mode(C2_MODE)
    for name in C2_MODE:
        assert not disposable_c2_execution_mode({**C2_MODE, name: ""})
        assert not disposable_c2_execution_mode({**C2_MODE, name: "other"})
    assert not disposable_c2_execution_mode({})


def _fail(ctx, exc, self_task, queue=None):
    real = EnhancedErrorHandler()
    handler = type(
        "EH", (), {"should_retry": staticmethod(lambda e, c: real.should_retry(e))}
    )()
    object.__setattr__(ctx, "error_handler", handler)
    FailureCoordinator().handle_failure(
        self_task=self_task,
        ctx=ctx,
        exc=exc,
        get_latest_session_task_link_fn=lambda *a, **k: None,
        write_project_state_snapshot_fn=fc._NOOP,
        save_orchestration_checkpoint_fn=fc._NOOP,
        record_live_log_fn=fc._NOOP,
        queue_task_for_session_fn=queue,
    )


class _RecordingRetryTask(fc._RetryCapableSelfTask):
    def __init__(self):
        self.calls = 0

    def retry(self, exc, **kwargs):
        self.calls += 1
        return super().retry(exc, **kwargs)


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_failures_are_retried_outside_c2_mode(db_session, normal_mode, kind):
    """Baseline: the unchanged normal path still schedules a Celery retry."""

    ctx, session, _task, execution = fc._seed_ctx(db_session)
    task = _RecordingRetryTask()
    with pytest.raises(fc._RetryCapableSelfTask._RetrySignal):
        _fail(ctx, RuntimeError(FAILURES[kind]), task)
    db_session.refresh(session)
    db_session.refresh(execution)
    assert task.calls == 1
    assert session.status == "retry_pending"
    assert session.continuation_kind == "celery_retry"


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_c2_failures_are_terminal_without_celery_retry(db_session, c2_mode, kind):
    ctx, session, task_row, execution = fc._seed_ctx(db_session)
    task = _RecordingRetryTask()
    exc = RuntimeError(FAILURES[kind])
    with pytest.raises(RuntimeError) as raised:
        _fail(ctx, exc, task)
    assert raised.value is exc
    db_session.refresh(session)
    db_session.refresh(task_row)
    db_session.refresh(execution)
    assert task.calls == 0
    assert execution.status == TaskStatus.FAILED
    assert task_row.status == TaskStatus.FAILED
    assert session.status == "failed"
    assert session.continuation_kind is None
    assert db_session.query(TaskExecution).count() == 1


def test_c2_automatic_mode_never_queues_a_recovery_rerun(db_session, c2_mode):
    ctx, session, _task, execution = fc._seed_ctx(
        db_session, execution_mode="automatic", plan_position=1
    )
    queued = []
    with pytest.raises(RuntimeError):
        _fail(
            ctx,
            RuntimeError(FAILURES["verification"]),
            fc._TerminalSelfTask(),
            queue=lambda **kwargs: queued.append(kwargs),
        )
    db_session.refresh(execution)
    assert queued == []
    assert execution.status == TaskStatus.FAILED
    assert db_session.query(TaskExecution).count() == 1


def test_c2_terminal_outcome_equals_the_existing_exhausted_retry_path(
    db_session, db_session_factory, monkeypatch
):
    """The C2 refusal reuses the existing terminal path unchanged."""

    def outcome(env: dict) -> tuple:
        for name in C2_MODE:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        ctx, session, task_row, execution = fc._seed_ctx(db_session)
        self_task = _RecordingRetryTask() if env else fc._TerminalSelfTask()
        with pytest.raises(RuntimeError):
            _fail(ctx, RuntimeError(FAILURES["acceptance"]), self_task)
        for row in (session, task_row, execution):
            db_session.refresh(row)
        return (
            session.status,
            session.is_active,
            task_row.status,
            execution.status,
            execution.failure_category,
        )

    assert outcome(C2_MODE) == outcome({})


def test_e4_capacity_retry_cannot_apply_to_the_c2_backend():
    """The remaining automatic retry site is pre-claim capacity admission,
    governed only for backends with a parallel-session limit."""

    from app.services.agents.agent_backends import get_backend_descriptor

    descriptor = get_backend_descriptor("openai_chat_completions")
    assert descriptor.capabilities.max_parallel_sessions is None


# --- B2: ripgrep for Discovery ----------------------------------------------


def test_worker_image_installs_ripgrep_for_the_non_root_worker():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "apt-get install --no-install-recommends -y ca-certificates git ripgrep" in (
        text
    )
    assert text.count("ARG PYTHON_BASE_IMAGE=python:3.12-slim") == 1
    user_at = text.index("USER orchestrator-worker")
    assert text.index("RUN rg --version") > user_at
    assert "docker.sock" not in text and "privileged" not in text


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed locally")
def test_discovery_search_runs_rg_with_fixed_argv_and_no_shell(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "rangekit").mkdir(parents=True)
    (root / "rangekit" / "ranges.py").write_text(
        "def merge_ranges(ranges):\n    return ranges\n", encoding="utf-8"
    )
    calls = []
    real_run = subprocess.run

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(read_only_discovery.subprocess, "run", run)
    request = read_only_discovery.parse_discovery_request(
        json.dumps(
            {
                "action": "search_text",
                "query": "def merge_ranges",
                "paths": ["rangekit"],
            }
        )
    )
    observation = read_only_discovery.execute_discovery_request(root, request)
    assert [(h.path, h.line_number) for h in observation.hits] == [
        ("rangekit/ranges.py", 1)
    ]
    ((argv, kwargs),) = calls
    assert argv[0] == shutil.which("rg") and kwargs["shell"] is False
    assert argv[-3:] == ["--", "def merge_ranges", "rangekit"]


def test_discovery_search_still_fails_closed_without_rg(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    monkeypatch.setattr(read_only_discovery.shutil, "which", lambda name: None)
    request = read_only_discovery.DiscoveryRequest(
        action="search_text", query="x", paths=("pkg",)
    )
    with pytest.raises(
        read_only_discovery.DiscoveryContractError,
        match="discovery_search_execution_failed",
    ):
        read_only_discovery.execute_discovery_request(tmp_path, request)


# --- B3: relay against a local stub upstream --------------------------------


class _Upstream:
    """Local stub provider. ``mode`` selects the next completion behaviour."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.mode = "ok"
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _reply(self, status, body, headers=()):
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                upstream.requests.append(("GET", self.path, dict(self.headers), b""))
                if self.path == "/redirect-target":
                    return self._reply(200, b"{}")
                data = [
                    {"id": MODEL, "root": "/models/q", "max_model_len": 131072},
                    {"id": "other-model"},
                ]
                self._reply(200, json.dumps({"object": "list", "data": data}).encode())

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                upstream.requests.append(("POST", self.path, dict(self.headers), body))
                mode = upstream.mode
                if mode == "ok" or mode == "fallback":
                    model = MODEL if mode == "ok" else "other-model"
                    answer = {
                        "model": model,
                        "choices": [
                            {"message": {"role": "assistant", "content": "ok"}}
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                    }
                    return self._reply(200, json.dumps(answer).encode())
                if mode == "error":
                    return self._reply(500, b'{"error": "boom"}')
                if mode == "redirect":
                    return self._reply(
                        302, b"", (("Location", f"{upstream.url}/redirect-target"),)
                    )
                if mode == "huge":
                    return self._reply(
                        200, b"x" * (relay_module.MAX_RESPONSE_BYTES + 1)
                    )
                if mode == "slow":
                    time.sleep(5)
                    return self._reply(200, b"{}")
                if mode == "disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self) -> int:
        return sum(method == "POST" for method, *_ in self.requests)


class _RelayHarness:
    def __init__(self, tmp_path: Path, upstream: _Upstream, clock=time.time):
        self.state = tmp_path / "relay-state"
        self.state.mkdir()
        self.ctl = tmp_path / "relay-ctl"
        self.ctl.mkdir()
        self.relay = relay_module.Relay(
            upstream=upstream.url,
            model=MODEL,
            state_dir=self.state,
            deadline_file=self.ctl / "deadline",
            clock=clock,
        )
        self.server = relay_module.build_server(self.relay, ("127.0.0.1", 0))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def arm(self, seconds: int = 1200) -> None:
        (self.ctl / "deadline").write_text(f"{int(time.time()) + seconds}\n")

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = dict(headers or {})
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, data, dict(response.getheaders()))
        connection.close()
        return result

    def chat(self, model=MODEL, **extra):
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        body.update(extra)
        return self.call("POST", "/v1/chat/completions", json.dumps(body).encode())

    def raw(self, data: bytes) -> int:
        with socket.create_connection(("127.0.0.1", self.port), 5) as sock:
            sock.sendall(data)
            reply = sock.recv(4096)
        return int(reply.split(b" ", 2)[1])

    def events(self) -> list[dict]:
        lines = (self.state / "ledger.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    stub = _Upstream()
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


@pytest.fixture
def harness(tmp_path, upstream):
    relay = _RelayHarness(tmp_path, upstream)
    yield relay
    relay.close()


def test_relay_exposes_only_the_two_endpoints_and_the_one_model(harness, upstream):
    harness.arm()
    status, data, _ = harness.call("GET", "/v1/models")
    assert status == 200
    assert [e["id"] for e in json.loads(data)["data"]] == [MODEL]
    refused = {
        "other_path": harness.call("GET", "/v1/models/other")[0],
        "query": harness.call("GET", "/v1/models?x=1")[0],
        "legacy": harness.call("POST", "/v1/completions", b"{}")[0],
        "delete": harness.call("DELETE", "/v1/models")[0],
        "put": harness.call("PUT", "/v1/chat/completions", b"{}")[0],
        "absolute_form": harness.raw(
            b"GET http://1.1.1.1/v1/models HTTP/1.1\r\nHost: 1.1.1.1\r\n\r\n"
        ),
        "connect": harness.raw(b"CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: x\r\n\r\n"),
        "other_model": harness.chat("other-model")[0],
        "streaming": harness.chat(stream=True)[0],
        "multiple_choices": harness.chat(n=2)[0],
        "not_json": harness.call("POST", "/v1/chat/completions", b"not json")[0],
    }
    assert refused == {
        "other_path": 404,
        "query": 404,
        "legacy": 404,
        "delete": 405,
        "put": 405,
        "absolute_form": 404,
        "connect": 405,
        "other_model": 403,
        "streaming": 400,
        "multiple_choices": 400,
        "not_json": 400,
    }
    # Only the model list reached the upstream; nothing was forwarded else.
    assert [(m, p) for m, p, *_ in upstream.requests] == [("GET", "/v1/models")]
    assert not any(e["event"] == "completion_admitted" for e in harness.events())


def test_relay_refuses_chunked_and_oversized_requests(harness, upstream):
    harness.arm()
    chunked = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    )
    assert harness.raw(chunked) == 411
    big = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: "
        + str(relay_module.MAX_REQUEST_BYTES + 1).encode()
        + b"\r\n\r\n"
    )
    assert harness.raw(big) == 413
    assert upstream.posts() == 0


def test_seventh_completion_is_refused_and_failures_consume_budget(harness, upstream):
    harness.arm()
    outcomes = []
    for mode in ("ok", "error", "disconnect", "ok", "error", "ok"):
        upstream.mode = mode
        outcomes.append(harness.chat()[0])
    assert outcomes == [200, 500, 502, 200, 500, 200]
    assert upstream.posts() == 6
    upstream.mode = "ok"
    status, data, _ = harness.chat()
    assert status == 429
    assert json.loads(data)["error"]["code"] == "completion_budget_exhausted"
    assert upstream.posts() == 6  # the seventh never reached the upstream
    # Model-list observations are counted separately and stay available.
    assert harness.call("GET", "/v1/models")[0] == 200
    events = harness.events()
    assert [e["completion_index"] for e in events if e["event"] == "completion"] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    assert sum(e["event"] == "completion_admitted" for e in events) == 6
    assert sum(e["event"] == "model_list" for e in events) == 1
    refused = [e for e in events if e["event"] == "refused"]
    assert [e["reason"] for e in refused] == ["completion_budget_exhausted"]
    assert refused[0]["forwarded"] is False


def test_budget_admission_is_atomic_under_concurrency(tmp_path, upstream):
    harness = _RelayHarness(tmp_path, upstream)
    try:
        harness.arm()
        admitted, refused = [], []
        barrier = threading.Barrier(40)

        def contend(sequence):
            barrier.wait()
            try:
                admitted.append(harness.relay.admit_completion(sequence)[0])
            except relay_module.Refused as error:
                refused.append(error.reason)

        threads = [threading.Thread(target=contend, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(admitted) == [1, 2, 3, 4, 5, 6]
        assert refused == ["completion_budget_exhausted"] * 34
        assert sum(e["event"] == "completion_admitted" for e in harness.events()) == 6
    finally:
        harness.close()


def test_concurrent_forwards_beyond_the_slot_limit_are_refused_unconsumed(
    harness, upstream
):
    harness.arm()
    upstream.mode = "slow"
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(harness.chat()[0]))
        for _ in range(relay_module.MAX_CONCURRENCY)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while (
        upstream.posts() < relay_module.MAX_CONCURRENCY and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    status, data, _ = harness.chat()
    assert status == 429
    assert json.loads(data)["error"]["code"] == "relay_concurrency_exhausted"
    for thread in threads:
        thread.join()
    assert upstream.posts() == relay_module.MAX_CONCURRENCY
    assert sum(e["event"] == "completion_admitted" for e in harness.events()) == 2


def test_no_completion_before_arming_or_after_the_deadline(tmp_path, upstream):
    now = [1_800_000_000.0]
    harness = _RelayHarness(tmp_path, upstream, clock=lambda: now[0])
    try:
        status, data, _ = harness.chat()
        assert status == 503
        assert json.loads(data)["error"]["code"] == "evaluation_not_armed"
        (harness.ctl / "deadline").write_text("1800001200\n")
        now[0] = 1_800_001_200.0
        status, data, _ = harness.chat()
        assert (status, json.loads(data)["error"]["code"]) == (
            503,
            "evaluation_deadline_exhausted",
        )
        # The deadline is read once: rewriting the file cannot extend it.
        (harness.ctl / "deadline").write_text("1900000000\n")
        assert harness.chat()[0] == 503
        assert upstream.posts() == 0
        assert not any(e["event"] == "completion_admitted" for e in harness.events())
    finally:
        harness.close()


def test_upstream_timeout_is_bounded_by_the_remaining_window(tmp_path, upstream):
    now = [1_800_000_000.0]
    harness = _RelayHarness(tmp_path, upstream, clock=lambda: now[0])
    try:
        (harness.ctl / "deadline").write_text("1800000001\n")  # 1 s remaining
        upstream.mode = "slow"  # 5 s
        started = time.monotonic()
        status, data, _ = harness.chat()
        assert time.monotonic() - started < 3
        assert (status, json.loads(data)["error"]["code"]) == (504, "upstream_timeout")
        (completion,) = [e for e in harness.events() if e["event"] == "completion"]
        assert completion["timeout_seconds"] <= 1.0
        assert completion["outcome"] == "upstream_timeout"
        assert completion["completion_index"] == 1  # a timed-out forward consumes
    finally:
        harness.close()


def test_redirects_fallback_and_oversized_replies_are_refused(harness, upstream):
    harness.arm()
    upstream.mode = "redirect"
    status, data, headers = harness.chat()
    assert (status, json.loads(data)["error"]["code"]) == (
        502,
        "upstream_redirect_refused",
    )
    assert "Location" not in headers
    assert not any(p == "/redirect-target" for _, p, *_ in upstream.requests)
    upstream.mode = "fallback"
    status, data, _ = harness.chat()
    assert (status, json.loads(data)["error"]["code"]) == (
        502,
        "upstream_model_mismatch",
    )
    upstream.mode = "huge"
    status, data, _ = harness.chat()
    assert (status, json.loads(data)["error"]["code"]) == (
        502,
        "upstream_response_too_large",
    )
    assert upstream.posts() == 3  # no upstream retry of any failure


def test_relay_records_bodies_but_never_headers_or_credentials(harness, upstream):
    harness.arm()
    secret = "Bearer relay-test-credential"
    body = json.dumps(
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]}
    ).encode()
    status, *_ = harness.call(
        "POST", "/v1/chat/completions", body, {"Authorization": secret}
    )
    assert status == 200
    _, _, headers, forwarded = upstream.requests[-1]
    assert forwarded == body
    assert "Authorization" not in headers
    stored = b"".join(p.read_bytes() for p in harness.state.rglob("*") if p.is_file())
    assert secret.encode() not in stored
    assert (harness.state / "bodies" / "0001.request.json").read_bytes() == body
    modes = {
        oct(p.stat().st_mode & 0o777) for p in (harness.state / "bodies").iterdir()
    }
    assert modes == {"0o600"}


def test_relay_refuses_a_used_state_dir_and_invalid_configuration(tmp_path, upstream):
    harness = _RelayHarness(tmp_path, upstream)
    harness.close()
    with pytest.raises(relay_module.RelayConfigurationError, match="not_empty"):
        relay_module.Relay(
            upstream=upstream.url,
            model=MODEL,
            state_dir=harness.state,
            deadline_file=harness.ctl / "deadline",
        )
    for value in (
        "https://ai-gateway:8000",
        "http://ai-gateway",
        "http://ai-gateway:8000/v1",
        "http://user:pw@ai-gateway:8000",
        "http://ai-gateway:8000?x=1",
        "ai-gateway:8000",
    ):
        with pytest.raises(relay_module.RelayConfigurationError):
            relay_module.parse_upstream(value)
    assert relay_module.parse_upstream("http://ai-gateway:8000") == ("ai-gateway", 8000)
    with pytest.raises(relay_module.RelayConfigurationError):
        relay_module.validate_model("qwen local")


def test_relay_main_refuses_without_contract(tmp_path, capsys):
    assert relay_module.main({}) == 78


def test_relay_binds_only_its_start_time_address(tmp_path, upstream, monkeypatch):
    """Started on the internal network only, the relay never listens on the
    provider network attached afterwards."""

    bound = []

    class _Server:
        def serve_forever(self):
            pass

    monkeypatch.setattr(relay_module.socket, "gethostname", lambda: "relay-host")
    monkeypatch.setattr(
        relay_module.socket,
        "gethostbyname",
        lambda name: {"relay-host": "172.30.0.5"}[name],
    )
    monkeypatch.setattr(
        relay_module,
        "build_server",
        lambda relay, address: bound.append(address) or _Server(),
    )
    (tmp_path / "state").mkdir()
    environ = {
        "P43_RELAY_UPSTREAM": upstream.url,
        "P43_RELAY_MODEL": MODEL,
        "P43_RELAY_STATE_DIR": str(tmp_path / "state"),
        "P43_RELAY_DEADLINE_FILE": str(tmp_path / "deadline"),
    }
    assert relay_module.main(environ) == 0
    assert bound == [("172.30.0.5", 8000)]


# --- Role routing: every provider role resolves to the relay ----------------


def _provider_contract() -> dict[str, str]:
    """``setup``'s contract.env: the template with every delta key replaced."""

    def parse(path: Path) -> dict[str, str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        return dict(
            line.split("=", 1) for line in lines if line and not line.startswith("#")
        )

    delta = {k: v.replace("@MODEL@", MODEL) for k, v in parse(PROVIDER_DELTA).items()}
    return {**parse(TEMPLATE), **delta}


def test_provider_contract_routes_every_endpoint_to_the_relay_alias():
    contract = _provider_contract()
    urls = {k: v for k, v in contract.items() if k.endswith("_BASE_URL")}
    assert set(urls) >= {
        "PLANNING_DIRECT_BASE_URL",
        "PLANNING_REPAIR_BASE_URL",
        "DEBUG_REPAIR_BASE_URL",
        "OPENAI_CHAT_COMPLETIONS_BASE_URL",
        "OPENAI_BASE_URL",
        "OLLAMA_BASE_URL",
    }
    assert all(v.startswith("http://model-provider:8000") for v in urls.values())
    assert {v for k, v in contract.items() if k.endswith("_BACKEND")} == {
        "openai_chat_completions"
    }
    models = {
        v
        for k, v in contract.items()
        if k.endswith("_MODEL") and k != "LOW_RESOURCE_SINGLE_MODEL"
    }
    assert models == {MODEL}
    assert not any(k.endswith("_API_KEY") for k in contract)
    assert contract["WORKSPACE_REVIEW_POLICY"] == "hold_all"
    assert contract["LOW_RESOURCE_SINGLE_MODEL"] == "false"
    assert disposable_c2_execution_mode(contract)
    validate_structured_http_provider_contract(
        provider_name=contract["PLANNING_PROVIDER"],
        backend_values={k: v for k, v in contract.items() if k.endswith("_BACKEND")},
    )
    # The provider-free template is unchanged: its roles stay unreachable.
    assert "model-provider:11434" in TEMPLATE.read_text(encoding="utf-8")


def test_every_role_resolves_to_the_relay_with_the_one_model(db_session, monkeypatch):
    from pydantic import TypeAdapter

    from app.services.agents.agent_runtime import (
        BackendRole,
        resolve_runtime_configuration,
        runtime_provider_role_matrix,
    )
    from app.services.agents.providers.openai_chat_adapter import (
        OpenAIChatCompletionsRuntime,
    )
    from app.services.planning.providers import direct_openai_compatible

    fields = type(settings).model_fields
    for name, value in _provider_contract().items():
        if name in fields:
            coerced = TypeAdapter(fields[name].annotation).validate_python(value)
            monkeypatch.setattr(settings, name, coerced)
    for name in (
        "OPENAI_API_KEY",
        "PLANNING_DIRECT_API_KEY",
        "AGENT_SECONDARY_BACKEND",
    ):
        monkeypatch.setattr(settings, name, "" if "KEY" in name else None)

    matrix = runtime_provider_role_matrix(db_session)
    assert set(matrix) == {r.value for r in BackendRole}
    for role in BackendRole:
        entry = matrix[role.value]
        assert entry.get("provider_ready") is True, entry
        assert entry["backend"] == "openai_chat_completions"
        assert entry["model"] == MODEL
        configuration = resolve_runtime_configuration(db_session, role)
        runtime = OpenAIChatCompletionsRuntime(
            db_session, None, runtime_configuration=configuration
        )
        assert runtime._base_url == RELAY_BASE
        assert runtime._invocation_base_url(object()) == RELAY_BASE
        assert runtime._api_key() == ""
    assert matrix["planning"]["endpoint"] == f"{RELAY_BASE}/chat/completions"
    assert matrix["debug_repair"]["endpoint"] == f"{RELAY_BASE}/chat/completions"
    # Planner direct/repair HTTP paths and the Planning provider use the same.
    assert settings.PLANNING_REPAIR_BASE_URL.rstrip("/") == RELAY_BASE
    assert direct_openai_compatible.settings.PLANNING_DIRECT_BASE_URL == RELAY_BASE


# --- Model submission: task text and baseline only --------------------------


def _model_mount(tmp_path: Path, monkeypatch) -> Path:
    material = c23b._stage(tmp_path, "model")
    monkeypatch.setattr(submitter, "U1_MATERIAL_ROOT", str(material))
    return material


def test_model_material_is_exactly_manifest_task_and_baseline(tmp_path):
    material = c23b._stage(tmp_path, "model")
    manifest = submitter.verify_u1_material(material, submitter.MODEL_PARTS)
    staged = {
        p.relative_to(material).as_posix() for p in material.rglob("*") if p.is_file()
    }
    u1 = submitter.U1_DIR
    assert staged == {f"{u1}/manifest.json", f"{u1}/task.txt"} | {
        f"{u1}/baseline/{rel}" for rel in manifest["baseline_files"]
    }
    # A mount that also carries reference material is refused, not filtered.
    reference = c23b._stage(tmp_path, "submit")
    with pytest.raises(submitter.SubmitterRefused, match="u1_material_mismatch"):
        submitter.verify_u1_material(reference, submitter.MODEL_PARTS)


def test_model_submission_carries_no_plan_or_evaluation_material(
    c2_env, monkeypatch, db_session_factory, tmp_path
):
    material = _model_mount(tmp_path, monkeypatch)
    evaluation = c23b._stage(tmp_path, "evaluation")
    queued = []

    def queue(db, session, task_id, timeout_seconds):
        execution = TaskExecution(
            task_id=task_id,
            session_id=session.id,
            attempt_number=1,
            status=TaskStatus.PENDING,
        )
        db.add(execution)
        db.commit()
        queued.append(timeout_seconds)
        return {"task_execution_id": execution.id, "status": "queued"}

    monkeypatch.setattr(
        "app.services.session.session_runtime_service.queue_task_for_session", queue
    )
    result = submitter.submit(db_session_factory, control="model")
    u1 = material / submitter.U1_DIR
    manifest = json.loads((u1 / "manifest.json").read_text())
    assert set(result["seeded_files"]) == set(manifest["baseline_files"])
    assert queued == [submitter.MODEL_TIMEOUT_SECONDS]
    db = db_session_factory()
    try:
        task = db.get(Task, result["task_id"])
        session = db.get(SessionModel, result["session_id"])
        assert task.steps is None  # no stored or preconstructed Plan
        assert task.description == (u1 / "task.txt").read_text(encoding="utf-8")
        assert task.execution_profile == "full_lifecycle"
        assert session.execution_mode == "manual"
        dump = "\n".join(db.connection().connection.iterdump())
    finally:
        db.close()
    # No evaluation-only line reaches the database (or, by construction, the
    # Worker's workspace, which holds exactly the seeded baseline).
    markers = _evaluation_only_lines(evaluation)
    assert markers and not [line for line in markers if line in dump]
    workspace_files = submitter._workspace_files(
        Path(c2_env["WORKSPACE_ROOT"]) / submitter.CONTROL_PROJECTS["model"]
    )
    assert workspace_files == manifest["baseline_files"]
    with pytest.raises(submitter.SubmitterRefused, match="control_already_submitted"):
        submitter.submit(db_session_factory, control="model")


def _evaluation_only_lines(evaluation: Path) -> list[str]:
    manifest = submitter.verify_u1_material(evaluation, submitter.EVALUATION_PARTS)
    visible = set(submitter._material_files(manifest, submitter.MODEL_PARTS))
    allowed = "\n".join((evaluation / rel).read_text() for rel in sorted(visible))
    lines = []
    for rel in sorted(
        set(submitter._material_files(manifest, ("reference", "naive", "hidden")))
        - visible
    ):
        for line in (evaluation / rel).read_text().splitlines():
            if len(line.strip()) >= 24 and line.strip() not in allowed:
                lines.append(line.strip())
    return lines


def test_model_observation_reports_every_execution(
    c2_env, monkeypatch, db_session_factory, tmp_path
):
    _model_mount(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.services.session.session_runtime_service.queue_task_for_session",
        lambda *a, **k: {"task_execution_id": None, "status": "queued"},
    )
    result = submitter.submit(db_session_factory, control="model")
    db = db_session_factory()
    try:
        for attempt, status in ((1, TaskStatus.FAILED), (2, TaskStatus.FAILED)):
            db.add(
                TaskExecution(
                    task_id=result["task_id"],
                    session_id=result["session_id"],
                    attempt_number=attempt,
                    status=status,
                )
            )
        db.commit()
    finally:
        db.close()
    observed = submitter.observe(
        db_session_factory, deadline_seconds=0, control="model"
    )
    assert observed["terminal"] is True
    assert observed["task_execution_count"] == 2  # a rerun is visible, not hidden
    assert observed["accepted_plan_sha256"] is None
    assert observed["expected_outcome_met"] is False


# --- Independent evaluation and contamination ------------------------------


def _model_observed(change_set_dir: Path, manifest: dict) -> dict:
    artifact = json.loads((change_set_dir / "manifest.json").read_text())
    baseline = set(manifest["baseline_files"])
    copied = artifact["copied_files"]
    return {
        "control": "model",
        "expected_outcome_met": True,
        "workspace_files": dict(manifest["baseline_files"]),
        "change_set": {
            "added_files": [r for r in copied if r not in baseline],
            "modified_files": [r for r in copied if r in baseline],
            "deleted_files": [],
        },
    }


def _reference_as_model_change_set(evaluation: Path, tmp_path: Path) -> Path:
    source = evaluation / submitter.U1_DIR / "controls" / "reference"
    target = tmp_path / "model-change-set"
    shutil.copytree(source, target)
    return target


def test_model_verdict_judges_the_actual_change_set(tmp_path):
    evaluation = c23b._stage(tmp_path, "evaluation")
    manifest = submitter.verify_u1_material(evaluation, submitter.EVALUATION_PARTS)
    change_set = _reference_as_model_change_set(evaluation, tmp_path)
    observed = _model_observed(change_set, manifest)
    passing = {"complete_delivery": True, "functional_pass": True}
    verdict = submitter.evaluation_verdict(
        "model", evaluation, change_set, passing, {"hidden_pass": True}, observed
    )
    # Matching the reference is a complete delivery, never leakage by itself.
    assert verdict["verdict"] == "COMPLETE_DELIVERY"
    assert verdict["integrity"]["ok"]
    incomplete = submitter.evaluation_verdict(
        "model", evaluation, change_set, passing, {"hidden_pass": False}, observed
    )
    assert incomplete["verdict"] == "INCOMPLETE_DELIVERY"
    with pytest.raises(submitter.SubmitterRefused, match="model_observation_required"):
        submitter.evaluation_verdict("model", evaluation, change_set, passing, {}, None)


@pytest.mark.parametrize(
    ("rel", "reason"),
    [
        ("tests/conftest.py", "test_environment_modification"),
        ("pytest.ini", "test_environment_modification"),
        ("rangekit/sitecustomize.py", "test_environment_modification"),
        ("rangekit/zz.pth", "test_environment_modification"),
        ("tests/test_rangekit_acceptance.py", "acceptance_modified"),
        ("notes/extra.txt", "unauthorized_addition"),
    ],
)
def test_model_verdict_fails_integrity_for_test_environment_manipulation(
    tmp_path, rel, reason
):
    evaluation = c23b._stage(tmp_path, "evaluation")
    manifest = submitter.verify_u1_material(evaluation, submitter.EVALUATION_PARTS)
    change_set = _reference_as_model_change_set(evaluation, tmp_path)
    artifact = json.loads((change_set / "manifest.json").read_text())
    (change_set / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
    (change_set / "files" / rel).write_text("import os\n")
    artifact["copied_files"] = sorted({*artifact["copied_files"], rel})
    (change_set / "manifest.json").write_text(json.dumps(artifact))
    verdict = submitter.evaluation_verdict(
        "model",
        evaluation,
        change_set,
        {"complete_delivery": True},
        {"hidden_pass": True},
        _model_observed(change_set, manifest),
    )
    assert verdict["verdict"] == "INTEGRITY_FAILED"
    assert any(r.startswith(reason) for r in verdict["integrity"]["reasons"])


def _relay_bodies(tmp_path: Path, exchanges: list[tuple[str, str | None]]) -> Path:
    state = tmp_path / "relay"
    bodies = state / "bodies"
    bodies.mkdir(parents=True)
    for index, (request, response) in enumerate(exchanges, start=1):
        payload = {"model": MODEL, "messages": [{"role": "user", "content": request}]}
        (bodies / f"{index:04d}.request.json").write_text(json.dumps(payload))
        if response is not None:
            answer = {"choices": [{"message": {"content": response}}]}
            (bodies / f"{index:04d}.response.json").write_text(json.dumps(answer))
    return state


def test_contamination_scan_distinguishes_leakage_from_model_output(tmp_path):
    evaluation = c23b._stage(tmp_path, "evaluation")
    lines = _evaluation_only_lines(evaluation)
    hidden_line = lines[-1]
    task = (evaluation / submitter.U1_DIR / "task.txt").read_text()

    clean = submitter.provider_request_contamination(
        evaluation, _relay_bodies(tmp_path / "clean", [(task, "patch")])
    )
    assert clean["classification"] == "NO_EVALUATION_MATERIAL_IN_REQUESTS"
    assert clean["requests_scanned"] == 1 and clean["marker_lines"] > 0

    leaked = submitter.provider_request_contamination(
        evaluation, _relay_bodies(tmp_path / "leaked", [(task + hidden_line, None)])
    )
    assert leaked["classification"] == "REVIEW_REQUIRED"
    assert leaked["unexplained_hits"] >= 1
    assert hidden_line not in json.dumps(leaked)  # digests only

    produced = submitter.provider_request_contamination(
        evaluation,
        _relay_bodies(
            tmp_path / "produced", [(task, hidden_line), (task + hidden_line, None)]
        ),
    )
    assert produced["classification"] == "NO_EVALUATION_MATERIAL_IN_REQUESTS"
    assert produced["hits"] and all(
        hit["explained_by_prior_response"] for hit in produced["hits"]
    )


# --- Runner: provider mode, one approval, provider-free unchanged -----------


def test_runner_provider_inputs_and_mode_are_explicit(tmp_path):
    functions = "".join(
        c23b._shell_function(LIVE_RUN, name)
        for name in (
            "provider_mode_requested",
            "require_provider_inputs",
            "provider_mode",
        )
    )
    prelude = (
        f"ROOT={tmp_path}\nNET=p43-c2-net\nPROVIDER_DELTA={PROVIDER_DELTA}\n"
        'refuse() { echo "refused: $*"; exit 78; }\n' + functions
    )
    good = (
        "export P43_C2_PROVIDER_UPSTREAM=http://ai-gateway:8000 "
        "P43_C2_PROVIDER_MODEL=qwen-local P43_C2_PROVIDER_NETWORK=ai-net\n"
    )

    def run(body: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + prelude + body],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    assert run(good + "require_provider_inputs; echo OK").stdout.strip() == "OK"
    for override in (
        "P43_C2_PROVIDER_UPSTREAM=http://ai-gateway:8000/v1",
        "P43_C2_PROVIDER_UPSTREAM=http://u:p@ai-gateway:8000",
        "P43_C2_PROVIDER_UPSTREAM=https://ai-gateway:8000",
        "P43_C2_PROVIDER_MODEL='qwen local'",
        "P43_C2_PROVIDER_NETWORK=p43-c2-net",
        "P43_C2_PROVIDER_NETWORK=",
    ):
        out = run(good + f"export {override}\nrequire_provider_inputs")
        assert out.returncode == 78, override
    # Provider mode is what setup recorded, never the later environment.
    assert run(good + "provider_mode && echo yes || echo no").stdout.strip() == "no"
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "run.vars").write_text("PROVIDER_MODE=1\n")
    assert run("provider_mode && echo yes || echo no").stdout.strip() == "yes"


def test_runner_relay_and_worker_launch_contracts():
    text = LIVE_RUN.read_text(encoding="utf-8")
    relay = c23b._shell_function(LIVE_RUN, "start_relay")
    for flag in (
        '--network "$NET" --network-alias model-provider',
        '--user "$RELAY_UID:$RELAY_UID" --read-only --cap-drop=ALL',
        "--security-opt=no-new-privileges:true",
        "--restart=no",
        '-v "$ROOT/relay-ctl:/run/p43-relay-ctl:ro"',
        '--entrypoint python3 "$IMAGE_TAG" -I "$RELAY_SCRIPT"',
        'docker network connect "$P43_C2_PROVIDER_NETWORK" "$RELAY"',
    ):
        assert flag in relay, flag
    for forbidden in ("docker.sock", "C2_WORKER_KEY", "C2_RUN", "u1-", "secrets.env"):
        assert forbidden not in relay, forbidden
    # The Worker keeps exactly its internal network; only the relay connects out.
    assert text.count('docker network connect "$P43_C2_PROVIDER_NETWORK"') == 1
    assert 'docker run -d --name "$WORKER" --network "$NET" \\' in text
    submit = c23b._shell_function(LIVE_RUN, "cmd_submit")
    assert (
        '[[ $control == model ]] || refuse "provider mode admits only the model control"'
        in submit
    )
    assert "the model control requires provider mode" in submit
    assert "set -o noclobber" in submit and "MODEL_WINDOW_SECONDS" in submit
    window = c23b._shell_function(LIVE_RUN, "cmd_run_model_window")
    order = [
        window.index(stage)
        for stage in (
            "trap model_window_interrupted INT TERM HUP",
            "setup --operator-approved",
            '"${WINDOW_SELF[@]}" boundary',
            "submit --operator-approved model",
            "evaluate model",
            "model_window_finish",
        )
    ]
    assert order == sorted(order)
    assert window.count("submit --operator-approved model") == 1
    finish = c23b._shell_function(LIVE_RUN, "model_window_finish")
    assert "EVIDENCE_FAILED_STATE_RETAINED" in finish
    assert finish.index('"${WINDOW_SELF[@]}" evidence') < finish.index(
        '"${WINDOW_SELF[@]}" cleanup --operator-approved'
    )
    observe = c23b._shell_function(LIVE_RUN, "observe_model")
    assert "observation_failed" in observe
    assert observe.count('docker stop --time 30 "$WORKER"') == 2
    cleanup = c23b._shell_function(LIVE_RUN, "cmd_cleanup")
    assert '"$WORKER" "$RELAY"' in cleanup


_WINDOW_STUB = r"""#!/bin/bash
echo "$1" >>"$LOG"
case "$1" in
setup) mkdir -p "$ROOT/env"; [[ $FAIL != setup ]] ;;
boundary) [[ $FAIL != boundary ]] ;;
submit)
    date >"$ROOT/env/submitted"
    if [[ $FAIL == interrupt ]]; then kill -INT "$PPID"; sleep 0.3; exit 130; fi
    [[ $FAIL == submit ]] && exit 1
    echo terminal >"$ROOT/env/model-window.outcome" ;;
evaluate) exit 0 ;;
evidence) [[ $FAIL != evidence ]] ;;
cleanup) exit 0 ;;
esac
"""


def _run_window(tmp_path: Path, fail: str) -> tuple[int, str, list[str], Path]:
    repo, root = tmp_path / "release", tmp_path / "srv"
    (repo / "scripts" / "maintenance").mkdir(parents=True)
    (repo / "scripts" / "maintenance" / "p43_c2_live_run.sh").write_text(_WINDOW_STUB)
    log, evidence = tmp_path / "stages.log", tmp_path / "evidence"
    functions = "".join(
        c23b._shell_function(LIVE_RUN, name)
        for name in (
            "cmd_run_model_window",
            "model_window_interrupted",
            "model_window_finish",
            "model_window_presubmission_capture",
        )
    )
    prelude = (
        f"REPO={repo}\nROOT={root}\nEVIDENCE={evidence}\n"
        "WORKER=p43-c2-worker\nRELAY=p43-c2-relay\n"
        + next(
            line + "\n"
            for line in LIVE_RUN.read_text(encoding="utf-8").splitlines()
            if line.startswith("need_approval() {")
        )
        + 'info() { echo "INFO $*"; }\nrefuse() { echo "refused: $*"; exit 78; }\n'
        "provider_mode_requested() { true; }\nrequire_provider_inputs() { :; }\n"
        'docker() { echo "docker $1" >>"$LOG"; }\n'
        'observe_once() { echo "observe $(basename "$1") $2" >>"$LOG"; echo {} >"$1"; }\n'
    )
    out = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            + prelude
            + functions
            + "cmd_run_model_window --operator-approved",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LOG": str(log),
            "ROOT": str(root),
            "FAIL": fail,
        },
    )
    stages = log.read_text().split("\n")[:-1] if log.exists() else []
    return out.returncode, out.stdout, stages, root


@pytest.mark.parametrize(
    "fail, rc, status, stages, outcome",
    [
        (
            "none",
            0,
            "COMPLETE",
            ["setup", "boundary", "submit", "evaluate", "docker stop", "evidence"],
            "terminal",
        ),
        ("setup", 1, "SETUP_FAILED", ["setup", "docker stop", "docker logs"], None),
        (
            "boundary",
            1,
            "BOUNDARY_FAILED",
            ["setup", "boundary", "docker stop", "docker logs"],
            None,
        ),
        (
            "submit",
            1,
            "COMPLETE_WITH_FAILURES",
            [
                "setup",
                "boundary",
                "submit",
                "evaluate",
                "docker stop",
                "observe observe.model.after-stop.json 0",
                "evidence",
            ],
            "submission_incomplete",
        ),
        (
            "interrupt",
            130,
            "INTERRUPTED",
            [
                "setup",
                "boundary",
                "submit",
                "docker stop",
                "observe observe.model.after-stop.json 0",
                "evidence",
            ],
            "interrupted",
        ),
    ],
)
def test_model_window_collects_evidence_then_cleans_up_on_every_path(
    tmp_path, fail, rc, status, stages, outcome
):
    code, stdout, observed, root = _run_window(tmp_path, fail)
    assert code == rc, stdout
    assert stdout.strip().splitlines()[-1] == f"C2_MODEL_WINDOW={status}"
    sequence = [s for s in observed if s != "docker logs"]
    assert sequence == [s for s in stages if s != "docker logs"] + ["cleanup"]
    assert observed.count("submit") <= 1
    if "submit" not in stages:
        assert (tmp_path / "evidence").is_dir() and "evidence" not in observed
    if outcome:
        assert (root / "env" / "model-window.outcome").read_text().strip() == outcome


def test_model_window_retains_state_when_evidence_fails(tmp_path):
    code, stdout, observed, _ = _run_window(tmp_path, "evidence")
    assert code == 1
    assert stdout.strip().splitlines()[-1] == (
        "C2_MODEL_WINDOW=EVIDENCE_FAILED_STATE_RETAINED"
    )
    assert observed[-1] == "evidence" and "cleanup" not in observed
    assert "docker stop" in observed


def test_runner_shell_syntax():
    out = subprocess.run(["bash", "-n", str(LIVE_RUN)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
