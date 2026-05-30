# Repair Budget Diagnostics

This directory preserves the unique temporary repair-budget diagnostic
workspaces from `/tmp/repair-budget-diag-*` so they can be copied to another
machine and tested there.

Included variants:

- `missing_main_guard`: copied from `/tmp/repair-budget-diag-9n5qrexv`.
- `missing_docstring_and_main_guard`: copied from `/tmp/repair-budget-diag-e92_mp08`.

Skipped duplicate:

- `/tmp/repair-budget-diag-g13rwkah` matched the existing
  `scripts/evals/fixtures/python_cli_small_feature` source and tests.

Run either variant with:

```bash
cd scripts/evals/repair_budget_diagnostics/<variant>
python3 -m pytest -q
```
