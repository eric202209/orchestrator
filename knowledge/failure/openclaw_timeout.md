---
type: failure_memory
title: OpenClaw Gateway Timeout
applies_to: [failure]
tags: [openclaw, timeout, gateway, llm]
priority: 10
---

OpenClaw gateway did not respond within the timeout window. The LLM request was abandoned.

Error patterns that match this failure:
- "OpenClaw timeout"
- "gateway timeout"
- "ReadTimeout"
- "httpx.ReadTimeout"
- "timed out waiting for openclaw"
- "port 18789 timeout"

Root cause: The OpenClaw gateway (port 18789) is overloaded, not running, or the request took longer than the configured timeout. May also occur when the underlying LLM model is slow to respond.

Recommended action: review_failure. Check that the OpenClaw process is running and port 18789 is accessible. If the task prompt is too large, break it into smaller steps. Retry once; if it times out again, stop and escalate.
