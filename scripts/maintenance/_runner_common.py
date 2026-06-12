#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def maintenance_dir() -> Path:
    return Path(__file__).resolve().parent


def ensure_repo_on_syspath() -> Path:
    root = repo_root()
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return root


def chdir_repo_root() -> Path:
    root = repo_root()
    os.chdir(root)
    return root


def load_sibling_module(module_name: str, filename: str) -> ModuleType:
    path = maintenance_dir() / filename
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
