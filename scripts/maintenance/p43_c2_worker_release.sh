#!/usr/bin/env bash
# P43-PRODUCT-22-C2-C3B: stage the C2 Worker image build context as a
# protected git-archive release of one exact committed candidate.
#
#   scripts/maintenance/p43_c2_worker_release.sh stage <commit> <new-dir>
#
# The context holds only the tracked files of <commit> (never the working
# tree, so uncommitted changes cannot be represented as that release) plus
# .orchestrator-release-identity.json in the format worker_identity verifies
# without Git (same writer as scripts/verification/f0r1.sh). Build with:
#
#   docker build -f <new-dir>/Dockerfile.worker \
#       --build-arg ORCHESTRATOR_GIT_SHA=<commit> -t <tag> <new-dir>
set -euo pipefail

refuse() {
    echo "p43-c2-worker-release: $*" >&2
    exit 78
}

[[ $# -eq 3 && $1 == stage ]] || refuse "usage: $0 stage <commit> <new-dir>"
repo=$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)
commit=$(git -C "$repo" rev-parse --verify --quiet "$2^{commit}") ||
    refuse "commit not found: $2"
tree=$(git -C "$repo" rev-parse "$commit^{tree}")
out=$3
[[ ! -e $out && ! -L $out ]] || refuse "output already exists: $out"

mkdir -m 0700 -- "$out"
git -C "$repo" archive --format=tar "$commit" | tar -x --no-same-owner -C "$out"
target=$out/.orchestrator-release-identity.json
[[ ! -e $target && ! -L $target ]] ||
    refuse "commit tree already contains $(basename -- "$target")"
commit_b64=$(git -C "$repo" cat-file commit "$commit" | base64 -w0)
printf '{"schema_version":1,"kind":"git-archive-release","candidate_head":"%s","candidate_tree":"%s","commit_object":"%s"}\n' \
    "$commit" "$tree" "$commit_b64" >"$target"
printf 'CANDIDATE_HEAD=%s\nCANDIDATE_TREE=%s\nRELEASE_CONTEXT=%s\n' \
    "$commit" "$tree" "$out"
