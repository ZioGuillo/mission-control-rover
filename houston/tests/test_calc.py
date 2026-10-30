import pytest

from houston.calc import CalcError, calculate


@pytest.mark.parametrize("expr, result", [
    ("7*8", 56), ("7 x 8", 56), ("7×8", 56), ("123-47", 76), ("81/9", 9), ("12*11", 132),
    ("(3+4)*2", 14), ("2**3", 8), ("-5+2", -3)])
def test_exact_results(expr, result):
    assert calculate(expr)["resultado"] == result


def test_division_with_remainder():
    r = calculate("17/5")
    assert r["resultado"] == 3.4 and r["cociente"] == 3 and r["residuo"] == 2


@pytest.mark.parametrize("expr", ["__import__('os')", "open('x')", "1/0", "9**999", "abc", "1" * 101])
def test_anything_but_arithmetic_is_refused(expr):
    with pytest.raises(CalcError):
        calculate(expr)
