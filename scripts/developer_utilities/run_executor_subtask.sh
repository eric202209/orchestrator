#!/usr/bin/env bash
set -euo pipefail

# Runs one executor subtask headlessly via `claude -p`, replacing the manual
# "paste prompt into tmux window 1, watch it work" step described in
# docs/roadmap/workflow/tmux-workflow.md. The planner (ChatGPT, in a browser)
# is unchanged: the operator still copies its generated subtask prompt out,
# but instead of pasting it into an interactive session, it goes into
# docs/roadmap/workflow/NEXT_PROMPT.md and this script runs it non-interactively.
#
# The file contract (WORKLOG.md / PHASE_LOG.md / HANDOFF_DRAFT.md) is
# untouched: CLAUDE.md and AGENTS.md load the same way in `-p` mode, so the
# executor still performs the same START/STOP ritual. This script only
# changes how the prompt goes in and how the handoff report comes back out.
#
# Usage:
#   scripts/developer_utilities/run_executor_subtask.sh [prompt-file]
#
# Default prompt-file: docs/roadmap/workflow/NEXT_PROMPT.md

PROMPT_FILE="${1:-docs/roadmap/workflow/NEXT_PROMPT.md}"
HANDOFF_FILE="docs/roadmap/workflow/HANDOFF_DRAFT.md"
LOG_DIR="logs"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_DIR}/executor-run-${TIMESTAMP}.log"

if [ ! -f "${PROMPT_FILE}" ]; then
    echo "Prompt file not found: ${PROMPT_FILE}"
    echo "Paste ChatGPT's subtask prompt into that file, then re-run."
    exit 1
fi

if [ ! -s "${PROMPT_FILE}" ]; then
    echo "Prompt file is empty: ${PROMPT_FILE}"
    exit 1
fi

echo "===== Subtask prompt (${PROMPT_FILE}) ====="
cat "${PROMPT_FILE}"
echo "============================================"
echo
read -r -p "Run this subtask now? [y/N] " CONFIRM
if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    echo "Aborted. Prompt file left untouched."
    exit 0
fi

mkdir -p "${LOG_DIR}"

echo "Running executor (log: ${LOG_FILE}) ..."
# --permission-mode acceptEdits auto-approves file edits only. Subtasks that
# need to run tests/commands (pytest, git status, etc.) will still block on
# a Bash permission prompt with no human present to answer it, and this
# invocation will hang until it times out. If your subtasks routinely need
# to run commands, switch this to --dangerously-skip-permissions (safe here
# only because this container is the same sandboxed dev environment
# tmux-workflow.md already assumes) rather than silently upgrading it.
if ! claude -p "$(cat "${PROMPT_FILE}")" \
    --output-format text \
    --permission-mode acceptEdits \
    2>&1 | tee "${LOG_FILE}"; then
    echo "Executor exited non-zero. Check ${LOG_FILE} and ${HANDOFF_FILE}."
    exit 1
fi

echo
echo "===== ${HANDOFF_FILE} (copy this back into ChatGPT) ====="
if [ -f "${HANDOFF_FILE}" ]; then
    cat "${HANDOFF_FILE}"
else
    echo "(missing — executor did not write a handoff report; check ${LOG_FILE})"
fi
echo "============================================================"
