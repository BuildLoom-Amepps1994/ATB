"""Exact, bounded money arithmetic for the micro-trading accounting boundary.

The legacy ledger (``positions``, ``trades``, ``paper_cash``) stores binary
floats. The micro-trading cycle records keep every amount as a decimal string
parsed and computed here, and only convert to float where they must update a
legacy table. Converting a float that already lost digits cannot recover them:
:func:`parse` accepts a float through its shortest ``repr`` and callers that
pass one say so in the field name or the record's provenance.

Every parse is bounded. A value is refused when it is a bool, None (missing is
never zero), not finite, longer than ``MAX_TEXT`` characters, has an exponent
outside ``MIN_EXPONENT..MAX_EXPONENT`` or a magnitude above ``MAX_ABS``, so a
hostile or corrupt exchange field cannot force unbounded formatting or memory
use. Quantization happens only at exchange and reporting boundaries, through
:func:`q_down` / :func:`q_up` with an explicit step.
"""
from __future__ import annotations

import re
from decimal import (ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, DivisionByZero,
                     Inexact, InvalidOperation, Overflow)

# 34 significant digits (IEEE decimal128) is far more than any venue precision.
CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN, Emax=99, Emin=-99,
                  traps=[InvalidOperation, DivisionByZero, Overflow])
MAX_ABS = Decimal('1e15')          # no amount, price or quantity here is near a quadrillion
MAX_TEXT = 64
MIN_EXPONENT, MAX_EXPONENT = -30, 15
ZERO, ONE = Decimal(0), Decimal(1)
BPS = Decimal(10000)

_NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?')


class MoneyError(ValueError):
    """A value that cannot enter the money boundary. The message names the field only."""


def parse(value, *, field: str = 'value', allow_zero: bool = True, allow_negative: bool = False,
          max_abs: Decimal = MAX_ABS) -> Decimal:
    """Parse ``value`` into a bounded finite Decimal or raise :class:`MoneyError`."""
    if value is None:
        raise MoneyError(f'{field} is missing')
    if isinstance(value, bool):
        raise MoneyError(f'{field} must be a number, not a boolean')
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        if abs(value) > int(max_abs):
            raise MoneyError(f'{field} is out of range')
        number = Decimal(value)
    elif isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise MoneyError(f'{field} must be finite')
        # repr is the shortest string that round-trips this float; it adds no
        # binary noise, but it cannot restore digits the float already lost.
        # float's own repr, so a subclass's (NumPy's 'np.float64(...)') is not parsed.
        try:
            number = Decimal(float.__repr__(value))
        except (InvalidOperation, ValueError):
            raise MoneyError(f'{field} is not a valid number') from None
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > MAX_TEXT or not _NUMBER.fullmatch(text):
            raise MoneyError(f'{field} is not a plain decimal number')
        try:
            number = Decimal(text)     # exact: the text is short and plain, checked above
        except (InvalidOperation, ValueError, OverflowError):
            raise MoneyError(f'{field} is not a valid number') from None
    else:
        raise MoneyError(f'{field} has an unsupported type')
    if not number.is_finite():
        raise MoneyError(f'{field} must be finite')
    if number != 0:
        exponent = number.adjusted()
        if exponent > MAX_EXPONENT or exponent < MIN_EXPONENT:
            raise MoneyError(f'{field} is out of range')
    if number.copy_abs() > max_abs:          # exact: abs() would round to the caller's decimal context
        raise MoneyError(f'{field} is out of range')
    if number == 0:
        if not allow_zero:
            raise MoneyError(f'{field} must not be zero')
        return ZERO
    if number < 0 and not allow_negative:
        raise MoneyError(f'{field} must not be negative')
    return number


def parse_optional(value, **kwargs) -> Decimal | None:
    """None stays None (unknown); anything else must parse."""
    return None if value is None else parse(value, **kwargs)


def positive(value, *, field: str = 'value') -> Decimal:
    return parse(value, field=field, allow_zero=False)


def _check_step(step: Decimal) -> Decimal:
    if not isinstance(step, Decimal) or not step.is_finite() or step <= 0:
        raise MoneyError('quantization step must be a positive finite Decimal')
    return step


# The quotient of a step rounding is taken at 200 digits, so a 34-digit value with a step that is not a
# power of ten, or a parsed value longer than 34 digits, cannot round across a step boundary first.
_QUOTIENT = Context(prec=200, rounding=ROUND_HALF_EVEN, Emax=999, Emin=-999,
                    traps=[InvalidOperation, DivisionByZero, Overflow])


# A multiple of the step is formed exactly or not at all: when the multiple next to the value needs more than
# 34 digits (a step finer than the value's precision), rounding the product would return a number that is not
# a multiple of the step, so the pair is refused. One correction covers a 200-digit quotient that rounded across
# a boundary; the second is spare, and none can loop.
_EXACT = Context(prec=34, rounding=ROUND_HALF_EVEN, Emax=99, Emin=-99,
                 traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])
_MAX_CORRECTIONS = 2


def _multiple(units: Decimal, step: Decimal) -> Decimal:
    try:
        return _EXACT.multiply(units, step)
    except Inexact:
        raise MoneyError('quantization step too fine for this value at 34 digits') from None


def q_down(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of ``step`` that is not above ``value``."""
    step = _check_step(step)
    units = _QUOTIENT.divide(value, step).to_integral_value(rounding=ROUND_FLOOR)
    for _ in range(_MAX_CORRECTIONS + 1):
        result = _multiple(units, step)
        if result <= value:
            return result
        units = _QUOTIENT.subtract(units, ONE)        # never return more than was given
    raise MoneyError('quantization step too fine for this value at 34 digits')


def q_up(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of ``step`` that is not below ``value``."""
    step = _check_step(step)
    units = _QUOTIENT.divide(value, step).to_integral_value(rounding=ROUND_CEILING)
    for _ in range(_MAX_CORRECTIONS + 1):
        result = _multiple(units, step)
        if result >= value:
            return result
        units = _QUOTIENT.add(units, ONE)             # never return less than was asked
    raise MoneyError('quantization step too fine for this value at 34 digits')


def add(*values: Decimal) -> Decimal:
    total = ZERO
    for value in values:
        total = CONTEXT.add(total, value)
    return total


def sub(a: Decimal, b: Decimal) -> Decimal:
    return CONTEXT.subtract(a, b)


def mul(*values: Decimal) -> Decimal:
    total = ONE
    for value in values:
        total = CONTEXT.multiply(total, value)
    return total


def div(a: Decimal, b: Decimal) -> Decimal:
    if b == 0:
        raise MoneyError('division by zero')
    return CONTEXT.divide(a, b)


def bps(value: Decimal) -> Decimal:
    """A rate in basis points as a fraction (25 -> 0.0025)."""
    return CONTEXT.divide(value, BPS)


def text(value: Decimal | None) -> str | None:
    """Canonical storage form: plain notation, no exponent, trailing zeros kept as computed."""
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MoneyError('only finite Decimals are stored')
    if value == 0:
        return '0'
    return format(value, 'f')


def from_text(value, *, field: str = 'stored value', allow_negative: bool = True) -> Decimal | None:
    """Read a stored decimal string back; None stays None."""
    if value is None:
        return None
    return parse(str(value), field=field, allow_negative=allow_negative)


def to_float(value: Decimal | None) -> float | None:
    """For the legacy float ledger only; the Decimal remains the authoritative figure."""
    return None if value is None else float(value)


def display(value: Decimal | None, places: int = 4, *, signed: bool = False) -> str:
    """Human display that never hides a sign or a non-zero amount behind rounding.

    ``display(Decimal('-0.00003'))`` is ``'-0.00003'``, not ``'0.0000'``:
    when the value rounds to zero at ``places`` the precision grows (up to 8
    places) until a significant digit shows, and a value smaller than that is
    shown as ``'<0.00000001'`` with its sign.
    """
    if value is None:
        return 'unknown'
    if not isinstance(value, Decimal):
        value = parse(value, field='display value', allow_negative=True)
    places = max(0, min(8, int(places)))
    shown_places = places
    rounded = value.quantize(Decimal(1).scaleb(-shown_places), rounding=ROUND_HALF_EVEN, context=CONTEXT)
    while value != 0 and rounded == 0 and shown_places < 8:
        shown_places += 1
        rounded = value.quantize(Decimal(1).scaleb(-shown_places), rounding=ROUND_HALF_EVEN, context=CONTEXT)
    if value != 0 and rounded == 0:
        return ('-' if value < 0 else ('+' if signed else '')) + '<0.00000001'
    body = format(abs(rounded), 'f')
    if value < 0:
        return '-' + body
    return ('+' if signed and value > 0 else '') + body


__all__ = ['MoneyError', 'parse', 'parse_optional', 'positive', 'q_down', 'q_up', 'add', 'sub', 'mul', 'div',
           'bps', 'text', 'from_text', 'to_float', 'display', 'CONTEXT', 'ZERO', 'ONE', 'BPS']
