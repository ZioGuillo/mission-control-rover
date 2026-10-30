"""Exact arithmetic for homework: gpt-oss said 7 x 8 = 64, then told Lucca 54 was right."""
from __future__ import annotations

import ast
import operator
from fractions import Fraction

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


class CalcError(ValueError):
    pass


def _eval(node: ast.AST) -> Fraction:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Fraction(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (right.denominator != 1 or abs(right) > 12):
            raise CalcError("potencia demasiado grande")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise CalcError("división entre cero")
        return Fraction(_OPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise CalcError("solo números y + - * / // % ** con paréntesis")


def calculate(expression: str) -> dict:
    """{"resultado": ...} for an arithmetic expression ("7*8", "123-47", "81/9"), never eval()."""
    text = expression.replace("×", "*").replace("÷", "/").replace("x", "*").replace(",", ".").strip()
    if len(text) > 100:
        raise CalcError("expresión demasiado larga")
    try:
        value = _eval(ast.parse(text, mode="eval").body)
    except SyntaxError as e:
        raise CalcError("no es una cuenta") from e
    if value.denominator == 1:
        return {"expresion": expression, "resultado": int(value)}
    whole, rest = divmod(value.numerator, value.denominator)
    return {"expresion": expression, "resultado": round(float(value), 6),
            "cociente": whole, "residuo": rest}
