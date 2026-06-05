from tiny_slug import slugify


def test_slugify_lowercases_and_removes_punctuation():
    assert slugify(" Hello, World! ") == "hello-world"


def test_slugify_collapses_mixed_whitespace():
    assert slugify("multi   space\tlabel") == "multi-space-label"


def test_slugify_collapses_existing_separators():
    assert slugify("Already---Slug") == "already-slug"
