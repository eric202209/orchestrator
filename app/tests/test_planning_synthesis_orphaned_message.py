from app.services.agents.openclaw_service import OpenClawSessionService


def test_planning_retry_session_ids_are_unique_within_one_second(monkeypatch):
    service = object.__new__(OpenClawSessionService)
    monkeypatch.setattr(service, "_resolve_openclaw_command", lambda: ["openclaw"])
    monkeypatch.setattr(service, "_resolve_execution_cwd", lambda: "/tmp/planning")
    monkeypatch.setattr(
        service,
        "_build_openclaw_agent_command",
        lambda command, cwd: [*command, "agent"],
    )
    monkeypatch.setattr("app.services.agents.openclaw_service.time.time", lambda: 100)

    first = service.build_cli_agent_command("first", session_prefix="planning")
    second = service.build_cli_agent_command("second", session_prefix="planning")

    first_id = first[first.index("--session-id") + 1]
    second_id = second[second.index("--session-id") + 1]
    assert first_id != second_id
