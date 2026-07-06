#!/usr/bin/env python3
"""
planner_relay.py — Stateless Planner Relay

Reads relay/input.md, pastes it into the Planner (currently: ChatGPT browser),
saves the response to relay/output.md, appends a log entry to relay/relay.log.

The wrapper script (run_planner_relay.sh) handles the workflow file contract.
This script only knows the relay/ files and the browser-session CDP endpoint.

Usage:
    python3 scripts/relay/planner_relay.py [--resume] [--verbose]
"""

import argparse
import atexit
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────

RELAY_DIR = Path(os.environ.get("RELAY_DIR", "./relay"))
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
EXPECTED_CONVERSATION_URL = os.environ.get(
    "RELAY_EXPECTED_CONVERSATION_URL", ""
).strip()

SELECTORS_FILE = Path(__file__).parent / "selectors.yaml"

INPUT_FILE = RELAY_DIR / "input.md"
OUTPUT_FILE = RELAY_DIR / "output.md"
LOG_FILE = RELAY_DIR / "relay.log"
METRICS_FILE = RELAY_DIR / "metrics.jsonl"
LOCK_FILE = RELAY_DIR / "relay.lock"
STATE_FILE = RELAY_DIR / "state.json"
SNAPSHOT_FILE = RELAY_DIR / "session_snapshot.json"

LOCK_STALE_AFTER_S = int(os.environ.get("RELAY_LOCK_STALE_AFTER_S", "21600"))
RESPONSE_TIMEOUT_S = int(os.environ.get("RELAY_RESPONSE_TIMEOUT_S", "120"))

FAILURE_CATEGORIES = {
    "browser_unavailable",
    "login_expired",
    "url_mismatch",
    "selector_failure",
    "operator_cancelled",
    "relay_cancelled",
    "response_timeout",
    "browser_reload",
    "conversation_changed",
    "unexpected_error",
}

LOCK_HELD = False
VERBOSE = False


# ── Helpers ───────────────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_selectors() -> dict:
    with open(SELECTORS_FILE, "r") as f:
        return yaml.safe_load(f)


def log(msg: str) -> None:
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{utc_now()}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def log_exception(prefix: str, exc: Exception) -> None:
    if VERBOSE:
        log(f"{prefix}: {exc!r}")
    else:
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else str(exc)
        log(f"{prefix}: {message}")


def record_metric(event: str, **fields) -> None:
    """Append one JSON line to relay/metrics.jsonl. Append-only, no database."""
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": utc_now(),
        "event": event,
        **fields,
    }
    with open(METRICS_FILE, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def fail(
    started: float,
    category: str,
    message: str,
    *,
    emit_metric: bool = True,
    **fields,
) -> None:
    if category not in FAILURE_CATEGORIES:
        category = "unexpected_error"
    log(f"FAILURE_CLASSIFICATION: {category}")
    log(message)
    if emit_metric:
        record_metric(
            "relay_completed",
            success=False,
            failure_reason=category,
            failure_category=category,
            elapsed_s=round(time.time() - started, 1),
            **fields,
        )


def read_input() -> str:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"relay/input.md not found at {INPUT_FILE}")
    content = INPUT_FILE.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("relay/input.md is empty")
    return content


def write_output(content: str) -> None:
    OUTPUT_FILE.write_text(content, encoding="utf-8")


def normalize_url(url: str) -> str:
    return url.rstrip("/")


def conversation_url_matches(current_url: str, expected_url: str) -> bool:
    return normalize_url(current_url) == normalize_url(expected_url)


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def acquire_lock(started: float) -> bool:
    global LOCK_HELD

    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "created_at": utc_now(),
        "created_at_epoch": time.time(),
    }

    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = read_json(LOCK_FILE)
            existing_pid = int(existing.get("pid") or 0)
            existing_ts = existing.get("created_at", "unknown")
            existing_epoch = float(existing.get("created_at_epoch") or 0)
            age = time.time() - existing_epoch if existing_epoch else None
            stale = not pid_exists(existing_pid)
            if age is not None and age > LOCK_STALE_AFTER_S:
                stale = True

            if stale:
                log(
                    "Stale relay lock detected; removing "
                    f"pid={existing_pid or 'unknown'} timestamp={existing_ts}"
                )
                LOCK_FILE.unlink(missing_ok=True)
                record_metric(
                    "relay_lock_stale_removed",
                    pid=existing_pid or None,
                    created_at=existing_ts,
                )
                continue

            fail(
                started,
                "unexpected_error",
                "Relay already running; refusing second relay instance. "
                f"Existing PID: {existing_pid or 'unknown'}, "
                f"timestamp: {existing_ts}",
                lock_pid=existing_pid or None,
                lock_created_at=existing_ts,
            )
            return False

        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
        LOCK_HELD = True
        log(f"Relay lock acquired: {LOCK_FILE} pid={payload['pid']}")
        return True


def release_lock() -> None:
    global LOCK_HELD
    if not LOCK_HELD:
        return
    try:
        current = read_json(LOCK_FILE)
        if int(current.get("pid") or 0) == os.getpid():
            LOCK_FILE.unlink(missing_ok=True)
            log(f"Relay lock released: {LOCK_FILE}")
    finally:
        LOCK_HELD = False


def install_signal_handlers(started: float) -> None:
    def handle_signal(signum, _frame) -> None:
        fail(
            started,
            "relay_cancelled",
            f"Relay cancelled by signal {signum}.",
            signal=signum,
        )
        release_lock()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def update_state(phase: str, **fields) -> None:
    payload = {
        "phase": phase,
        "pid": os.getpid(),
        "updated_at": utc_now(),
        **fields,
    }
    write_json(STATE_FILE, payload)
    log(f"Resume state updated: phase={phase}")
    record_metric("relay_state_updated", phase=phase)


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        log("Resume state cleared.")


def load_state() -> dict:
    state = read_json(STATE_FILE)
    if not state:
        raise FileNotFoundError(f"resume state not found at {STATE_FILE}")
    return state


# ── Browser helpers ───────────────────────────────────────────────────────────


def capture_snapshot(page, sel: dict, label: str) -> dict:
    response_selector = sel.get("response", "")
    try:
        assistant_count = len(page.query_selector_all(response_selector))
    except Exception:
        assistant_count = None
    try:
        title = page.title()
    except Exception:
        title = None
    snapshot = {
        "label": label,
        "captured_at": utc_now(),
        "url": page.url,
        "title": title,
        "assistant_message_count": assistant_count,
    }
    log(
        "Snapshot "
        f"{label}: url={snapshot['url']} title={title!r} "
        f"assistant_messages={assistant_count}"
    )
    return snapshot


def diff_snapshots(before: dict, after: dict) -> list[dict]:
    differences = []
    for key in ("url", "title", "assistant_message_count"):
        if before.get(key) != after.get(key):
            differences.append(
                {"field": key, "before": before.get(key), "after": after.get(key)}
            )
    return differences


def write_session_snapshot(label: str, snapshot: dict) -> dict:
    existing = read_json(SNAPSHOT_FILE)
    existing[label] = snapshot
    if "before_send" in existing and "after_response" in existing:
        existing["differences"] = diff_snapshots(
            existing["before_send"], existing["after_response"]
        )
    write_json(SNAPSHOT_FILE, existing)
    return existing


def report_snapshot_differences(snapshots: dict) -> None:
    differences = snapshots.get("differences") or []
    if not differences:
        log("Snapshot comparison: no differences detected.")
        return
    log("Snapshot comparison: differences detected.")
    for diff in differences:
        log(
            "  "
            f"{diff['field']}: before={diff['before']!r} "
            f"after={diff['after']!r}"
        )


def diagnose_selector(page, name: str, selector: str, *, required: bool = True) -> dict:
    try:
        matches = page.query_selector_all(selector)
    except Exception as exc:
        return {"name": name, "ok": False, "reason": str(exc)}
    if matches or not required:
        return {"name": name, "ok": True, "matches": len(matches)}
    return {"name": name, "ok": False, "reason": "selector not found"}


def print_selector_diagnostics(
    diagnostics: list[dict], *, failure_name: str | None = None
) -> None:
    print()
    print("Selector diagnostics:")
    for item in diagnostics:
        status = "PASS" if item["ok"] else "FAIL"
        print()
        print(f"{item['name']}:")
        print(status)
        if not item["ok"]:
            print()
            print("Reason:")
            print(item.get("reason", "selector not found"))
    print()
    for item in diagnostics:
        log(
            "Selector diagnostic: "
            f"{item['name']} {'PASS' if item['ok'] else 'FAIL'} "
            f"{item.get('reason', 'matches=' + str(item.get('matches', 0)))}"
        )
    if failure_name:
        record_metric("selector_diagnostics", failed_selector=failure_name)


def selector_diagnostics(page, sel: dict, *, include_streaming: bool) -> list[dict]:
    return [
        diagnose_selector(page, "Input selector", sel["input"]),
        diagnose_selector(page, "Send button", sel["send_button"]),
        diagnose_selector(
            page,
            "Streaming indicator",
            sel["streaming_indicator"],
            required=include_streaming,
        ),
        diagnose_selector(page, "Assistant response", sel["response"]),
    ]


def wait_for_response(page, sel: dict, timeout_s: int = RESPONSE_TIMEOUT_S) -> str:
    log("Waiting for streaming to start...")
    try:
        page.wait_for_selector(sel["streaming_indicator"], timeout=30_000)
        log("Streaming started.")
        update_state("waiting_response", url=page.url)
    except PlaywrightTimeoutError:
        log("Warning: streaming indicator not detected; proceeding anyway.")

    log("Waiting for streaming to finish...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not page.query_selector(sel["streaming_indicator"]):
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"Planner did not finish within {timeout_s}s")

    log("Streaming finished.")
    update_state("extracting_response", url=page.url)
    time.sleep(2)

    messages = page.query_selector_all(sel["response"])
    if not messages:
        raise RuntimeError("No response found in DOM")
    return messages[-1].inner_text()


def connect_page(playwright, started: float):
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        log_exception(f"ERROR: Browser unavailable at {CDP_URL}", exc)
        fail(started, "browser_unavailable", f"Browser unavailable at {CDP_URL}.")
        return None, None

    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return browser, page


def prepare_page(page, sel: dict, started: float) -> bool:
    log(f"Current page: {page.url}")

    if not page.url.startswith(sel["target_url"].rstrip("/")):
        log(f"Navigating to {sel['target_url']}")
        page.goto(sel["target_url"], wait_until="networkidle", timeout=30_000)
        time.sleep(3)

    if any(pat in page.url for pat in sel["login_url_patterns"]):
        fail(
            started,
            "login_expired",
            "Not logged in. Open http://localhost:6080, log in manually, then re-run.",
        )
        return False

    if EXPECTED_CONVERSATION_URL:
        if not conversation_url_matches(page.url, EXPECTED_CONVERSATION_URL):
            fail(
                started,
                "url_mismatch",
                "Conversation URL mismatch. Relay aborted. "
                f"Expected: {EXPECTED_CONVERSATION_URL} Actual: {page.url}. "
                "Open the expected ChatGPT conversation tab yourself; the relay "
                "does not switch tabs automatically.",
                expected_url=EXPECTED_CONVERSATION_URL,
                actual_url=page.url,
            )
            return False
        log(f"Conversation URL pinned and verified: {page.url}")
    return True


def paste_input(page, sel: dict, content: str, started: float) -> bool:
    log("Waiting for input box...")
    try:
        page.wait_for_selector(sel["input"], timeout=15_000)
    except Exception as exc:
        log_exception("ERROR: Input selector not found", exc)
        diagnostics = selector_diagnostics(page, sel, include_streaming=False)
        print_selector_diagnostics(diagnostics, failure_name="input")
        fail(
            started,
            "selector_failure",
            "Input selector failed. See selector diagnostics above.",
            selector="input",
        )
        return False

    box = page.locator(sel["input"])
    box.click()
    box.fill("")
    box.fill(content)
    log("Input pasted into Planner.")
    return True


def approve_send() -> bool:
    print()
    print("=" * 60)
    print("PLANNER RELAY: Ready to send relay/input.md to ChatGPT.")
    print("Preview at: http://localhost:6080")
    print("=" * 60)
    return input("Send? [y/N] ").strip().lower() == "y"


def approve_extract() -> bool:
    print()
    print("=" * 60)
    print("PLANNER RELAY: Resume mode will not send automatically.")
    print("It will only wait for/extract the current assistant response.")
    print("Preview at: http://localhost:6080")
    print("=" * 60)
    return input("Continue extraction? [y/N] ").strip().lower() == "y"


def classify_snapshot_change(snapshots: dict) -> str | None:
    before = snapshots.get("before_send") or {}
    after = snapshots.get("after_response") or {}
    if not before or not after:
        return None
    before_url = before.get("url")
    after_url = after.get("url")
    if (
        before_url
        and after_url
        and normalize_url(before_url) != normalize_url(after_url)
    ):
        if "chatgpt.com" not in after_url:
            return "browser_reload"
        return "conversation_changed"
    return None


def finish_with_response(page, sel: dict, response: str, started: float) -> None:
    elapsed = round(time.time() - started, 1)
    write_output(response)
    log(f"Output: {len(response)} chars written to relay/output.md")
    after = capture_snapshot(page, sel, "after_response")
    snapshots = write_session_snapshot("after_response", after)
    report_snapshot_differences(snapshots)
    snapshot_failure = classify_snapshot_change(snapshots)
    if snapshot_failure:
        fail(
            started,
            snapshot_failure,
            "Snapshot comparison detected browser reload or conversation replacement.",
            emit_metric=False,
        )
        record_metric(
            "relay_snapshot_warning",
            failure_reason=snapshot_failure,
            failure_category=snapshot_failure,
        )
    log(f"Elapsed: {elapsed}s")
    clear_state()
    record_metric("relay_completed", success=True, elapsed_s=elapsed)


# ── Relay Core ────────────────────────────────────────────────────────────────


def run_relay(*, resume: bool = False) -> int:
    sel = load_selectors()
    started = time.time()
    categories = sorted(FAILURE_CATEGORIES)
    log("Supported failure classifications: " + ", ".join(categories))
    record_metric("relay_start", resume=resume, failure_categories=categories)
    install_signal_handlers(started)
    atexit.register(release_lock)

    if not acquire_lock(started):
        return 1

    try:
        if resume:
            try:
                state = load_state()
            except FileNotFoundError as exc:
                fail(started, "unexpected_error", f"Resume requested but {exc}.")
                return 1
            phase = state.get("phase")
            log(
                f"Resuming relay from phase={phase} updated_at={state.get('updated_at')}"
            )
        else:
            phase = "start"
            state = {}

        if phase in {"start", "waiting_send"}:
            try:
                content = read_input()
            except (FileNotFoundError, ValueError) as exc:
                fail(started, "unexpected_error", f"Input error: {exc}")
                return 1
            log(f"Input: {len(content)} chars from relay/input.md")
        else:
            content = ""

        log(f"Connecting to Chromium via CDP at {CDP_URL}")
        with sync_playwright() as p:
            browser, page = connect_page(p, started)
            if browser is None or page is None:
                return 1
            try:
                if not prepare_page(page, sel, started):
                    return 1

                if phase in {"waiting_response", "extracting_response"}:
                    update_state("extracting_response", url=page.url)
                    if not approve_extract():
                        fail(started, "operator_cancelled", "Cancelled by user.")
                        return 1
                    try:
                        response = wait_for_response(page, sel)
                    except TimeoutError as exc:
                        fail(started, "response_timeout", f"ERROR: {exc}")
                        return 1
                    except RuntimeError as exc:
                        diagnostics = selector_diagnostics(
                            page, sel, include_streaming=False
                        )
                        print_selector_diagnostics(diagnostics, failure_name="response")
                        fail(
                            started,
                            "selector_failure",
                            f"ERROR: {exc}",
                            selector="response",
                        )
                        return 1
                    finish_with_response(page, sel, response, started)
                    return 0

                update_state("waiting_send", url=page.url)
                if not paste_input(page, sel, content, started):
                    return 1

                before = capture_snapshot(page, sel, "before_send")
                write_session_snapshot("before_send", before)
                update_state("waiting_send", url=page.url, input_chars=len(content))

                if not approve_send():
                    fail(started, "operator_cancelled", "Cancelled by user.")
                    return 1

                log("Sending...")
                try:
                    page.click(sel["send_button"])
                except Exception as exc:
                    log_exception("ERROR: Send button click failed", exc)
                    diagnostics = selector_diagnostics(
                        page, sel, include_streaming=False
                    )
                    print_selector_diagnostics(diagnostics, failure_name="send_button")
                    fail(
                        started,
                        "selector_failure",
                        "Send button selector failed. See selector diagnostics above.",
                        selector="send_button",
                    )
                    return 1

                update_state("waiting_response", url=page.url)

                try:
                    response = wait_for_response(page, sel)
                except TimeoutError as exc:
                    fail(started, "response_timeout", f"ERROR: {exc}")
                    return 1
                except RuntimeError as exc:
                    diagnostics = selector_diagnostics(
                        page, sel, include_streaming=False
                    )
                    print_selector_diagnostics(diagnostics, failure_name="response")
                    fail(
                        started,
                        "selector_failure",
                        f"ERROR: {exc}",
                        selector="response",
                    )
                    return 1

                finish_with_response(page, sel, response, started)
                return 0
            finally:
                browser.close()
    except KeyboardInterrupt:
        return 130
    except PlaywrightError as exc:
        log_exception("ERROR: Playwright failure", exc)
        fail(started, "unexpected_error", "Unexpected Playwright failure.")
        return 1
    except Exception as exc:
        log_exception("ERROR: Unexpected relay failure", exc)
        fail(started, "unexpected_error", "Unexpected relay failure.")
        return 1
    finally:
        release_lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Planner Relay.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume from relay/state.json without automatic Send",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include exception representations in relay.log diagnostics",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    VERBOSE = args.verbose
    exit_code = run_relay(resume=args.resume)
    if exit_code == 0:
        print()
        print("=" * 60)
        print("RELAY COMPLETE")
        print(f"Response saved to: {OUTPUT_FILE}")
        print("=" * 60)
    sys.exit(exit_code)
