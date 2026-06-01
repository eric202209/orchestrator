# Stale Replace Repair Fixture

Seed workspace for the `stale_replace_repair` evaluation case.

The fixture starts with a tiny inventory summary package whose tests fail
because the implementation emits stale label text and preserves insertion
order. The repair should make the existing public function produce stable,
sorted summary lines without weakening tests.

Suggested task prompt:

```text
Fix the failing inventory summary tests without weakening tests. Keep changes
scoped to src/ and tests/. Verify with python3 -m pytest -q.
```

Verifier command:

```bash
python3 -m pytest -q
```

The seed fixture is expected to fail on deterministic output mismatch. After a
correct repair, repeated item names should be counted and rendered in sorted
order as `name: count`.
