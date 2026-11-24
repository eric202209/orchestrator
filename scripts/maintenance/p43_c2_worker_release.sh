#!/usr/bin/env bash
# P43-PRODUCT-22-C2-C3B: stage an exact committed candidate as an immutable
# git-archive release. The same verified source is the Host deployment source
# and C2 Worker build context.
#
#   scripts/maintenance/p43_c2_worker_release.sh stage <commit> <new-dir>
#   scripts/maintenance/p43_c2_worker_release.sh stage-host <commit> <tree> <new-dir> <repo>
#
# The context holds only the tracked files of <commit> (never the working
# tree) plus the identity marker verified by worker_identity without Git.
#
#   docker build -f <new-dir>/Dockerfile.worker \
#       --build-arg ORCHESTRATOR_GIT_SHA=<commit> -t <tag> <new-dir>
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

refuse() {
    echo "p43-c2-worker-release: $*" >&2
    exit 78
}

git_safe() {
    [[ "${1:-}" == -C && -n "${2:-}" ]] || refuse "internal Git invocation is not scoped"
    local repo=$2
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1 \
        GIT_OPTIONAL_LOCKS=0 GIT_PAGER=cat \
        /usr/bin/git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
        -c core.attributesFile=/dev/null -c "safe.directory=$repo" "$@"
}

if [[ $# -eq 3 && $1 == stage ]]; then
    host_stage=0
    repo=$(git_safe -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)
elif [[ $# -eq 5 && $1 == stage-host ]]; then
    host_stage=1
    [[ $EUID -eq 0 ]] || refuse "Host source staging requires root"
    [[ "$2" =~ ^[0-9a-f]{40}$ && "$3" =~ ^[0-9a-f]{40}$ && "$5" == /* ]] ||
        refuse "stage-host requires full candidate/tree ids and an absolute repository path"
    repo=$(git_safe -C "$5" rev-parse --show-toplevel)
else
    refuse "usage: $0 stage <commit> <new-dir> | stage-host <commit> <tree> <new-dir> <repo>"
fi
commit=$(git_safe -C "$repo" rev-parse --verify --quiet "$2^{commit}") ||
    refuse "commit not found: $2"
if (( host_stage )); then
    [[ "$(git_safe -C "$repo" rev-parse --show-toplevel)" == "$repo" ]] ||
        refuse "repository root did not resolve exactly"
    [[ "$commit" == "$2" ]] || refuse "commit must be the full resolved object id"
fi
if (( host_stage )); then
    [[ "$(git_safe -C "$repo" rev-parse HEAD)" == "$commit" ]] ||
        refuse "repository HEAD does not match candidate commit"
    [[ "$(git_safe -C "$repo" rev-parse "$commit^{tree}")" == "$3" ]] ||
        refuse "repository tree does not match independently supplied candidate tree"
    [[ -z "$(git_safe -C "$repo" status --porcelain)" ]] ||
        refuse "repository working tree is not clean"
    git_safe -C "$repo" show "$commit:scripts/maintenance/p43_c2_worker_release.sh" |
        /usr/bin/cmp -s - "${BASH_SOURCE[0]}" || refuse "stager is not the committed candidate version"
fi
tree=$(git_safe -C "$repo" rev-parse "$commit^{tree}")
if (( host_stage )); then out=$4; else out=$3; fi
if (( host_stage )); then
    [[ "$out" == /opt/orchestrator-releases/c2-c3b ]] ||
        refuse "Host source destination must be /opt/orchestrator-releases/c2-c3b"
    for parent in /opt /opt/orchestrator-releases; do
        [[ -d $parent && ! -L $parent && "$(stat -c '%u' "$parent")" == 0 &&
           $(( 0$(stat -c '%a' "$parent") & 022 )) -eq 0 ]] ||
            refuse "release parent is not root-controlled: $parent"
    done
    destination=$(readlink -m "$out")
    mounts=$(docker inspect ai-agent --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' 2>/dev/null) ||
        refuse "cannot verify source isolation from ai-agent mounts"
    while IFS= read -r mount_source; do
        [[ -n "$mount_source" ]] || continue
        mount_source=$(readlink -m "$mount_source")
        [[ "$mount_source" == / || "$destination" == "$mount_source" ||
           "$destination" == "$mount_source"/* || "$mount_source" == "$destination"/* ]] &&
            refuse "Host source destination overlaps ai-agent mount $mount_source"
    done <<<"$mounts"
fi
[[ ! -e $out && ! -L $out ]] || refuse "output already exists: $out"

mkdir -m 0700 -- "$out"
tree_manifest=
cleanup() {
    rm -rf --one-file-system -- "$out"
    [[ -z "$tree_manifest" ]] || rm -f -- "$tree_manifest"
}
trap cleanup ERR INT TERM
if (( host_stage )); then
    tree_manifest=$(mktemp /run/p43-c2-tree.XXXXXX)
    git_safe -C "$repo" ls-tree -rz -r --full-tree "$commit" >"$tree_manifest"
fi
archive=$out/.candidate.tar
git_safe -C "$repo" archive --format=tar "$commit" >"$archive"
# Validate the archive before extraction: only canonical relative paths and
# regular files/directories are accepted, so links cannot redirect extraction.
/usr/bin/python3 -I - "$archive" "$out" <<'PY'
import os, pathlib, shutil, sys, tarfile
archive, destination = map(pathlib.Path, sys.argv[1:])
with tarfile.open(archive, "r:") as stream:
    members = stream.getmembers()
    seen = set()
    for member in members:
        raw_name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
        path = pathlib.PurePosixPath(raw_name)
        if ("\\" in raw_name or "\x00" in raw_name or path.is_absolute() or not path.parts or
                any(part in ("", ".", "..") for part in path.parts) or
                path.as_posix() != raw_name or
                not (member.isfile() or member.isdir()) or raw_name in seen):
            raise SystemExit("unsafe candidate archive member")
        seen.add(raw_name)
    for member in members:
        raw_name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
        target = destination.joinpath(*pathlib.PurePosixPath(raw_name).parts)
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
[[ ! -e $target && ! -L $target && ! -e "$out/release-manifest.json" ]] ||
    refuse "commit tree already contains a reserved release identity path"
commit_b64=$(git_safe -C "$repo" cat-file commit "$commit" | base64 -w0)
printf '{"schema_version":1,"kind":"git-archive-release","candidate_head":"%s","candidate_tree":"%s","commit_object":"%s"}\n' \
    "$commit" "$tree" "$commit_b64" >"$target"
if (( host_stage )); then
    # Validate extracted bytes against Git's committed tree without importing
    # or executing any Python module from the development repository.
    /usr/bin/python3 -I - "$tree_manifest" "$out" "$commit" "$tree" <<'PY'
import base64, hashlib, json, os, pathlib, re, stat, sys

manifest_path, root = map(pathlib.Path, sys.argv[1:3])
expected_head, expected_tree = sys.argv[3:5]
def git_oid(kind, data):
    return hashlib.sha1(kind + b" " + str(len(data)).encode() + b"\0" + data).digest()

expected = {}
for record in manifest_path.read_bytes().split(b"\0"):
    if not record:
        continue
    metadata, raw_path = record.split(b"\t", 1)
    mode, kind, oid = metadata.decode("ascii").split(" ")
    if kind != "blob" or mode not in {"100644", "100755"} or not re.fullmatch(r"[0-9a-f]{40}", oid):
        raise SystemExit("candidate contains unsupported Git entry type or mode")
    path = raw_path.decode("utf-8", "strict")
    pure = pathlib.PurePosixPath(path)
    if ("\\" in path or "\0" in path or pure.is_absolute() or
            not pure.parts or pure.as_posix() != path or
            any(part in {"", ".", ".."} for part in pure.parts) or path in expected):
        raise SystemExit("candidate contains unsafe or duplicate Git path")
    expected[path] = (mode, oid)

marker_path = root / ".orchestrator-release-identity.json"
marker = json.loads(marker_path.read_text(encoding="utf-8"))
if (set(marker) != {"schema_version", "kind", "candidate_head", "candidate_tree", "commit_object"} or
        marker["schema_version"] != 1 or marker["kind"] != "git-archive-release" or
        marker["candidate_head"] != expected_head or marker["candidate_tree"] != expected_tree):
    raise SystemExit("candidate identity marker mismatch")
commit_data = base64.b64decode(marker["commit_object"], validate=True)
if git_oid(b"commit", commit_data).hex() != expected_head or not commit_data.startswith(f"tree {expected_tree}\n".encode()):
    raise SystemExit("candidate commit object does not anchor the expected tree")

actual_files, actual_dirs = set(), set()
for current, dirs, files in os.walk(root, followlinks=False):
    base = pathlib.Path(current)
    for name in dirs + files:
        item = base / name
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise SystemExit("candidate extraction contains a link or non-root entry")
        rel = item.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            actual_dirs.add(rel)
        elif stat.S_ISREG(info.st_mode):
            if rel != ".orchestrator-release-identity.json":
                actual_files.add(rel)
        else:
            raise SystemExit("candidate extraction contains an unsupported file type")
if actual_files != set(expected):
    raise SystemExit("candidate archive is incomplete or contains untracked files")
expected_dirs = {str(pathlib.PurePosixPath(p).parent) for p in expected if "/" in p}
for path in tuple(expected_dirs):
    parent = pathlib.PurePosixPath(path).parent
    while str(parent) != ".":
        expected_dirs.add(str(parent))
        parent = parent.parent
if actual_dirs != expected_dirs:
    raise SystemExit("candidate archive directory inventory mismatch")

for path, (mode, expected_blob) in expected.items():
    item = root / pathlib.PurePosixPath(path)
    info = item.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            bool(info.st_mode & stat.S_IXUSR) != (mode == "100755")):
        raise SystemExit(f"candidate file mode/type mismatch: {path}")
    digest = hashlib.sha1()
    digest.update(f"blob {info.st_size}\0".encode())
    with item.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_blob:
        raise SystemExit(f"candidate file content mismatch: {path}")

def tree_oid(directory, rel=""):
    entries = []
    for child in directory.iterdir():
        child_rel = f"{rel}/{child.name}".lstrip("/")
        if child_rel == ".orchestrator-release-identity.json":
            continue
        info = child.lstat()
        name = os.fsencode(child.name)
        if stat.S_ISDIR(info.st_mode):
            oid = tree_oid(child, child_rel)
            entries.append((name + b"/", b"40000 " + name + b"\0" + oid))
        else:
            mode, oid_hex = expected[child_rel]
            entries.append((name, mode.encode() + b" " + name + b"\0" + bytes.fromhex(oid_hex)))
    entries.sort(key=lambda entry: entry[0])
    return git_oid(b"tree", b"".join(entry for _, entry in entries))

if tree_oid(root).hex() != expected_tree:
    raise SystemExit("staged bytes do not reproduce the independently supplied Git tree")
release_manifest = {
    "schema_version": 1,
    "kind": "p43-host-source",
    "candidate_head": expected_head,
    "candidate_tree": expected_tree,
    "files": [
        {"path": path, "mode": expected[path][0], "blob_sha1": expected[path][1]}
        for path in sorted(expected)
    ],
}
(root / "release-manifest.json").write_text(
    json.dumps(release_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
    encoding="utf-8",
)
PY
    rm -f -- "$tree_manifest"
    tree_manifest=
    # Protect the independently verified candidate before any other root
    # operation consumes it. Preserve the Git executable bit.
    find "$out" -type d -exec chmod 0555 {} +
    find "$out" -type f ! -perm /111 -exec chmod 0444 {} +
    find "$out" -type f -perm /111 -exec chmod 0555 {} +
    chmod 0555 "$out"
fi
trap - ERR INT TERM
printf 'CANDIDATE_HEAD=%s\nCANDIDATE_TREE=%s\nRELEASE_CONTEXT=%s\n' \
    "$commit" "$tree" "$out"
