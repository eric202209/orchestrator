# Phase 39 P0B maintenance tooling and release candidates

This package carries the pidfd-bound worker refresh, protected receipt
authority, Gate A verifier, and release checks. Source in this shared checkout
is not a root execution boundary.

## R1 package candidate

`release_packaging.py` builds a release-specific Ubuntu Noble arm64 `.deb`
under `/usr/lib/orchestrator-maintenance/phase39-p0b/<release-id>/`. The Debian
package name is `orchestrator-phase39-p0b-<release-id>` and the package version
is `39.1.0+<release-id>`. Package files are normalized to root-owned `0444` or
`0555` on installation. The canonical `release-package-manifest.json` binds
source HEAD/tree and tracked-worktree-delta identity to every payload file
hash, ABI requirements, package identity, and the explicit
`NOT_ACTIVATABLE` state.

R1 has one `preinst` guard because dpkg otherwise unpacks over an existing
release path. It rejects an existing or partial release ID and unsafe existing
parents. It cannot enable, start, stop, or restart services; change APT trust;
write approvals or pins; or invoke Gate A/B. The package has no postinst,
prerm, postrm, config, triggers, or conffiles. The guard is reviewed package
code and is not an artifact authenticator.

An offline package build uses committed tracked paths and records the exact
commit object. Any tracked worktree delta is separately hashed and makes the
artifact explicitly non-activatable. A release intended for future activation
must be rebuilt from a reviewed committed baseline. The builder does not
create keys or approvals.

## Repository trust and installation boundary

`publish_repository()` uses an already provisioned signing key. Tests generate
only disposable `TEST ONLY` keys in temporary directories. The resulting
repository contains canonical `Packages`, dated `Release`, and signed
`InRelease`; the package SHA-256 is in `Packages`, whose SHA-256 is signed in
`Release`. `verify_repository()` checks the dedicated keyring fingerprint,
InRelease signature, origin, Noble suite/codename, arm64 architecture,
validity window, canonical index, exact package/version/release ID, and
package bytes. A generated APT source uses an explicit `signed-by=` keyring;
the pin policy matches the package and signed Release origin. No host APT
source or keyring is changed.

The production order is distinct:

1. An independently provisioned `Signed-By` key authenticates APT metadata;
   signed metadata binds the exact `.deb` bytes and version.
2. APT selects that exact package/version and asks dpkg to unpack it. Bare
   `dpkg -i` is not an approved authentication path.
3. A separate post-install verifier checks the package manifest and installed
   files and confirms dpkg reports the exact package/version installed. Dpkg
   status alone does not prove current file integrity or APT origin.
4. A separate operator approval key authorizes a release-specific action.
   The APT key does not create or replace that approval.
5. A later worker-only activation step uses a protected release and an
   unprivileged worker identity. R1 cannot activate.

The old custom `release_manager.py install` production route is disabled;
the direct install helper remains only for isolated legacy test fixtures and
rejects the production path. `release-install.sh` is a fail-closed compatibility
stub. APT/dpkg is the sole selected production installation authority.

## Runtime and worker boundary

The package declares Ubuntu 24.04 Noble arm64, CPython 3.12, glibc 2.39 or
newer, Bash, and runtime pidfd capability requirements. R1 does not bundle or
close over Python wheels, native extensions, the full standard library, or ELF
dependencies; these are R2. No service unit or activation hook is shipped.

The application source is copied into the protected release tree, so a future
worker can load code from a versioned path instead of the writable checkout.
That is not sufficient by itself: R2 must provide its complete runtime, and R3
must launch the worker from the protected release under a dedicated
unprivileged service identity. The current `start.sh` is a development/full
stack launcher and remains unsuitable as a production service boundary.

Existing `release_manager.verify_installed_release()` and the signed-approval
checks remain useful for the later approval/runtime contract. The release
entry point now rejects a package manifest not explicitly marked
`ACTIVATABLE`; this candidate is `NOT_ACTIVATABLE`. Package manifest integrity
is not a signature: trust starts only with the independently authenticated APT
metadata and then the dpkg payload.

Historical approvals `P39-P0B-GATEB-001` and `P39-P0B-GATEB-002` remain
invalid. Do not create or reuse an approval, worksheet, pin, signing key, or
repository source from this checkout.

## Offline commands

Build an inert package to a disposable directory with a fixed source epoch:

```bash
venv/bin/python3 -m scripts.maintenance.phase39_p0b.release_packaging build \
  --checkout "$PWD" --output-dir /tmp/phase39-p0b-packages \
  --release-id auto
```

Verify package and signed repository fixtures with:

```bash
venv/bin/python3 -m unittest -q app.tests.test_phase39_p0b_signed_apt_packaging
```

These commands do not install the package. Do not run mutable checkout code as
root. Production source/key provisioning, APT installation, approval, worker
identity and launch policy, and activation are separate operator-controlled
missions.
