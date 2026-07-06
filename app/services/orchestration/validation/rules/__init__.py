"""Validator rule modules (Phase 20J skeleton).

Reserved package for the validator rule split planned in
docs/roadmap/refactoring-phases.md. `core_*` modules will own
`core_invariant` rules (structural boundaries that hold regardless of
workload); `contract_*` modules will own `workload_contract` rules
(reusable, workload-family-scoped checks). See
`app/services/orchestration/rule_registry.py` for the ownership taxonomy.

Empty as of Phase 20J — no rule implementation has moved here yet.
"""

from __future__ import annotations
