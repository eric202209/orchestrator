from import_repair import normalize_greeting


def test_normalize_greeting_trims_and_title_cases_name():
    assert normalize_greeting("  ada   lovelace ") == "Hello, Ada Lovelace!"


def test_normalize_greeting_handles_single_name():
    assert normalize_greeting("grace") == "Hello, Grace!"
