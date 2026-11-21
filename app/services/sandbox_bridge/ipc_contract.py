"""Correlate an in-sandbox shared-memory observation with exact Docker inspect evidence."""

from __future__ import annotations

from typing import Any


def classify_ipc_observation(
    observation: dict[str, Any],
    inspection: dict[str, Any] | None,
    *,
    expected_id: str,
    expected_name: str,
) -> str:
    """Return PASS, FAIL, or INVALID_PRECONDITION; never trusts request claims."""
    if not inspection or not expected_id or not expected_name:
        return "INVALID_PRECONDITION"
    if (
        inspection.get("Id") != expected_id
        or inspection.get("Name", "").lstrip("/") != expected_name
    ):
        return "INVALID_PRECONDITION"
    host = inspection.get("HostConfig")
    mounts = inspection.get("Mounts")
    if not isinstance(host, dict) or not isinstance(mounts, list):
        return "INVALID_PRECONDITION"
    if "IpcMode" not in host:
        return "INVALID_PRECONDITION"
    if host.get("IpcMode") != "none":
        return "FAIL"
    if any(
        not isinstance(m, dict) or m.get("Destination") in ("/dev/shm", "/run/shm")
        for m in mounts
    ):
        return "FAIL"
    if len(mounts) != 1 or mounts[0].get("Destination") != "/ws":
        return "FAIL"

    state = observation.get("path_state")
    mounted = observation.get("mounted")
    writable = observation.get("writable")
    if state not in ("absent", "present") or type(mounted) is not bool:
        return "INVALID_PRECONDITION"
    if mounted:
        return "FAIL"
    if state == "absent":
        return "PASS"
    if type(writable) is not bool:
        return "INVALID_PRECONDITION"
    return "FAIL" if writable else "PASS"
