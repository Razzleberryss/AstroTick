"""Find the KXBTC15M window by close time, with an events fallback.

``status=open`` has returned an empty list while markets were still
``initialized`` and trading. Close-time filters are also incompatible with
``status=open`` on the current API, so discovery does not send that status.
The horizon is configurable because the listing lead time is only observed.
"""

from __future__ import annotations

from datetime import UTC, datetime

TRADEABLE_STATUSES = frozenset({"", "open", "active", "initialized", "unopened"})
DEAD_STATUSES = frozenset(
    {"settled", "finalized", "determined", "closed", "inactive", "disputed", "amended"}
)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def markets_from_events(events: list[dict]) -> list[dict]:
    found: list[dict] = []
    for event in events or []:
        nested = event.get("markets") or []
        if nested:
            found.extend(item for item in nested if isinstance(item, dict))
            continue
        # Some payloads only carry the event ticker. Callers that need the
        # market still have the event for a second lookup.
        if event.get("event_ticker") or event.get("ticker"):
            found.append({"event": event, "ticker": event.get("event_ticker") or event.get("ticker")})
    return found


def select_markets(
    markets: list[dict],
    *,
    now: datetime,
    series: str,
    horizon_seconds: float,
    require_opened: bool = True,
) -> list[dict]:
    """Markets in this series whose close is in (now, now + horizon]."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    chosen: list[dict] = []
    prefix = f"{series}-"
    for market in markets or []:
        ticker = str(market.get("ticker") or "")
        if ticker and not ticker.startswith(prefix):
            # Event-only fallback rows are not markets.
            if "event" in market and "close_time" not in market:
                continue
            if not ticker.startswith(prefix):
                continue
        status = str(market.get("status") or "").lower()
        if status in DEAD_STATUSES:
            continue
        if status and status not in TRADEABLE_STATUSES:
            continue
        close = _parse_time(market.get("close_time"))
        if close is None:
            continue
        remaining = (close - now).total_seconds()
        if remaining <= 0 or remaining > horizon_seconds:
            continue
        if require_opened:
            opened = _parse_time(market.get("open_time"))
            if opened is not None and opened > now:
                continue
        chosen.append(market)
    chosen.sort(key=lambda item: item.get("close_time") or "")
    return chosen


def discover(
    primary: list[dict] | None,
    events_fallback: list[dict] | None,
    *,
    now: datetime,
    series: str,
    horizon_seconds: float,
) -> tuple[list[dict], str]:
    """Prefer the close-window market query. Use events only when it is empty."""
    selected = select_markets(
        primary or [],
        now=now,
        series=series,
        horizon_seconds=horizon_seconds,
    )
    if selected:
        return selected, "close_window"
    flattened = markets_from_events(events_fallback or [])
    selected = select_markets(
        flattened,
        now=now,
        series=series,
        horizon_seconds=horizon_seconds,
        require_opened=False,
    )
    if selected:
        return selected, "events_fallback"
    return [], "none"
