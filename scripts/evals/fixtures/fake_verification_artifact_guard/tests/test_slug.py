from verification_guard import slugify


def test_slugify_removes_punctuation_and_collapses_spaces():
    assert slugify(" Hello,   World! ") == "hello-world"


def test_slugify_handles_mixed_separators():
    assert slugify("Phase_12B: Truthfulness") == "phase-12b-truthfulness"
