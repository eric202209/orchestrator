# P43-PRODUCT-23A: Sandbox image for the python312-pytest-no-network-v1 profile.
# Built once on the Host (PRODUCT-23B); its local image ID is then pinned as
# PYTEST_IMAGE in app/services/sandbox_bridge/docker_executor.py. The Launcher
# runs it with --pull=never, --network=none and the fixed isolation profile.
# Base is the exact image already pinned as IMAGE for the script profile.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --no-deps --only-binary=:all: \
        --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python3 -c "import pytest; assert pytest.__version__ == '9.0.3'"
USER 65534:65534
