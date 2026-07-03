#!/usr/bin/env python3
"""
planner_relay.py — Stateless Planner Relay

Reads relay/input.md, pastes it into the Planner (currently: ChatGPT browser),
saves the response to relay/output.md, appends a log entry to relay/relay.log.

This script knows NOTHING about:
  - HANDOFF_DRAFT.md
  - NEXT_PROMPT.md
  - Orchestrator
  - workflow phase names

It only knows:
  - relay/input.md  → read
  - relay/output.md → write
  - relay/relay.log → append

The wrapper script (run_planner_relay.sh) handles the file renaming.

Usage:
    python3 scripts/relay/planner_relay.py

Environment:
    RELAY_DIR       path to the relay/ folder (default: ./relay)
    CDP_URL         Chrome DevTools Protocol URL (default: http://localhost:9222)
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────

RELAY_DIR = Path(os.environ.get("RELAY_DIR", "./relay"))
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")

SELECTORS_FILE = Path(__file__).parent / "selectors.yaml"

INPUT_FILE = RELAY_DIR / "input.md"
OUTPUT_FILE = RELAY_DIR / "output.md"
LOG_FILE = RELAY_DIR / "relay.log"

# ── Helpers ───────────────────────────────────────────────────────────────────


def load_selectors() -> dict:
    with open(SELECTORS_FILE, "r") as f:
        return yaml.safe_load(f)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def read_input() -> str:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"relay/input.md not found at {INPUT_FILE}")
    content = INPUT_FILE.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("relay/input.md is empty")
    return content


def write_output(content: str) -> None:
    OUTPUT_FILE.write_text(content, encoding="utf-8")


# ── Relay Core ────────────────────────────────────────────────────────────────


def wait_for_response(page, sel: dict, timeout_s: int = 120) -> str:
    log("Waiting for streaming to start...")
    try:
        page.wait_for_selector(sel["streaming_indicator"], timeout=30_000)
        log("Streaming started.")
    except Exception:
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
    time.sleep(2)  # allow final DOM update

    messages = page.query_selector_all(sel["response"])
    if not messages:
        raise RuntimeError("No response found in DOM")
    return messages[-1].inner_text()


def run_relay() -> None:
    sel = load_selectors()
    content = read_input()
    started = time.time()

    log(f"Input: {len(content)} chars from relay/input.md")
    log(f"Connecting to Chromium via CDP at {CDP_URL}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        log(f"Current page: {page.url}")

        # Navigate to Planner if needed
        if not page.url.startswith(sel["target_url"].rstrip("/")):
            log(f"Navigating to {sel['target_url']}")
            page.goto(sel["target_url"], wait_until="networkidle", timeout=30_000)
            time.sleep(3)

        # Login check
        if any(pat in page.url for pat in sel["login_url_patterns"]):
            log("ERROR: Not logged in.")
            log("Open http://localhost:6080, log in manually, then re-run.")
            browser.close()
            return

        # Paste input
        log("Waiting for input box...")
        page.wait_for_selector(sel["input"], timeout=15_000)
        box = page.locator(sel["input"])
        box.click()
        box.fill("")
        box.fill(content)
        log("Input pasted into Planner.")

        # Human confirmation — always kept
        print()
        print("=" * 60)
        print("PLANNER RELAY: Ready to send relay/input.md to ChatGPT.")
        print("Preview at: http://localhost:6080")
        print("=" * 60)
        confirm = input("Send? [y/N] ").strip().lower()
        if confirm != "y":
            log("Cancelled by user.")
            browser.close()
            return

        log("Sending...")
        page.click(sel["send_button"])

        response = wait_for_response(page, sel)
        elapsed = round(time.time() - started, 1)

        write_output(response)
        log(f"Output: {len(response)} chars written to relay/output.md")
        log(f"Elapsed: {elapsed}s")

        browser.close()

    print()
    print("=" * 60)
    print("RELAY COMPLETE")
    print(f"Response saved to: {OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    run_relay()
