"""Verification integrity checks for task completion."""

from __future__ import annotations

import ast
import builtins
import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

CommandQuality = Literal[
    "behavioral", "regression_test", "smoke_only", "insufficient", "missing"
]

TEST_FILE_PATTERN = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")
SKIP_PATTERN = re.compile(
    r"pytest\.mark\.(?:skip|xfail)|unittest\.skip|@skip(?:if|unless)?\b"
)
ASSERT_TEXT_PATTERN = re.compile(
    r"\bassert\b|\bself\.assert[A-Z]\w*\(|\bpytest\.raises\("
)
FILE_EXISTENCE_PATTERN = re.compile(
    r"\b(?:os\.path\.)?(?:exists|isfile|isdir)\(|\.exists\(\)"
)
_PYTEST_KNOWN_GLOBALS = {
    "capsys",
    "capfd",
    "caplog",
    "monkeypatch",
    "tmp_path",
    "tmpdir",
    "pytestconfig",
    "request",
    "mocker",
}
_BUILTIN_NAMES = set(dir(builtins))


@dataclass(frozen=True)
class IntegrityFinding:
    code: str
    message: str
    path: Optional[str] = None
    line: Optional[int] = None
    severity: str = "warning"
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationEvidence:
    command: Optional[str] = None
    command_quality: CommandQuality = "missing"
    stdout_fingerprint: Optional[str] = None
    stderr_fingerprint: Optional[str] = None
    integrity_findings: list[IntegrityFinding] = field(default_factory=list)
    promotion_blockers: list[str] = field(default_factory=list)
    verification_insufficient: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["integrity_findings"] = [
            finding.to_dict() for finding in self.integrity_findings
        ]
        return payload


def output_fingerprint(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in str(text or "").splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def capture_baseline_result(
    *,
    command: str,
    returncode: Optional[int],
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    return {
        "command": str(command or ""),
        "returncode": returncode,
        "stdout_fingerprint": output_fingerprint(stdout),
        "stderr_fingerprint": output_fingerprint(stderr),
        "stdout_preview": str(stdout or "")[:500],
        "stderr_preview": str(stderr or "")[:500],
    }


def compare_baseline(
    before: Optional[dict[str, Any]],
    after: Optional[dict[str, Any]],
    policy: str = "pass_fail_transition",
) -> dict[str, Any]:
    if not before or not after:
        return {
            "policy": policy,
            "status": "missing",
            "passed": False,
            "reason": "before/after baseline evidence is incomplete",
        }
    before_code = before.get("returncode")
    after_code = after.get("returncode")
    if policy == "pass_fail_transition":
        passed = before_code not in (0, "0", None) and after_code in (0, "0")
        return {
            "policy": policy,
            "status": "passed" if passed else "failed",
            "passed": passed,
            "reason": (
                "failing command passed after repair"
                if passed
                else "command did not show a fail-to-pass transition"
            ),
            "before": before,
            "after": after,
        }
    if policy == "exact_match":
        passed = (
            before.get("stdout_fingerprint") == after.get("stdout_fingerprint")
            and before.get("stderr_fingerprint") == after.get("stderr_fingerprint")
            and before_code == after_code
        )
        return {
            "policy": policy,
            "status": "passed" if passed else "failed",
            "passed": passed,
            "reason": (
                "output fingerprints match"
                if passed
                else "output fingerprints changed unexpectedly"
            ),
            "before": before,
            "after": after,
        }
    return {
        "policy": policy,
        "status": "unsupported",
        "passed": False,
        "reason": f"unsupported baseline comparison policy: {policy}",
        "before": before,
        "after": after,
    }


def is_python_test_path(path: str | Path) -> bool:
    normalized = str(path).replace("\\", "/").lstrip("./")
    return bool(TEST_FILE_PATTERN.search(normalized))


def python_test_files(project_dir: str | Path) -> list[str]:
    root = Path(project_dir)
    if not root.exists():
        return []
    results: list[str] = []
    ignored_parts = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".openclaw",
    }
    for path in root.rglob("*.py"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if set(rel.parts) & ignored_parts:
            continue
        rel_text = str(rel).replace("\\", "/")
        if is_python_test_path(rel_text):
            results.append(rel_text)
    return sorted(results)


def pre_existing_python_test_files(
    project_dir: str | Path,
    change_set: Optional[dict[str, Any]],
) -> list[str]:
    added = {
        str(path).replace("\\", "/").lstrip("./")
        for path in ((change_set or {}).get("added_files") or [])
    }
    return [path for path in python_test_files(project_dir) if path not in added]


def classify_verification_command(command: Optional[str]) -> CommandQuality:
    text = str(command or "").strip().lower()
    if not text:
        return "missing"

    if re.search(r"(?:^|[;&|()\n])\s*(?:echo|ls|cat|find|wc\s+-l)\b", text):
        return "insufficient"
    if re.search(r"(?:^|[;&|()\n])\s*grep\s+-q\b", text):
        return "insufficient"
    if re.search(r"(?:^|[;&|()\n])\s*test\s+-\w\b", text):
        return "smoke_only"
    if re.search(r"\bpy_compile\b|\bpython(?:3)?\s+-c\s+['\"]\s*import\b", text):
        if "unittest.main" in text and "discover" not in text:
            return "insufficient"
        return "smoke_only"
    if "python -m unittest" in text or "python3 -m unittest" in text:
        return "regression_test"
    if "pytest" in text or "npm test" in text or "pnpm test" in text:
        return "regression_test"
    if "cargo test" in text or "go test" in text:
        return "regression_test"
    if (
        "npm run build" in text
        or "pnpm build" in text
        or "yarn build" in text
        or re.search(r"\btsc\b", text)
    ):
        return "behavioral"
    if re.search(r"\b(?:python3?|node)\s+\S+", text):
        return "behavioral"
    if "uv run" in text:
        return "behavioral"
    return "insufficient"


def scan_test_file_changes(
    changed_files: Iterable[str | Path],
    project_dir: str | Path,
) -> list[IntegrityFinding]:
    root = Path(project_dir)
    findings: list[IntegrityFinding] = []
    seen: set[str] = set()
    for raw_path in changed_files:
        rel_path = str(raw_path).replace("\\", "/").lstrip("./")
        if rel_path in seen or not is_python_test_path(rel_path):
            continue
        seen.add(rel_path)
        path = (root / rel_path).resolve()
        try:
            if not path.is_relative_to(root.resolve()):
                continue
        except ValueError:
            continue
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        findings.extend(_scan_test_text(text, rel_path))
    return findings


def scan_python_test_text(text: str, rel_path: str) -> list[IntegrityFinding]:
    """Scan supplied Python test text without reading it from disk."""

    if not is_python_test_path(rel_path):
        return []
    return _scan_test_text(text, rel_path)


def check_test_preservation(
    change_set: Optional[dict[str, Any]],
    project_dir: str | Path,
) -> list[IntegrityFinding]:
    payload = change_set or {}
    added = [str(path) for path in payload.get("added_files") or []]
    modified = [str(path) for path in payload.get("modified_files") or []]
    deleted = [str(path) for path in payload.get("deleted_files") or []]

    findings: list[IntegrityFinding] = []
    for path in deleted:
        if is_python_test_path(path):
            findings.append(
                IntegrityFinding(
                    code="test_weakened_or_removed",
                    message="Repair deleted an existing Python test file",
                    path=path,
                    severity="error",
                    confidence="high",
                )
            )

    findings.extend(scan_test_file_changes([*added, *modified], project_dir))
    findings.extend(_scan_assertion_drops(payload, project_dir))
    return findings


def _scan_test_text(text: str, rel_path: str) -> list[IntegrityFinding]:
    findings: list[IntegrityFinding] = []
    if SKIP_PATTERN.search(text):
        findings.append(
            IntegrityFinding(
                code="skip_added",
                message="Test file contains skip or xfail markers",
                path=rel_path,
                severity="error",
                confidence="high",
            )
        )

    has_assertion_text = bool(ASSERT_TEXT_PATTERN.search(text))
    has_file_existence = bool(FILE_EXISTENCE_PATTERN.search(text))
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return findings + [
            IntegrityFinding(
                code="test_parse_failed",
                message="Python test file could not be parsed for integrity checks",
                path=rel_path,
                line=exc.lineno,
                severity="warning",
                confidence="medium",
            )
        ]

    test_functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]
    if test_functions and not has_assertion_text:
        findings.append(
            IntegrityFinding(
                code="import_only_verification",
                message="Test file defines tests without assertions or exception checks",
                path=rel_path,
                severity="error",
                confidence="high",
            )
        )
    if test_functions and has_file_existence and _only_file_existence_assertions(tree):
        findings.append(
            IntegrityFinding(
                code="file_existence_only",
                message="Test assertions only verify file existence",
                path=rel_path,
                severity="error",
                confidence="high",
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            if _is_tautological_assert(node.test):
                findings.append(
                    IntegrityFinding(
                        code="tautological_assertion",
                        message="Test contains an assertion that cannot fail meaningfully",
                        path=rel_path,
                        line=node.lineno,
                        severity="error",
                        confidence="high",
                    )
                )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test") and _has_self_derived_expected_value(node):
                findings.append(
                    IntegrityFinding(
                        code="self_derived_expected_value",
                        message=(
                            "Test computes an expected value from the same call "
                            "pattern it verifies"
                        ),
                        path=rel_path,
                        line=node.lineno,
                        severity="error",
                        confidence="high",
                    )
                )
    findings.extend(_undefined_test_name_findings(tree, rel_path))
    return findings


def _undefined_test_name_findings(
    tree: ast.AST,
    rel_path: str,
) -> list[IntegrityFinding]:
    imported: set[str] = set()
    module_defined: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            imported.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                module_defined.update(_bound_names(target))
        elif isinstance(node, ast.AnnAssign):
            module_defined.update(_bound_names(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            module_defined.update(_statement_bound_names(node))

    findings: list[IntegrityFinding] = []
    module_scope = imported | module_defined | _BUILTIN_NAMES | _PYTEST_KNOWN_GLOBALS
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        local_defined = set(module_scope)
        local_defined.update(arg.arg for arg in node.args.args)
        local_defined.update(arg.arg for arg in node.args.kwonlyargs)
        if node.args.vararg:
            local_defined.add(node.args.vararg.arg)
        if node.args.kwarg:
            local_defined.add(node.args.kwarg.arg)

        loaded_before_store: list[ast.Name] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id not in local_defined and child.id not in {"self", "cls"}:
                    loaded_before_store.append(child)
            elif isinstance(child, ast.Name) and isinstance(
                child.ctx, (ast.Store, ast.Param)
            ):
                local_defined.add(child.id)
            elif isinstance(child, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                local_defined.update(_statement_bound_names(child))

        seen: set[str] = set()
        for name in loaded_before_store:
            if name.id in seen:
                continue
            seen.add(name.id)
            findings.append(
                IntegrityFinding(
                    code="undefined_test_name",
                    message=(
                        "Python test references an obvious undefined name: "
                        f"{name.id}"
                    ),
                    path=rel_path,
                    line=getattr(name, "lineno", None),
                    severity="error",
                    confidence="high",
                )
            )
    return findings


def _statement_bound_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, (ast.For, ast.AsyncFor)):
        names.update(_bound_names(node.target))
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                names.update(_bound_names(item.optional_vars))
    return names


def _bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in node.elts:
            names.update(_bound_names(elt))
        return names
    return set()


def _scan_assertion_drops(
    change_set: dict[str, Any],
    project_dir: str | Path,
) -> list[IntegrityFinding]:
    snapshot_raw = str(change_set.get("snapshot_path") or "").strip()
    if not snapshot_raw:
        return []
    snapshot_root = Path(snapshot_raw)
    target_root = Path(str(change_set.get("target_path") or project_dir))
    if not snapshot_root or not snapshot_root.exists():
        return []

    findings: list[IntegrityFinding] = []
    for raw_path in change_set.get("modified_files") or []:
        rel_path = str(raw_path).replace("\\", "/").lstrip("./")
        if not is_python_test_path(rel_path):
            continue
        before_path = (snapshot_root / rel_path).resolve()
        after_path = (target_root / rel_path).resolve()
        try:
            if not before_path.is_relative_to(snapshot_root.resolve()):
                continue
            if not after_path.is_relative_to(target_root.resolve()):
                continue
        except ValueError:
            continue
        if not before_path.is_file() or not after_path.is_file():
            continue

        before_counts = _assertion_counts_by_test(before_path)
        after_counts = _assertion_counts_by_test(after_path)
        for test_name, before_count in sorted(before_counts.items()):
            after_count = after_counts.get(test_name, 0)
            if before_count > after_count:
                findings.append(
                    IntegrityFinding(
                        code="test_weakened_or_removed",
                        message=(
                            f"Test {test_name} has fewer assertions after repair "
                            f"({before_count} -> {after_count})"
                        ),
                        path=rel_path,
                        severity="error",
                        confidence="high",
                    )
                )
    return findings


def _assertion_counts_by_test(path: Path) -> dict[str, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}

    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        counts[node.name] = sum(
            1
            for child in ast.walk(node)
            if isinstance(child, ast.Assert) or _is_unittest_assert_call(child)
        )
    return counts


def _is_unittest_assert_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("assert")
    )


def _has_self_derived_expected_value(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    assigned_calls: dict[str, str] = {}
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and len(child.targets) == 1:
            target = child.targets[0]
            if isinstance(target, ast.Name) and isinstance(child.value, ast.Call):
                assigned_calls[target.id] = ast.dump(
                    child.value, include_attributes=False
                )

    if len(assigned_calls) < 2:
        return False

    for child in ast.walk(node):
        if not isinstance(child, ast.Assert):
            continue
        for compare in ast.walk(child.test):
            if not (
                isinstance(compare, ast.Compare)
                and len(compare.ops) == 1
                and isinstance(compare.ops[0], (ast.Eq, ast.Is))
                and len(compare.comparators) == 1
            ):
                continue
            left_call = _comparison_call_fingerprint(compare.left, assigned_calls)
            right_call = _comparison_call_fingerprint(
                compare.comparators[0], assigned_calls
            )
            if left_call and right_call and left_call == right_call:
                return True
    return False


def _comparison_call_fingerprint(
    node: ast.AST,
    assigned_calls: dict[str, str],
) -> Optional[str]:
    if isinstance(node, ast.Name):
        return assigned_calls.get(node.id)
    if isinstance(node, ast.Call):
        return ast.dump(node, include_attributes=False)
    return None


def _only_file_existence_assertions(tree: ast.AST) -> bool:
    assertions = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    if not assertions:
        return False
    return all(_contains_file_existence_call(node.test) for node in assertions)


def _contains_file_existence_call(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute) and func.attr in {
            "exists",
            "isfile",
            "isdir",
        }:
            return True
        if isinstance(func, ast.Name) and func.id in {"exists", "isfile", "isdir"}:
            return True
    return False


def _is_tautological_assert(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value) is True
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
    ):
        left = ast.dump(node.left, include_attributes=False)
        right = ast.dump(node.comparators[0], include_attributes=False)
        if left == right and isinstance(node.ops[0], (ast.Eq, ast.Is)):
            return True
        if (
            isinstance(node.left, ast.Constant)
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.ops[0], (ast.Eq, ast.Is))
            and node.left.value == node.comparators[0].value
        ):
            return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return False
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_is_tautological_assert(value) for value in node.values)
    return False
