---
type: failure_memory
title: Backend Request Timeout
applies_to: [failure]
tags: [backend, timeout, http, fastapi]
priority: 9
---

The backend HTTP request exceeded its timeout. The orchestrator did not receive a response from the FastAPI backend within the allowed window.

Error patterns that match this failure:
- "backend timeout"
- "Request timed out"
- "httpx.TimeoutException"
- "ReadTimeout: backend"
- "connection timeout to backend"

Root cause: The backend (uvicorn/FastAPI) is overloaded, restarting, or the database query took too long. May also occur during heavy migration or bulk operations.

Recommended action: review_failure. Verify the backend is running on the expected port. Check backend logs for slow queries or startup failures. If the backend is healthy, retry once. If timeouts persist, the task may be issuing requests that are too expensive.
