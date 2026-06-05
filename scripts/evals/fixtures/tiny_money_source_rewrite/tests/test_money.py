from tiny_money import format_cents


def test_format_cents_positive_amount():
    assert format_cents(1234) == "$12.34"


def test_format_cents_zero_pads_cents():
    assert format_cents(105) == "$1.05"


def test_format_cents_handles_zero():
    assert format_cents(0) == "$0.00"


def test_format_cents_handles_negative_amount():
    assert format_cents(-5) == "-$0.05"
