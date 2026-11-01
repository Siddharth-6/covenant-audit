"""The single definition of every arithmetic operation used in a covenant calculation.

The evaluator uses it to compute, and the result validator uses it to re-check a stored
trace, so the two cannot drift apart. All arithmetic is `decimal.Decimal` under an
explicit context; nothing is converted to float.
"""

from __future__ import annotations

from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from functools import reduce
from typing import Literal, Sequence

RoundingMode = Literal["ROUND_HALF_UP", "ROUND_HALF_EVEN", "ROUND_DOWN", "ROUND_UP"]
_MODES = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_UP": ROUND_UP,
}

# 50 significant digits is far beyond any financial input; traps turn silent NaN/Inf
# results into exceptions instead of letting them propagate into a ratio.
CALC_CONTEXT = Context(
    prec=50,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


class DivisionByZeroError(ArithmeticError):
    """Raised for x / 0, including 0 / 0."""


def apply_op(op: str, operands: Sequence[Decimal]) -> Decimal:
    """Apply one AST operation. `term` is the identity (a named alias of its formula)."""
    ctx = CALC_CONTEXT
    if op == "sum":
        return reduce(ctx.add, operands)
    if op == "diff":
        _arity(op, operands, 2)
        return ctx.subtract(operands[0], operands[1])
    if op == "mul":
        _arity(op, operands, 2)
        return ctx.multiply(operands[0], operands[1])
    if op == "div":
        _arity(op, operands, 2)
        if operands[1] == 0:
            raise DivisionByZeroError("division by zero")
        return ctx.divide(operands[0], operands[1])
    if op == "min":
        return min(operands)
    if op == "max":
        return max(operands)
    if op == "term":
        _arity(op, operands, 1)
        return operands[0]
    raise ValueError(f"unsupported operation '{op}'")


def _arity(op: str, operands: Sequence[Decimal], n: int) -> None:
    if len(operands) != n:
        raise ValueError(f"operation '{op}' takes {n} operand(s), got {len(operands)}")


def divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    return apply_op("div", (numerator, denominator))


def round_ratio(ratio: Decimal, places: int | None, mode: RoundingMode) -> Decimal:
    """Apply the contract's rounding rule. `places=None` means the contract specifies none."""
    if places is None:
        return ratio
    return ratio.quantize(Decimal(1).scaleb(-places), rounding=_MODES[mode], context=CALC_CONTEXT)
