#!/usr/bin/env bash
# P43-PRODUCT-22-C2-C3B: stage an exact committed candidate as an immutable
# git-archive release. The same verified source is the Host deployment source
# and C2 Worker build context.
#
#   scripts/maintenance/p43_c2_worker_release.sh stage <commit> <new-dir>
#   scripts/maintenance/p43_c2_worker_release.sh stage-host <commit> <new-dir> <repo>
#
# The context holds only the tracked files of <commit> (never the working
# tree) plus the identity marker verified by worker_identity without Git.
#
#   docker build -f <new-dir>/Dockerfile.worker \
#       --build-arg ORCHESTRATOR_GIT_SHA=<commit> -t <tag> <new-dir>
set -euo pipefail

refuse() {
    echo "p43-c2-worker-release: $*" >&2
    exit 78
}

if [[ $# -eq 3 && $1 == stage ]]; then
    host_stage=0
    repo=$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)
elif [[ $# -eq 4 && $1 == stage-host ]]; then
    host_stage=1
    [[ $EUID -eq 0 ]] || refuse "Host source staging requires root"
    repo=$(git -C "$4" rev-parse --show-toplevel)
else
    refuse "usage: $0 stage <commit> <new-dir> | stage-host <commit> <new-dir> <repo>"
fi
commit=$(git -C "$repo" rev-parse --verify --quiet "$2^{commit}") ||
    refuse "commit not found: $2"
if (( host_stage )); then
    [[ "$commit" == "$2" ]] || refuse "commit must be the full resolved object id"
fi
if (( host_stage )); then
    [[ "$(git -C "$repo" rev-parse HEAD)" == "$commit" ]] ||
        refuse "repository HEAD does not match candidate commit"
    [[ -z "$(git -C "$repo" status --porcelain)" ]] ||
        refuse "repository working tree is not clean"
    git -C "$repo" show "$commit:scripts/maintenance/p43_c2_worker_release.sh" |
        cmp -s - "${BASH_SOURCE[0]}" || refuse "stager is not the committed candidate version"
fi
tree=$(git -C "$repo" rev-parse "$commit^{tree}")
out=$3
if (( host_stage )); then
    [[ "$out" == /opt/orchestrator-releases/c2-c3b ]] ||
        refuse "Host source destination must be /opt/orchestrator-releases/c2-c3b"
    [[ -d /opt/orchestrator-releases && ! -L /opt/orchestrator-releases &&
       "$(stat -c '%u' /opt/orchestrator-releases)" == 0 &&
       $(( 0$(stat -c '%a' /opt/orchestrator-releases) & 022 )) -eq 0 ]] ||
        refuse "release parent is not root-controlled"
fi
[[ ! -e $out && ! -L $out ]] || refuse "output already exists: $out"

mkdir -m 0700 -- "$out"
cleanup() { rm -rf --one-file-system -- "$out"; }
trap cleanup ERR INT TERM
archive=$out/.candidate.tar
git -C "$repo" archive --format=tar "$commit" >"$archive"
# Validate the archive before extraction: only canonical relative paths and
# regular files/directories are accepted, so links cannot redirect extraction.
python3 - "$archive" "$out" <<'PY'
import os, pathlib, shutil, sys, tarfile
archive, destination = map(pathlib.Path, sys.argv[1:])
with tarfile.open(archive, "r:") as stream:
    members = stream.getmembers()
    seen = set()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if (path.is_absolute() or not path.parts or
                any(part in ("", ".", "..") for part in path.parts) or
                not (member.isfile() or member.isdir()) or member.name in seen):
            raise SystemExit("unsafe candidate archive member")
        seen.add(member.name)
    for member in members:
        target = destination.joinpath(*pathlib.PurePosixPath(member.name).parts)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = stream.extractfile(member)
        if source is None:
            raise SystemExit("candidate archive member unreadable")
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        os.chmod(target, member.mode & 0o777)
archive.unlink()
PY
target=$out/.orchestrator-release-identity.json
[[ ! -e $target && ! -L $target ]] ||
    refuse "commit tree already contains $(basename -- "$target")"
commit_b64=$(git -C "$repo" cat-file commit "$commit" | base64 -w0)
printf '{"schema_version":1,"kind":"git-archive-release","candidate_head":"%s","candidate_tree":"%s","commit_object":"%s"}\n' \
    "$commit" "$tree" "$commit_b64" >"$target"
if (( host_stage )); then
    # The verifier treats write permission as a release-integrity failure.
    find "$out" -type d -exec chmod 0555 {} +
    find "$out" -type f ! -perm /111 -exec chmod 0444 {} +
    find "$out" -type f -perm /111 -exec chmod 0555 {} +
    chmod 0555 "$out"
    # Recompute the tree from staged bytes using the existing Worker verifier.
    python3 - "$repo" "$out" "$commit" "$tree" <<'PY'
import importlib.util, pathlib, sys
sys.dont_write_bytecode = True
repo, root, expected_head, expected_tree = map(pathlib.Path, sys.argv[1:])
module_path = repo / "app/services/orchestration/worker_identity.py"
spec = importlib.util.spec_from_file_location("p43_worker_identity", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
dirty, head, tree = module._release_identity(root)
if dirty or head != str(expected_head) or tree != str(expected_tree):
    raise SystemExit("staged candidate identity/tree mismatch")
PY
    chown -R 0:0 -- "$out"
fi
trap - ERR INT TERM
printf 'CANDIDATE_HEAD=%s\nCANDIDATE_TREE=%s\nRELEASE_CONTEXT=%s\n' \
    "$commit" "$tree" "$out"
