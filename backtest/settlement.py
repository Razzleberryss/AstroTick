"""Settlement of a KXBTC15M market.

The contract pays YES when the 60-second average is greater than or equal
to the strike. A tie at that average is YES. ``expiration_value`` is that
average when the file has it. The result string is only a fallback.
"""

from __future__ import annotations


def resolves_yes(
    expiration_value: float | None,
    floor_strike: float | None,
    result: str | None = None,
) -> bool | None:
    """Return True for YES, False for NO, None when the file cannot say."""
    if expiration_value is not None and floor_strike is not None:
        return float(expiration_value) >= float(floor_strike)
    label = (result or "").strip().lower()
    if label in ("yes", "no"):
        return label == "yes"
    return None
