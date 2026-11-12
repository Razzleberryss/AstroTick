"""Shard balance parsing.

KXBTC15M clears on the crypto exchange shard (``exchange_index`` 2 in the
live API; the index is still a setting). The aggregate ``/portfolio/balance``
figure does not prove that shard is funded — the two balance endpoints have
been observed to disagree — so an unscoped payload returns None and the
runner refuses to send orders.

A zero balance is a real answer (the shard pair can exist before it is
funded) and is not treated as "unknown".
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _rows(payload) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "subaccount_balances",
        "balances",
        "balance_breakdown",
        "exchange_balances",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _row_balance(row: dict) -> Decimal | None:
    for key in ("balance", "balance_dollars", "available_balance_dollars", "available_balance"):
        if key in row and row[key] is not None:
            parsed = _decimal(row[key])
            if parsed is None:
                continue
            # Integer cents only when the field is the legacy name and the
            # value looks like a whole number of cents (no decimal point).
            if key == "available_balance" and "." not in str(row[key]):
                return parsed / Decimal("100")
            return parsed
    return None


def primary_shard_balance(
    payload,
    exchange_index: int,
    *,
    subaccount: int = 0,
    request_was_scoped: bool = False,
) -> Decimal | None:
    """Balance of ``subaccount`` on ``exchange_index``.

    None means the payload does not identify that shard. Decimal('0') means
    the shard was identified and is empty.
    """
    rows = _rows(payload)
    matched: list[Decimal] = []
    saw_indexed_row = False
    for row in rows:
        if "exchange_index" not in row and "exchangeIndex" not in row:
            continue
        saw_indexed_row = True
        index = row.get("exchange_index", row.get("exchangeIndex"))
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        sub = row.get("subaccount_number", row.get("subaccount", subaccount))
        try:
            sub = int(sub)
        except (TypeError, ValueError):
            sub = subaccount
        if index != int(exchange_index) or sub != int(subaccount):
            continue
        amount = _row_balance(row)
        if amount is not None:
            matched.append(amount)
    if matched:
        return sum(matched, Decimal("0"))
    if saw_indexed_row:
        # The account listed shards and this one was not among them.
        return Decimal("0")
    if request_was_scoped and isinstance(payload, dict):
        for key in ("balance_dollars", "balance", "available_balance_dollars"):
            if key in payload:
                amount = _decimal(payload[key])
                if amount is None:
                    return None
                if key == "balance" and "." not in str(payload[key]):
                    return amount / Decimal("100")
                return amount
    return None


def allow_trading(balance: Decimal | None, minimum: Decimal) -> tuple[bool, str]:
    if balance is None:
        return False, "shard_balance_unknown"
    if balance < minimum:
        return False, "shard_unfunded"
    return True, "ok"
