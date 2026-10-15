# Phase 37 OPS1-B4B-DP3-F0-R2 — disposable image for scripts/verification/f0r1.sh.
#
# linux/arm64 only. No COPY/ADD, no USER, no ENV, no ARG, no secrets. Build it
# from stdin so no build context is sent:
#   docker build --platform linux/arm64 --no-cache -t orch-f0r1-rehearsal:r2 - \
#     < scripts/verification/f0r2-image/Dockerfile
#
# BUILD-TIME REQUIREMENT: replace the placeholder below with an operator-approved
# immutable ubuntu:24.04 linux/arm64 digest (sha256:<64 hex>). The placeholder is
# not a valid digest, so the build fails closed until that is done.
FROM ubuntu:24.04@sha256:OPERATOR_APPROVED_UBUNTU_24_04_ARM64_DIGEST_REQUIRED

# python3-minimal  /usr/bin/python3, the target of the staged venv's bin/python3
# python3.12       /usr/bin/python3.12 + libpython3.12-stdlib (_sqlite3 module)
# libsqlite3-0     SQLite library for R4 (WAL/SHM)
# redis-server     disposable broker (R5)
# libstdc++6 libgcc-s1 zlib1g   system libraries NEEDED by the venv's native wheels
# bash coreutils findutils grep sed mawk util-linux   script tools (setpriv: util-linux)
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3-minimal python3.12 libsqlite3-0 redis-server \
        libstdc++6 libgcc-s1 zlib1g \
        bash coreutils findutils grep sed mawk util-linux; \
    rm -rf /var/lib/apt/lists/*; \
    test "$(readlink -f /usr/bin/python3)" = /usr/bin/python3.12; \
    /usr/bin/python3.12 -I -S -c 'import sqlite3, sys; assert sys.version_info[:2] == (3, 12)'; \
    for tool in bash sh setpriv timeout sha256sum find cp chown chmod install redis-server \
        awk stat realpath sort xargs seq grep sed tr cut head cat readlink date wc sleep \
        uname env ls id; do \
        command -v "$tool" >/dev/null; \
    done; \
    dpkg-query -W -f '${Package}\t${Version}\t${Architecture}\n' \
        >/usr/share/f0r1-image-packages.tsv
