from medium_cli.cli import main
from medium_cli.formatting import format_summary
from medium_cli.store import TaskStore


def test_store_summary_counts_total_and_completed():
    store = TaskStore()
    store.add("write docs", completed=True)
    store.add("ship feature", completed=False)
    store.add("close ticket", completed=True)

    assert store.summary() == (3, 2)


def test_format_summary_uses_compact_text():
    assert format_summary(total=3, completed=2) == "3 tasks, 2 complete"


def test_summary_command_prints_summary(capsys):
    assert main(["summary"]) == 0
    assert capsys.readouterr().out.strip() == "3 tasks, 2 complete"
