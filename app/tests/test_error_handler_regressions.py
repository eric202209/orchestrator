from app.services.error_handler import EnhancedErrorHandler
from app.services.orchestration.planning.planner import PlannerService


def test_attempt_json_parsing_recovers_qwen_broken_shell_quotes_and_localhost_links():
    handler = EnhancedErrorHandler()
    broken = """[{"step_number": 2, "description": "create_backend_skeleton", "commands": ["python3 -m venv .venv"], "verification": ".venv/bin/python -c 'from app.main import app; from app.config import Settings; print("backend imports OK")'", "rollback": "rm -rf .venv", "expected_files": ["app/main.py"]}, {"step_number": 3, "description": "wire_api_config", "commands": ["grep -n 'localhost:8080' frontend/vite.config.ts"], "verification": ".venv/bin/python -c 'from app.config import Settings; s=Settings(); assert "[](http://localhost:3000)<http://localhost:3000>" in s.CORS_ORIGINS; print("cors aligned")'", "rollback": null, "expected_files": ["frontend/vite.config.ts"]}]"""

    success, parsed, strategy = handler.attempt_json_parsing(broken, context="planning")

    assert success is True
    assert isinstance(parsed, list)
    assert parsed[0]["step_number"] == 2
    assert 'print("backend imports OK")' in parsed[0]["verification"]
    assert '"http://localhost:3000"' in parsed[1]["verification"]
    assert "Fixed common errors" in strategy or "Found and fixed JSON" in strategy


def test_attempt_json_parsing_accepts_valid_write_file_content_json_without_repair():
    handler = EnhancedErrorHandler()
    valid = """[
      {
        "step_number": 1,
        "description": "Create script",
        "commands": [],
        "verification": "python -m py_compile csv_summary.py",
        "rollback": "rm -f csv_summary.py",
        "expected_files": ["csv_summary.py"],
        "ops": [
          {
            "op": "write_file",
            "path": "csv_summary.py",
            "content": "ERR = {\\"error\\": \\"File not found\\"}\\n"
          }
        ]
      }
    ]"""

    success, parsed, strategy = handler.attempt_json_parsing(valid, context="planning")

    assert success is True
    assert strategy == ""
    assert parsed[0]["ops"][0]["content"] == 'ERR = {"error": "File not found"}\n'


def test_attempt_json_parsing_repairs_unescaped_write_file_content_json():
    handler = EnhancedErrorHandler()
    broken = """[
      {
        "step_number": 1,
        "description": "Create script",
        "commands": [],
        "verification": "python -m py_compile csv_summary.py",
        "rollback": "rm -f csv_summary.py",
        "expected_files": ["csv_summary.py"],
        "ops": [
          {
            "op": "write_file",
            "path": "csv_summary.py",
            "content": "ERR = {"error": "File not found"}\\n"
          }
        ]
      }
    ]"""

    success, parsed, strategy = handler.attempt_json_parsing(broken, context="planning")

    assert success is True
    assert isinstance(parsed, list)
    assert parsed[0]["step_number"] == 1
    assert parsed[0]["commands"] == []
    assert parsed[0]["ops"][0]["content"] == 'ERR = {"error": "File not found"}\n'
    assert strategy == "Fixed common errors"


def test_attempt_json_parsing_repairs_fenced_unescaped_write_file_content_json():
    handler = EnhancedErrorHandler()
    broken = """```json
[
  {
    "step_number": 1,
    "description": "Create script",
    "commands": [],
    "verification": "python -m py_compile csv_summary.py",
    "rollback": "rm -f csv_summary.py",
    "expected_files": ["csv_summary.py"],
    "ops": [
      {
        "op": "write_file",
        "path": "csv_summary.py",
        "content": "ERR = {"error": "File not found"}\\n"
      }
    ]
  }
]
```"""

    success, parsed, strategy = handler.attempt_json_parsing(broken, context="planning")

    assert success is True
    assert isinstance(parsed, list)
    assert parsed[0]["step_number"] == 1
    assert parsed[0]["ops"][0]["content"] == 'ERR = {"error": "File not found"}\n'
    assert strategy == "Cleaned markdown fences and fixed common errors"


def test_attempt_json_parsing_repairs_unescaped_content_json_with_arrays():
    handler = EnhancedErrorHandler()
    broken = """[
      {
        "step_number": 1,
        "description": "Create CSV summary fixture",
        "commands": [],
        "verification": "test -s summary.json",
        "rollback": "rm -f summary.json",
        "expected_files": ["summary.json"],
        "ops": [
          {
            "op": "write_file",
            "path": "summary.json",
            "content": "{"row_count": 3, "column_count": 3, "headers": ["name", "age", "city"], "sample": [["Alice", "30", "New York"], ["Bob", "", "London"]]}\\n"
          }
        ]
      }
    ]"""

    success, parsed, strategy = handler.attempt_json_parsing(broken, context="planning")

    assert success is True
    assert isinstance(parsed, list)
    assert parsed[0]["step_number"] == 1
    assert '"headers": ["name", "age", "city"]' in parsed[0]["ops"][0]["content"]
    assert '"Bob", "", "London"' in parsed[0]["ops"][0]["content"]
    assert strategy == "Fixed common errors"


def test_attempt_json_parsing_does_not_return_nested_content_object_for_plan_source():
    handler = EnhancedErrorHandler()
    broken = """[
      {
        "step_number": 1,
        "description": "Create CSV summary fixture",
        "commands": [],
        "ops": [
          {
            "op": "write_file",
            "path": "summary.json",
            "content": "{"row_count": 3, "column_count": 3, "headers": ["name", "age"]}\\n"
          }
        ]
      }
    ]"""

    success, parsed, _strategy = handler.attempt_json_parsing(
        broken, context="planning"
    )

    assert success is True
    assert isinstance(parsed, list)
    assert parsed[0]["step_number"] == 1


def test_attempt_json_parsing_rejects_nested_step_from_malformed_plan_array():
    handler = EnhancedErrorHandler()
    broken = """[
      {
        "step_number": 1,
        "description": "Inspect the current workspace",
        "commands": ["rg --files . | sort"],
        "verification": "python -c \\"import sys; sys.exit(0)\\"",
        "rollback": null,
        "expected_files": []
      },
      {
        "step_number": 2,
        "description": "Add the --uppercase option",
        "ops": [
          {
            "op": "replace_in_file",
            "path": "src/small_cli/cli.py",
            "old": "def main(argv=None):",
            "new": "def main(argv=None):\\n    parser.add_argument("--uppercase", action="store_true")"
          }
        ],
        "commands": [],
        "verification": "python -m pytest -q",
        "rollback": null,
        "expected_files": ["src/small_cli/cli.py"]
      }
    ]"""

    success, parsed, strategy = handler.attempt_json_parsing(broken, context="planning")

    assert success is False
    assert parsed is None
    assert strategy == "Failed to parse planning"


def test_attempt_json_parsing_recovers_final_fenced_plan_from_prose_response():
    handler = EnhancedErrorHandler()
    response = """To fulfill the requirement, use these steps:

```json
{
  "step": "create_file",
  "file": "README.md",
  "content": "# Project Title\\n\\n**Status:** In progress."
}
```

Final Answer:

```json
[
  {
    "step": "create_file",
    "file": "README.md",
    "content": "# Project Title\\n\\nThis is the project description.\\n\\n**Status:** In progress."
  },
  {
    "step": "verify_file",
    "file": "README.md",
    "expected_content": "Status"
  }
]
```
"""

    success, parsed, strategy = handler.attempt_json_parsing(
        response, context="planning"
    )
    normalized = PlannerService.sanitize_common_plan_issues(parsed)

    assert success is True
    assert strategy == "Extracted from mixed content"
    assert isinstance(parsed, list)
    assert parsed[0]["step"] == "create_file"
    assert normalized[0]["ops"][0]["op"] == "write_file"
    assert normalized[0]["expected_files"] == ["README.md"]
    assert normalized[1]["commands"] == [normalized[1]["verification"]]
    assert "Status" in normalized[1]["verification"]


def test_attempt_json_parsing_prefers_last_plan_shaped_fence():
    handler = EnhancedErrorHandler()
    response = """Draft:

```json
{"step": "create_file", "file": "draft.md", "content": "draft"}
```

Final:

```json
[
  {"step": "create_file", "file": "README.md", "content": "final"}
]
```
"""

    success, parsed, strategy = handler.attempt_json_parsing(
        response, context="planning"
    )

    assert success is True
    assert strategy == "Extracted from mixed content"
    assert isinstance(parsed, list)
    assert parsed[0]["file"] == "README.md"


def test_attempt_json_parsing_does_not_treat_path_only_metadata_as_plan_shape():
    handler = EnhancedErrorHandler()
    response = """```json
{"path": "/tmp/output", "size": 1024}
```

```json
{"summary": "not a plan but longer than the path metadata block"}
```
"""

    success, parsed, strategy = handler.attempt_json_parsing(
        response, context="planning"
    )

    assert success is True
    assert strategy == "Extracted from mixed content"
    assert parsed == {"summary": "not a plan but longer than the path metadata block"}


def test_attempt_json_parsing_prefers_longest_parseable_fence_without_plan_shape():
    handler = EnhancedErrorHandler()
    response = """```json
{"summary": "short"}
```

```json
{"result": {"files": ["README.md"], "notes": "longer parseable metadata block"}}
```

```json
{"ok": true}
```
"""

    success, parsed, strategy = handler.attempt_json_parsing(
        response, context="planning"
    )

    assert success is True
    assert strategy == "Extracted from mixed content"
    assert parsed == {
        "result": {
            "files": ["README.md"],
            "notes": "longer parseable metadata block",
        }
    }


def test_attempt_json_parsing_fallback_handles_deeply_nested_json():
    handler = EnhancedErrorHandler()
    response = (
        'prefix {"step_number": 1, "commands": ["echo ok"], '
        '"verification": {"outer": {"inner": {"check": "ok"}}}} suffix'
    )

    success, parsed, strategy = handler.attempt_json_parsing(
        response, context="planning"
    )

    assert success is True
    assert strategy == "Extracted from mixed content"
    assert parsed == {
        "step_number": 1,
        "commands": ["echo ok"],
        "verification": {"outer": {"inner": {"check": "ok"}}},
    }
