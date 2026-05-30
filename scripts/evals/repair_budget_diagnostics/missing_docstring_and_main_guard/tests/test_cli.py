from small_cli.cli import build_parser, format_message, main


def test_format_message_returns_message_by_default():
    assert format_message("hello") == "hello"


def test_parser_accepts_message():
    args = build_parser().parse_args(["hello"])
    assert args.message == "hello"


def test_cli_prints_message(capsys):
    assert main(["hello"]) == 0
    assert capsys.readouterr().out.strip() == "hello"


def test_uppercase_option_prints_uppercase_message(capsys):
    assert main(["--uppercase", "hello"]) == 0
    assert capsys.readouterr().out.strip() == "HELLO"
