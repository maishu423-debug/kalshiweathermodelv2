"""
Live Kalshi trading — places real orders via the Kalshi REST API.
Mirrors enough of paper.py's interface so the algorithm views can drive it.

Pending-buy guard
-----------------
After a buy is placed, Kalshi's /portfolio/positions API can take several
seconds to reflect the new holding.  Without a guard the 5-second auto-trade
cycle would see "no position" and place a second identical buy.

_pending_buys holds an in-memory record of every buy we've placed for up to
PENDING_TTL_SEC seconds.  get_state() merges these into the returned positions
list so the algorithm treats them as held contracts and says "hold".

Once the real Kalshi position appears (or the TTL expires) the entry is dropped.
"""

import threading
import time

from .client import (
    KalshiClientError,
    get_portfolio_balance,
    get_portfolio_positions,
    place_kalshi_order,
)


class LiveTradeError(Exception):
    pass


# ── Pending-buy registry ──────────────────────────────────────────────────────

PENDING_TTL_SEC = 60   # treat a pending buy as "held" for up to 60 s

_pending_lock = threading.Lock()
_pending_buys: dict = {}   # ticker → position-shaped dict with "expires_at"


def _add_pending(ticker: str, side: str, price_cents: int, contracts: int):
    with _pending_lock:
        _pending_buys[ticker] = {
            "market_ticker":   ticker,
            "market_label":    ticker,
            "side":            side,
            "contracts":       contracts,
            "avg_price_cents": price_cents,
            "cost_basis_cents": contracts * price_cents,
            "expires_at":      time.time() + PENDING_TTL_SEC,
        }


def _remove_pending(ticker: str):
    with _pending_lock:
        _pending_buys.pop(ticker, None)


def _live_pending_positions(confirmed_tickers: set) -> list:
    """Return pending buys that haven't yet appeared in Kalshi's positions API."""
    now = time.time()
    result = []
    with _pending_lock:
        expired = [t for t, v in _pending_buys.items() if v["expires_at"] < now]
        for t in expired:
            del _pending_buys[t]
        for ticker, v in _pending_buys.items():
            if ticker not in confirmed_tickers:
                result.append({k: v[k] for k in v if k != "expires_at"})
    return result


def _has_pending(ticker: str) -> bool:
    with _pending_lock:
        v = _pending_buys.get(ticker)
        if v is None:
            return False
        if v["expires_at"] < time.time():
            del _pending_buys[ticker]
            return False
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_count(value) -> int:
    """V2 API returns count as a string like '10.00' — normalise to int."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _yes_price(side: str, price_cents: int) -> int:
    if side == "yes":
        return int(price_cents)
    return 100 - int(price_cents)


def _normalize_positions(data: dict) -> list:
    # Kalshi V2 response shape (confirmed from live API):
    #   market_positions[].ticker          (not "market_ticker")
    #   market_positions[].position_fp     decimal string e.g. "27.56"  (positive=YES, negative=NO)
    #   event_positions[].total_cost_dollars / total_cost_shares_fp  → avg buy price

    # Build event-level avg buy price (cents) from event_positions
    event_avg_cents: dict = {}
    for ep in data.get("event_positions", []):
        ev = ep.get("event_ticker", "")
        try:
            cost   = float(ep.get("total_cost_dollars",   0) or 0)
            shares = float(ep.get("total_cost_shares_fp", 0) or 0)
            if shares > 0:
                event_avg_cents[ev] = int(round(cost / shares * 100))
        except (TypeError, ValueError):
            pass

    positions = []
    for p in data.get("market_positions", []):
        # V2 uses "ticker" not "market_ticker"
        ticker = p.get("ticker") or p.get("market_ticker", "")
        if not ticker:
            continue

        # V2 uses "position_fp" (decimal string); fallback to legacy int "position"
        raw = p.get("position_fp") or p.get("position", 0)
        try:
            count = float(raw)
        except (TypeError, ValueError):
            count = 0.0

        if count == 0:
            continue

        # Positive = YES contracts, negative = NO contracts
        side      = "yes" if count >= 0 else "no"
        contracts = abs(count)

        # Derive event ticker: "KXHIGHMIA-26JUN24-B92.5" → "KXHIGHMIA-26JUN24"
        parts        = ticker.rsplit("-", 1)
        event_ticker = parts[0] if len(parts) == 2 else ""
        avg_price    = event_avg_cents.get(event_ticker, 50)

        positions.append({
            "market_ticker":    ticker,
            "market_label":     ticker,
            "side":             side,
            "contracts":        int(round(contracts)),
            "avg_price_cents":  avg_price,
            "cost_basis_cents": int(round(contracts * avg_price)),
        })
    return positions


# ── Public API ────────────────────────────────────────────────────────────────

def get_state() -> dict:
    """
    Return live Kalshi portfolio state shaped like paper.py's get_state().
    Includes any pending (recently placed but not yet confirmed) buys.
    """
    try:
        bal = get_portfolio_balance()
        balance_cents = int(bal.get("balance", 0) or 0)
    except KalshiClientError as exc:
        raise LiveTradeError(f"Could not fetch Kalshi balance: {exc}") from exc

    try:
        pos_data  = get_portfolio_positions()
        positions = _normalize_positions(pos_data)
    except KalshiClientError as exc:
        raise LiveTradeError(f"Could not fetch Kalshi positions: {exc}") from exc

    # Prefer pending-buy avg_price (exact price we paid) over Kalshi's estimate
    with _pending_lock:
        for pos in positions:
            pb = _pending_buys.get(pos["market_ticker"])
            if pb and pb["expires_at"] > time.time():
                pos["avg_price_cents"] = pb["avg_price_cents"]

    # Merge pending buys that haven't shown up in Kalshi's API yet
    confirmed = {p["market_ticker"] for p in positions}
    positions.extend(_live_pending_positions(confirmed))

    return {
        "account": {
            "cash_cents":          balance_cents,
            "starting_cash_cents": None,
            "total_profit_cents":  None,
            "total_loss_cents":    None,
        },
        "positions": positions,
        "trades":    [],
    }


def place_live_buy(ticker: str, side: str, price_cents: int,
                   dollars_cents: int, market_label: str = "") -> dict:
    """
    Buy contracts on Kalshi.
    Raises LiveTradeError if:
      - a buy for this ticker is already pending (duplicate-buy guard)
      - account balance is insufficient
    Returns {"trade": ..., "state": ...}.
    """
    # ── Duplicate-buy guard ───────────────────────────────────────────────
    if _has_pending(ticker):
        raise LiveTradeError(
            f"Buy already pending for {ticker} — waiting for position to settle "
            f"(up to {PENDING_TTL_SEC}s)."
        )

    count      = max(1, int(dollars_cents) // int(price_cents))
    cost_cents = count * int(price_cents)

    # ── Balance guard ─────────────────────────────────────────────────────
    try:
        bal           = get_portfolio_balance()
        balance_cents = int(bal.get("balance", 0) or 0)
    except KalshiClientError as exc:
        raise LiveTradeError(f"Cannot verify balance: {exc}") from exc

    if balance_cents < cost_cents:
        raise LiveTradeError(
            f"Insufficient balance: order costs ${cost_cents / 100:.2f} "
            f"but account only has ${balance_cents / 100:.2f}."
        )

    # ── Place order ───────────────────────────────────────────────────────
    yp = _yes_price(side, price_cents)
    try:
        result = place_kalshi_order(ticker, "buy", count, yp)
    except KalshiClientError as exc:
        raise LiveTradeError(str(exc)) from exc

    order  = result.get("order", {})
    filled = _parse_count(order.get("filled_count", count))

    # Register as pending so the next cycle sees us as "holding"
    _add_pending(ticker, side, price_cents, max(filled, count))

    trade = {
        "order_id":      order.get("order_id", ""),
        "market_ticker": ticker,
        "market_label":  market_label,
        "action":        "buy",
        "side":          side,
        "price_cents":   price_cents,
        "contracts":     filled,
        "status":        order.get("status", "submitted"),
    }
    return {"trade": trade, "state": get_state()}


def place_live_sell(ticker: str, side: str, price_cents: int,
                    contracts: int, market_label: str = "") -> dict:
    """
    Sell contracts on Kalshi using immediate_or_cancel so the exit fires now.
    Clears any pending-buy entry for this ticker.
    Returns {"trade": ..., "state": ...}.
    """
    count = max(1, int(contracts))
    yp    = _yes_price(side, price_cents)
    try:
        result = place_kalshi_order(ticker, "sell", count, yp,
                                    time_in_force="immediate_or_cancel")
    except KalshiClientError as exc:
        raise LiveTradeError(str(exc)) from exc

    # Position is gone — clear the pending entry so a fresh buy can happen
    _remove_pending(ticker)

    order  = result.get("order", {})
    filled = _parse_count(order.get("filled_count", count))
    trade  = {
        "order_id":      order.get("order_id", ""),
        "market_ticker": ticker,
        "market_label":  market_label,
        "action":        "sell",
        "side":          side,
        "price_cents":   price_cents,
        "contracts":     filled,
        "status":        order.get("status", "submitted"),
    }
    return {"trade": trade, "state": get_state()}
