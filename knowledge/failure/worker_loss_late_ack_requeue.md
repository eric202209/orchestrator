---
title: Worker Loss Requires Late Ack Requeue
type: failure_memory
applies_to:
  - failure
  - planning
tags:
  - celery
  - worker
  - durability
failure_signature: worker_lost_or_pending_backlog
priority: 90
---

If a Celery worker accepts orchestration tasks and exits before execution completes, pending work can appear stuck as RUNNING or PENDING unless orchestration tasks use late acknowledgements and worker-loss requeue behavior.

For long-running orchestration tasks, prefer `task_acks_late=True`, `task_reject_on_worker_lost=True`, `task_acks_on_failure_or_timeout=True`, and `worker_prefetch_multiplier=1`. This keeps a worker crash from silently consuming a batch and improves recovery evidence quality.

When validating locally on constrained agent hosts, use a low-concurrency or solo worker so failures reflect orchestration behavior instead of process pressure.
