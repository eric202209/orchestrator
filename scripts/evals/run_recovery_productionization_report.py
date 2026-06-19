#!/usr/bin/env python3
"""Generate Phase 13B productionization recovery ops report."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.services.orchestration.recovery.recovery_metrics import (
    collect_recovery_ops_metrics,
)


def main() -> int:
    with SessionLocal() as db:
        metrics = collect_recovery_ops_metrics(db)
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
