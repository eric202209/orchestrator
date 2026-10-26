"""Phase 40D autonomous engineering benchmark.

Mode A (``evaluate-history``) is offline and read-only: it normalizes retained
Phase 40B-40C evidence into versioned attempt records and metrics. Mode B
(``run``) prepares a controlled benchmark against the isolated development
runtime; live execution is not implemented and fails closed.
"""
