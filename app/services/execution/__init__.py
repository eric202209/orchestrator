"""Execution-side services.

This package is downstream of Protocol v2 Planning and must never write to
Planning tables (``planning_*``, ``PlanningCheckpoint``,
``PlanningCompletionManifest``, ``PlanningCommitManifest``, review events).
"""
