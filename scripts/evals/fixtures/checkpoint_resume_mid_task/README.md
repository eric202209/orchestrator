# Checkpoint Resume Mid Task Fixture

Seed workspace for the `checkpoint_resume_mid_task` evaluation case.

The fixture represents a partially completed two-step task. Step one has already
produced `docs/step-one.txt`; step two is still incomplete. The orchestrator
should resume from the existing checkpoint context, preserve completed work, and
finish the remaining implementation without destructive replay.

Suggested task prompt:

```text
Resume a partially completed multi-step task from checkpoint and finish without
replaying completed work destructively. Keep the existing step-one artifact
unchanged, implement the missing step-two behavior, and verify with
python3 -m pytest -q.
```

Verifier command:

```bash
python3 -m pytest -q
```

The seed fixture is expected to fail because step two is not implemented. After
a correct resume, `build_status_report()` should include both step outputs and
the existing step-one artifact should remain unchanged.
