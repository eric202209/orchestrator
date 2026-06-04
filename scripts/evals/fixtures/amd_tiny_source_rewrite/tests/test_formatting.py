from amd_tiny import format_label


def test_format_label_title_cases_words():
    assert format_label("hello world") == "Hello World"


def test_format_label_trims_and_collapses_whitespace():
    assert format_label("  multi   word\tlabel  ") == "Multi Word Label"


def test_format_label_preserves_empty_input_as_empty_string():
    assert format_label("   ") == ""
