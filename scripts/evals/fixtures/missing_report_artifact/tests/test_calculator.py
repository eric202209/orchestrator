from report_artifact import add, multiply, subtract


def test_adds_two_numbers():
    assert add(2, 3) == 5


def test_multiplies_two_numbers():
    assert multiply(4, 5) == 20


def test_subtracts_two_numbers():
    assert subtract(8, 3) == 5
