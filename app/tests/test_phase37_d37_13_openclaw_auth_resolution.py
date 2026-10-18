"""Offline contract test for OpenClaw's installed auth-profile resolver."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tests.test_phase37_d37_13_ephemeral_runtime_credential_minimization import (
    LOCAL_TOKEN,
    _bind,
    _persistent_config,
)


_RESOLVER_SCRIPT = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const [modulePath, configPath, agentDir] = process.argv.slice(1);
const auth = await import(pathToFileURL(modulePath).href);
const resolve = auth.resolveApiKeyForProvider ?? auth.s;
if (typeof resolve !== "function") process.exit(3);
const cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));
const projectedKey = cfg.models.providers.openai.apiKey;
const result = await resolve({ provider: "openai", cfg, agentDir });
process.stdout.write(JSON.stringify({
  source: result.source,
  profileId: result.profileId ?? null,
  keyMatchesProjection: result.apiKey === projectedKey,
}));
"""


def _installed_resolver() -> tuple[str, str]:
    openclaw = shutil.which("openclaw")
    node = shutil.which("node")
    if not openclaw or not node:
        pytest.skip("OpenClaw and Node.js are required for resolver compatibility")
    binary = Path(openclaw).resolve()
    package_root = next(
        (parent for parent in binary.parents if (parent / "dist").is_dir()), None
    )
    if package_root is None:
        pytest.skip("Installed OpenClaw package directory is unavailable")
    modules = [
        path
        for path in (package_root / "dist").glob("model-auth-*.js")
        if "markers" not in path.name
    ]
    if not modules:
        pytest.skip("Installed OpenClaw auth resolver module is unavailable")
    return node, str(sorted(modules)[0])


def test_generated_binding_resolves_projected_marker_without_provider_request(
    tmp_path,
):
    node, resolver_module = _installed_resolver()
    config, _before = _persistent_config(tmp_path)
    source = json.loads(config.read_text(encoding="utf-8"))
    source["models"]["providers"]["openai"]["apiKey"] = LOCAL_TOKEN
    config.write_text(json.dumps(source, indent=2), encoding="utf-8")
    binding = _bind(tmp_path, config, "openclaw-contract")
    try:
        projected = json.loads(binding.config_path.read_text(encoding="utf-8"))
        assert projected["models"]["providers"]["openai"]["apiKey"] == LOCAL_TOKEN
        proc = subprocess.run(
            [
                node,
                "--input-type=module",
                "-e",
                _RESOLVER_SCRIPT,
                resolver_module,
                str(binding.config_path),
                str(binding._tmp_dir / "agent"),
            ],
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(binding._tmp_dir / "home"),
                "OPENCLAW_STATE_DIR": str(binding._tmp_dir / "state"),
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0, "OpenClaw resolver subprocess failed"
        result = json.loads(proc.stdout)
        assert result == {
            "source": "profile:openai:default",
            "profileId": "openai:default",
            "keyMatchesProjection": True,
        }
    finally:
        binding.release()
