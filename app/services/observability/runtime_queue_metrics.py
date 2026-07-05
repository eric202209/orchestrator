"""In-process runtime queue latency recorder — Phase 19E.

Read-only observability. Records wall-clock durations into small rolling
windows (per metric name) so `/ops/runtime-queues` can report live avg/p95/p99
without querying the DB. Samples are process-local and reset on restart —
this complements, not replaces, the DB-backed historical stats in
`ops_queue_latency`.

Recording a sample never raises and never blocks the caller; call sites
add a `record(...)` line around existing work without changing behavior.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict

_MAX_SAMPLES = 500
_lock = threading.Lock()
_samples: Dict[str, Deque[float]] = {}


def record(metric: str, seconds: float) -> None:
    """Append one duration sample (seconds) for `metric`. Never raises."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return
    with _lock:
        bucket = _samples.setdefault(metric, deque(maxlen=_MAX_SAMPLES))
        bucket.append(value)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


def stats(metric: str) -> Dict[str, float | int | None]:
    """Return count/avg/min/max/p50/p95/p99 for `metric` (None fields if empty)."""
    with _lock:
        values = sorted(_samples.get(metric, ()))
    if not values:
        return {
            "count": 0,
            "avg_seconds": None,
            "min_seconds": None,
            "max_seconds": None,
            "p50_seconds": None,
            "p95_seconds": None,
            "p99_seconds": None,
        }
    return {
        "count": len(values),
        "avg_seconds": round(sum(values) / len(values), 3),
        "min_seconds": round(values[0], 3),
        "max_seconds": round(values[-1], 3),
        "p50_seconds": round(_percentile(values, 0.50), 3),
        "p95_seconds": round(_percentile(values, 0.95), 3),
        "p99_seconds": round(_percentile(values, 0.99), 3),
    }


def all_stats() -> Dict[str, Dict[str, float | int | None]]:
    with _lock:
        metric_names = list(_samples.keys())
    return {name: stats(name) for name in metric_names}
