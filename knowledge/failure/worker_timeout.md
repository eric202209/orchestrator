---
type: failure_memory
title: Celery Worker Timeout
applies_to: [failure]
tags: [worker, celery, timeout, task, soft_time_limit]
priority: 10
---

The Celery worker task exceeded its time limit. Celery raised SoftTimeLimitExceeded or the task was killed by the hard time limit.

Error patterns that match this failure:
- "SoftTimeLimitExceeded"
- "TimeLimitExceeded"
- "time limit exceeded"
- "Task timed out after 5 minutes"
- "worker timed out"
- "celery.exceptions.SoftTimeLimitExceeded"

Root cause: The task ran longer than the configured Celery TASK_TIME_LIMIT (default 5 minutes). This often happens when the LLM call hangs, the workspace operation is slow, or a retry loop is stuck.

Recommended action: stop_retry. Retrying a task that timed out will likely time out again. Break the task into smaller steps. Check worker logs for where the task was stuck. Do not increase time limits as a first fix.
