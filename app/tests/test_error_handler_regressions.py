from app.services.error_handler import EnhancedErrorHandler


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
