from medium_cli.cli import build_parser, main


def test_parser_accepts_list_command():
    args = build_parser().parse_args(["list"])
    assert args.command == "list"


def test_cli_lists_tasks(capsys):
    assert main(["list"]) == 0
    output = capsys.readouterr().out.splitlines()
    assert output == ["write docs", "ship feature", "close ticket"]
