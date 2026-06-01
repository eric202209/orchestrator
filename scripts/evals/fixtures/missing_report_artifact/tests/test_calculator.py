from report_artifact import add, multiply


def test_adds_two_numbers():
    assert add(2, 3) == 5


def test_multiplies_two_numbers():
    assert multiply(4, 5) == 20
