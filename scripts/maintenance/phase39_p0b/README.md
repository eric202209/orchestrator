# Phase 39 P0B maintenance tooling and protected release

This tracked package carries the pidfd-bound worker refresh, protected receipt
authority, Gate A verifier, and release tooling. The historical copies under
`docs/roadmap/reports/evidence/phase39-p0b-operator-tooling-01/` are evidence
only. Do not execute tooling from `docs/` or from the shared checkout as root.

## Release stages

| Stage | Result | Authority |
|---|---|---|
| `SOURCE_BUILD` | Canonical file inventory; `NOT_APPROVED`; no worksheet written | Unprivileged build from a committed source archive |
| `RELEASE_INSTALLATION` | Versioned root-owned release; `RELEASE_INSTALLED_NOT_APPROVED` | Existing protected installer or separately trusted bootstrap |
| `OPERATOR_APPROVAL` | Ed25519-signed, time-bounded record binding source, release, launcher, dependencies, configuration, worksheet, operator, and approval ID | External operator approval key and approval process |
| `WORKER_ACTIVATION` | A separately authorized pin install / worker refresh / Gate A operation | Fixed protected operator entry point |

A manifest SHA-256 detects changes but does not authenticate a release. The
installer requires an expected digest supplied through an independent review
channel. The execution entry point verifies an Ed25519 signature with the
root-owned trust key at
`/etc/orchestrator-maintenance/phase39-p0b/approval-ed25519.pub`. No key,
worksheet, or approval is created by this mission. Historical
`P39-P0B-GATEB-001` and `P39-P0B-GATEB-002` are explicitly rejected.

## Release payload and dependency closure

Build a payload from the exact committed Git tree, not from a writable checkout.
It contains the tracked repository source required by the worker, the three
maintenance modules and release entry points, `start.sh`, the worksheet helper,
the Python application tree, and a copied venv with the Python launcher,
`pyvenv.cfg`, Celery entry script, and every installed dependency. The venv
must contain regular files only; do not use symlinked or hardlinked executables.
The root `.env` configuration is a separate release input, mode `0600`, and is
bound by both its file hash and the configuration fingerprint. The release
manager excludes only `.env`, `venv/`, `logs/`, its own release metadata, and
`__pycache__/` from the committed Git tree calculation; the release manifest
still inventories the venv and configuration.

The dependency inventory is closed over these required names:

- Tracked Python: `operator_tooling`, `worker_pidfd`,
  `protected_refresh_authority`, `release_manager`, both Python entry points,
  `worker_identity`, `invocation_provenance`, and `worker_pin_worksheet`.
- Application and configuration: the complete `app/` source tree, `.env`,
  `venv/bin/python3`, `venv/bin/celery`, `venv/pyvenv.cfg`, Python standard
  library, and every installed Python distribution/native extension.
- Worker launcher: `start.sh`, `/usr/bin/bash`, `/usr/bin/env`,
  `/usr/bin/nohup`, `/usr/bin/cmp`, and `/usr/bin/stat`.
- Runtime base: Python's executable and standard library, the dynamic loader,
  `libpython`, `libc`, other native libraries recorded from ELF dependencies,
  Linux procfs, and the reviewed `pidfd_open` / `pidfd_send_signal` syscalls.
- Approval verifier: `/usr/bin/openssl` and the separately protected Ed25519
  public key.

All release files and directories must be root-owned, single-linked regular
files/directories with no group or other write bits. Code files are normalized
to `0444` or `0555`; `.env` is `0600`; only `logs/worker.log` is mutable runtime
state (`logs/` `0700`, log `0600`). All ancestors of the release path are
checked. The release manager rejects unmanifested files, symlinks, hardlinks,
wrong ownership/modes, missing dependencies, and changed hashes.

This implementation deliberately fails closed on symlinks/hardlinks in
external `system-tree` dependencies. On the current host, `/usr/lib/python3.12`
contains root-owned symlinks (`sitecustomize.py`,
`_sysconfigdata__linux_aarch64-linux-gnu.py`, and
`config-3.12-aarch64-linux-gnu/libpython3.12.so`) and hardlinked
`__pycache__`/`ensurepip` entries. A production dependency inventory therefore
needs a separately reviewed OS-package trust rule or a protected self-contained
runtime. This mission does not declare that external closure complete.

## Fixed entry points and environment

The installed operator command is the absolute path
`/usr/local/lib/orchestrator-maintenance/phase39-p0b/<release-id>/scripts/maintenance/phase39_p0b/entrypoint.sh`.
It accepts only `install`, `refresh`, and `gate-a`; runs `/usr/bin/bash` and
`/usr/bin/python3.12` by absolute path; clears the environment with
`/usr/bin/env -i`; uses Python `-I -S`; starts in `/`; validates the signed
approval, manifest, ownership, parent directories, modes, hashes, source tree,
launcher, and dependency inventory before importing operator modules; then
changes to the verified release directory. Only the verified release and its
inventoried site-packages are added to `sys.path`. Caller `PYTHONPATH`, user
site, cwd imports, and caller `sitecustomize` are not used. Python dependency
`.pth` files are rejected from the release inventory, so site startup
configuration cannot add paths or run code.

The release installation command is the absolute
`.../<existing-release>/scripts/maintenance/phase39_p0b/release-install.sh`.
It requires an already installed, protected maintenance release and root. It
does not execute a manager from the shared source checkout. It installs a new
version to a same-filesystem private staging directory, fsyncs files and
directories, then atomically renames to the versioned release ID without
overwriting an existing release. Installation ends at
`RELEASE_INSTALLED_NOT_APPROVED`; it cannot activate a worker.

The first installation has no existing protected installer. It requires a
root-owned bootstrap supplied by the host image/package manager and a trusted
source/authentication path. Do not run `release_manager.py install` as root
from this repository. That initial bootstrap is not supplied here.

## Offline source build and install commands

After the source is committed, a maintainer can create an archive and a clean
payload. The commit object is included so the existing worker identity module
can verify `HEAD` and the Git tree without `.git`:

```bash
/usr/bin/git -C <checkout> rev-parse HEAD
/usr/bin/git -C <checkout> rev-parse 'HEAD^{tree}'
/usr/bin/git -C <checkout> cat-file commit HEAD | /usr/bin/base64 -w0
```

Use `release_manager.py manifest --payload-root <payload> --release-id <id>
--source-head <head> --source-tree <tree> --commit-object-b64 <base64>
--source-manifest-sha256 <worksheet-derived-source-digest>
--configuration-fingerprint <fingerprint> --launcher-json <launcher.json>
--dependencies-json <dependencies.json> --output <release-manifest.json>`.
The builder must be unprivileged and must not create the authoritative worksheet
until the source is committed. Compare the resulting canonical manifest digest
through the independent review channel. A successful command is only
`SOURCE_BUILD`, never approval.

Offline fixture tests call `install_release()` with temporary target parents.
The production destination is fixed for future use:

```text
/usr/local/lib/orchestrator-maintenance/phase39-p0b/<release-id>/
```

The release installer accepts a caller-provided expected manifest digest,
committed HEAD/tree, and exact launcher identity. It rejects an existing or
partial release directory. No production installation was run for this
mission.

## Recovery

- Failure before the final rename removes the private staging directory during
  normal exception handling. A process interruption can leave a staging path
  and lock; do not clear either automatically. Inspect them as root and retain
  an audit record before manual cleanup.
- Failure after atomic rename preserves the candidate and its
  `RELEASE_INSTALLED_NOT_APPROVED` state. Verification failure blocks use;
  do not overwrite or silently delete it.
- A retry never replaces a release ID. Select a new release ID for a corrected
  candidate. Worker activation and rollback to an earlier release require a
  separate operator action and approval.

## Offline tests

From the repository root, run the tracked suites with the project's Python
test environment. Tests use disposable process or temporary filesystem
fixtures and do not operate on production pins or workers:

```bash
venv/bin/python3 -m unittest -q app.tests.test_phase39_p0b_operator_tooling
venv/bin/python3 -m unittest -q app.tests.test_phase39_p0b_pidfd_receipt_authority
venv/bin/python3 -m unittest -q app.tests.test_phase39_p0b_release_integrity
```
