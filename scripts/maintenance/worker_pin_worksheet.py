#!/usr/bin/env python3
"""Offline, deterministic candidate-pin worksheet for the Celery Worker.

Derives the identity a pinned Worker boot will be compared against from the
checkout plus an explicit, operator-supplied launcher context.  It never
contacts a running Worker, reads boot manifests, Redis, the database, task
payloads or providers, and never writes or installs a pin.  The output is a
proposal: approval stays with the operator.

The ``source_manifest_digest`` embeds the launcher identity of the process that
builds the manifest (the Worker's own ``sys.argv``), so the helper's own argv
must not leak into it.  The launcher context therefore names the Worker
command explicitly and both launcher forms are derived from it:

- digest launcher: ``worker_source_launcher_identity`` with the interpreter and
  the Python-level argv ``[script, *args]`` (what the Worker sees);
- pin launcher: the ``/proc`` form ``[interpreter, script, *args]`` with the
  process cwd (what boot compares against the live master).

Run from the source root with that checkout's interpreter::

    venv/bin/python3 -m scripts.maintenance.worker_pin_worksheet \\
        --source-root "$PWD" --expected-head <sha> --expected-tree <sha> \\
        --launcher-context launcher-context.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

WORKSHEET_KIND = "orchestrator-worker-candidate-pin-worksheet"
WORKSHEET_SCHEMA_VERSION = 1
LAUNCHER_CONTEXT_SCHEMA_VERSION = 1
DERIVATION_METHOD = "explicit-launcher-context/worker_source_launcher_identity"
DERIVATION_VERSION = 1
NOT_APPROVED = "NOT_APPROVED"
_CONTEXT_FIELDS = frozenset(
    {
        "context_schema_version",
        "launcher",
        "interpreter",
        "script",
        "args",
        "cwd",
        "worker_node_identity",
    }
)
_CONCURRENCY = re.compile(r"--concurrency=([1-9][0-9]{0,3})")


class WorksheetError(RuntimeError):
    """A fail-closed derivation error; the message is a fixed reason code."""


def parse_launcher_context(value: Any) -> dict[str, Any]:
    """Validate the closed launcher-context schema without executing it."""

    if not isinstance(value, Mapping):
        raise WorksheetError("launcher_context_not_object")
    missing = _CONTEXT_FIELDS - set(value)
    if missing:
        raise WorksheetError(
            "launcher_context_missing_fields:" + ",".join(sorted(missing))
        )
    unknown = set(value) - _CONTEXT_FIELDS
    if unknown:
        raise WorksheetError(
            "launcher_context_unknown_fields:" + ",".join(sorted(unknown))
        )
    if (
        isinstance(value["context_schema_version"], bool)
        or value["context_schema_version"] != LAUNCHER_CONTEXT_SCHEMA_VERSION
    ):
        raise WorksheetError("launcher_context_unsupported_schema")
    for name in ("launcher", "interpreter", "script", "cwd", "worker_node_identity"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise WorksheetError(f"launcher_context_{name}_invalid")
    for name in ("interpreter", "script", "cwd"):
        if not Path(value[name]).is_absolute():
            raise WorksheetError(f"launcher_context_{name}_not_absolute")
    args = value["args"]
    if not isinstance(args, list) or not all(
        isinstance(item, str) and item for item in args
    ):
        raise WorksheetError("launcher_context_args_invalid")
    concurrency = [m for item in args if (m := _CONCURRENCY.fullmatch(item))]
    if len(concurrency) != 1 or any(
        item.startswith(("--concurrency", "-c")) and not _CONCURRENCY.fullmatch(item)
        for item in args
    ):
        raise WorksheetError("launcher_context_concurrency_invalid")
    return {**value, "concurrency": int(concurrency[0].group(1))}


def _require_candidate_process(root: Path) -> None:
    """Code and configuration must come from the candidate checkout itself."""

    from app.services.orchestration import worker_identity as wi

    if Path(wi.__file__).resolve().parents[3] != root:
        raise WorksheetError("helper_code_not_from_source_root")
    # Settings read .env relative to the process cwd.
    if Path.cwd().resolve() != root:
        raise WorksheetError("helper_cwd_not_source_root")


def _shebang_interpreter(script: Path) -> str:
    try:
        with script.open("rb") as handle:
            first = handle.readline(4096)
    except OSError as exc:
        raise WorksheetError("launcher_script_unreadable") from exc
    if not first.startswith(b"#!"):
        raise WorksheetError("launcher_script_shebang_missing")
    return first[2:].decode("utf-8", errors="replace").strip()


def derive_worksheet(
    source_root: str | Path,
    *,
    expected_head: str,
    expected_tree: str,
    launcher_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the deterministic, unapproved worksheet for one candidate."""

    from app.services.orchestration import worker_identity as wi

    context = parse_launcher_context(launcher_context)
    root = Path(source_root).expanduser().resolve(strict=True)
    _require_candidate_process(root)

    script = Path(context["script"])
    if not script.is_file():
        raise WorksheetError("launcher_script_missing")
    if _shebang_interpreter(script) != context["interpreter"]:
        raise WorksheetError("launcher_script_shebang_mismatch")
    if not Path(context["interpreter"]).resolve().is_file():
        raise WorksheetError("launcher_interpreter_missing")
    worker_argv = [context["script"], *context["args"]]
    try:
        digest_launcher = wi.worker_source_launcher_identity(
            root, executable=context["interpreter"], argv=worker_argv
        )
        manifest = wi.build_candidate_source_manifest(
            root,
            launcher_identity=digest_launcher,
            expected_head=expected_head,
            expected_tree=expected_tree,
        )
        pin_launcher = wi.canonical_launcher_identity(
            {
                "launcher": context["launcher"],
                "executable": context["interpreter"],
                "cwd": context["cwd"],
                "argv": [context["interpreter"], *worker_argv],
            }
        )
    except wi.CandidateManifestError as exc:
        raise WorksheetError(f"candidate_manifest_invalid:{exc}") from exc
    check = wi.verify_candidate_source_manifest(
        manifest,
        expected_head=expected_head,
        expected_tree=expected_tree,
        expected_source_root=str(root),
        expected_source_manifest_digest=manifest.source_manifest_digest,
    )
    if check.classification != wi.IDENTITY_VERIFIED:
        raise WorksheetError("candidate_manifest_unverified:" + ",".join(check.reasons))

    node = context["worker_node_identity"]
    if not wi._PIN_IDENTIFIER.fullmatch(node):
        raise WorksheetError("launcher_context_worker_node_identity_invalid")
    fingerprint, projection = _configuration_identity(context["concurrency"])

    from app.config import Settings

    proposed_pin = {
        "pin_schema_version": wi.CANDIDATE_PIN_SCHEMA_VERSION,
        "candidate_head": manifest.candidate_head,
        "candidate_tree": manifest.candidate_tree,
        "source_root": manifest.source_root,
        "source_manifest_digest": manifest.source_manifest_digest,
        "launcher_identity": pin_launcher,
        "worker_node_identity": node,
        "configuration_schema_version": projection.get("schema_version"),
        "configuration_fingerprint": fingerprint,
        "configuration_policy_id": projection.get("projection_policy_id"),
        # Deliberately not a valid approval: the loader rejects this body
        # until the operator supplies their own approval record.
        "approval": None,
    }
    return {
        "worksheet_kind": WORKSHEET_KIND,
        "worksheet_schema_version": WORKSHEET_SCHEMA_VERSION,
        "approval_state": NOT_APPROVED,
        "derivation": {
            "method": DERIVATION_METHOD,
            "version": DERIVATION_VERSION,
            "source_manifest_schema_version": manifest.schema_version,
            "affected_module_count": len(manifest.affected_modules),
            "inputs": [
                "checkout HEAD/tree and affected module sources",
                "explicit launcher context",
                "<source_root>/.env and process environment (configuration)",
            ],
            "environment_configuration_overrides": sorted(
                name for name in Settings.model_fields if name in os.environ
            ),
        },
        "OBSERVED_SOURCE": {
            "candidate_head": manifest.candidate_head,
            "candidate_tree": manifest.candidate_tree,
            "source_root": manifest.source_root,
            "dirty_source_policy": manifest.dirty_source_policy,
            "source_manifest_digest": manifest.source_manifest_digest,
            "digest_launcher_identity": dict(manifest.launcher_identity),
        },
        "explicit_launcher_context": {
            key: context[key] for key in sorted(_CONTEXT_FIELDS)
        },
        "pin_launcher_identity": pin_launcher,
        "configuration_identity": {
            "configuration_fingerprint": fingerprint,
            "configuration_schema_version": projection.get("schema_version"),
            "configuration_policy_id": projection.get("projection_policy_id"),
            "worker_concurrency": context["concurrency"],
        },
        "worker_node_identity": node,
        "validation": {
            "result": "VALID",
            "candidate_manifest_classification": check.classification,
            "expected_head_matched": True,
            "expected_tree_matched": True,
            "launcher_script_shebang_matched": True,
        },
        # Filled by the operator outside this tool; never by the helper.
        "OPERATOR_APPROVED_SOURCE": None,
        "proposed_pin_body": proposed_pin,
    }


def _configuration_identity(concurrency: int) -> tuple[str, dict[str, Any]]:
    from app.celery_app import celery_app

    celery_app.loader.import_default_modules()
    from app.tasks.worker import worker_configuration_fingerprint

    return worker_configuration_fingerprint(
        celery_app, SimpleNamespace(concurrency=concurrency)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive an unapproved Worker candidate-pin worksheet offline."
    )
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--launcher-context", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        try:
            context = json.loads(args.launcher_context.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorksheetError("launcher_context_unreadable") from exc
        worksheet = derive_worksheet(
            args.source_root,
            expected_head=args.expected_head,
            expected_tree=args.expected_tree,
            launcher_context=context,
        )
    except WorksheetError as exc:
        print(
            json.dumps({"validation": {"result": "FAILED", "reason": str(exc)}}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(worksheet, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
