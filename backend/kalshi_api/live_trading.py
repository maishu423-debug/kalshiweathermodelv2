"""
Real-money trading on the user's Kalshi account.

The auto-trader decides WHAT to trade (one leg per cycle: YES if there is a YES edge, otherwise the
best NO). This module decides WHETHER and HOW MUCH, and sends the order:

  * Only runs while the Live Trading switch (PaperAccount.live_enabled) is on. It starts off.
  * PER_TRADE_CONTRACTS per order and DAILY_MAX_CONTRACTS per Eastern trading day. The daily total is
    summed from the database, so a restart cannot reset it. Orders that were rejected by Kalshi do not
    count; an order whose outcome is unknown (timeout / 5xx) does, to stay on the safe side.
  * Every order is written to LiveOrder BEFORE it is sent, under a unique client_order_id.
  * Orders are immediate-or-cancel limit orders at the current ask: they fill what is available at or
    below that price and the rest is cancelled, so nothing is left resting in the book.
  * Direction guard: after a fill we read the position back from Kalshi. If it moved the wrong way
    (e.g. a NO order produced a YES position) live trading switches itself off.

Run with a single worker/replica (same constraint as the other background tasks).
"""
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import IntegrityError
from django.db.models import Sum
from django.utils import timezone

from .models import LiveOrder
from .paper import get_account


ET = ZoneInfo("America/New_York")

PER_TRADE_CONTRACTS = 50
DAILY_MAX_CONTRACTS = 200

POSITION_CHECK_TRIES = 6
POSITION_CHECK_DELAY_SEC = 1.5


class LiveTradeSkipped(Exception):
    """Nothing was sent (daily limit reached, duplicate slot, ...). Not an error."""


# ── Status / switch ───────────────────────────────────────────────────────────

def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def contracts_used_today() -> int:
    total = (
        LiveOrder.objects.filter(et_date=today_et())
        .exclude(status="error")
        .aggregate(total=Sum("contracts_requested"))["total"]
    )
    return int(total or 0)


def credentials_configured() -> bool:
    """True only if a key ID is set AND the private key can actually sign (RSA-PSS). An Ed25519 key loads
    fine but cannot sign this way, so loading alone is not enough."""
    from .auth import get_api_key_id, get_private_key, sign_pss_text

    if not get_api_key_id():
        return False
    try:
        sign_pss_text(get_private_key(), "check")
    except Exception:
        return False
    return True


def live_status() -> dict:
    from .client import KALSHI_BASE_URL

    used = contracts_used_today()
    return {
        "enabled": get_account().live_enabled,
        "credentials_configured": credentials_configured(),
        "environment": "demo" if "demo" in KALSHI_BASE_URL else "production",
        "base_url": KALSHI_BASE_URL,
        "per_trade_contracts": PER_TRADE_CONTRACTS,
        "daily_max_contracts": DAILY_MAX_CONTRACTS,
        "contracts_used_today": used,
        "contracts_remaining_today": max(0, DAILY_MAX_CONTRACTS - used),
    }


def set_live_enabled(enabled: bool):
    account = get_account()
    account.live_enabled = bool(enabled)
    account.save(update_fields=["live_enabled", "updated_at"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decimal(value, default="0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _dollars_to_cents(value):
    if value in (None, ""):
        return None
    try:
        return float(Decimal(str(value)) * 100)
    except (InvalidOperation, ValueError):
        return None


def _position_count(ticker: str):
    """Signed contracts we hold in `ticker` on Kalshi (+YES / −NO), or None if it cannot be read."""
    from .client import KalshiClientError, fetch_json_auth

    try:
        data = fetch_json_auth("/portfolio/positions", {"limit": 1000})
    except KalshiClientError:
        return None
    for p in data.get("market_positions", []):
        if (p.get("ticker") or p.get("market_ticker")) == ticker:
            raw = p.get("position_fp") if p.get("position_fp") not in (None, "") else p.get("position", 0)
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return 0.0


def _verify_direction(order: LiveOrder, before) -> str:
    """'ok' | 'wrong' | 'unverified' — did Kalshi's position move the way this side should?"""
    if before is None:
        return "unverified"
    expected_sign = 1 if order.side == "yes" else -1
    for attempt in range(POSITION_CHECK_TRIES):
        after = _position_count(order.market_ticker)
        if after is not None:
            delta = after - before
            if abs(delta) >= 0.01:
                return "ok" if delta * expected_sign > 0 else "wrong"
        if attempt < POSITION_CHECK_TRIES - 1:
            time.sleep(POSITION_CHECK_DELAY_SEC)   # Kalshi's positions can lag a few seconds
    return "unverified"


# ── Place one order ───────────────────────────────────────────────────────────

def submit_live_buy(slot: str, leg: dict) -> LiveOrder:
    """
    Send one real buy for `leg` ({ticker, label, side, price_cents, model_prob, kalshi_prob}).
    Raises LiveTradeSkipped if nothing was sent. Returns the saved LiveOrder otherwise (whatever its status).
    """
    from .client import KalshiClientError, place_kalshi_buy

    remaining = DAILY_MAX_CONTRACTS - contracts_used_today()
    if remaining <= 0:
        raise LiveTradeSkipped(f"daily limit reached ({DAILY_MAX_CONTRACTS}/{DAILY_MAX_CONTRACTS} contracts)")
    count = min(PER_TRADE_CONTRACTS, remaining)

    ticker, side, price = leg["ticker"], leg["side"], int(leg["price_cents"])
    client_order_id = f"{slot}-{ticker}-{side}"
    try:
        order = LiveOrder.objects.create(
            client_order_id=client_order_id,
            slot=slot,
            et_date=today_et(),
            market_ticker=ticker,
            market_label=leg.get("label", ""),
            side=side,
            limit_price_cents=price,
            contracts_requested=count,
            status="pending",
            model_prob=leg.get("model_prob"),
            kalshi_prob=leg.get("kalshi_prob"),
        )
    except IntegrityError:
        raise LiveTradeSkipped("this slot already sent its order")

    before = _position_count(ticker)

    try:
        response = place_kalshi_buy(ticker, side, count, price, client_order_id)
    except KalshiClientError as exc:
        text = str(exc)
        # A 4xx answer means Kalshi refused it, so nothing was placed. Anything else (timeout, 5xx) is unknown.
        order.status = "error" if text.startswith("Kalshi API 4") else "unknown"
        order.message = text[:900]
        order.save(update_fields=["status", "message"])
        return order

    fill = _decimal(response.get("fill_count"))
    order.order_id = str(response.get("order_id", ""))
    order.fill_count = fill
    order.status = "filled" if fill >= count else "partial" if fill > 0 else "unfilled"

    if fill > 0:
        yes_leg_cents = _dollars_to_cents(response.get("average_fill_price"))
        if yes_leg_cents is not None:
            # Kalshi prices are the YES leg; our NO price is the complement.
            order.avg_price_cents = yes_leg_cents if side == "yes" else 100 - yes_leg_cents
        fee_each = _dollars_to_cents(response.get("average_fee_paid"))
        if fee_each is not None:
            order.fee_cents = fee_each * float(fill)

        verdict = _verify_direction(order, before)
        if verdict == "wrong":
            order.status = "mismatch"
            order.message = (
                f"{side.upper()} order moved the Kalshi position the wrong way. Live trading was switched off — "
                "check your Kalshi account before turning it back on."
            )
            set_live_enabled(False)
        elif verdict == "unverified":
            order.message = "Fill not yet visible in Kalshi positions; direction not verified."
    order.save()
    return order


# ── Settlement ────────────────────────────────────────────────────────────────

def settle_live_orders() -> int:
    """Fill in payout and profit/loss on filled orders whose Kalshi market has resolved."""
    from .client import KalshiClientError, fetch_json

    open_orders = list(LiveOrder.objects.filter(fill_count__gt=0, settled_at__isnull=True))
    results = {}
    for ticker in {o.market_ticker for o in open_orders}:
        try:
            market = fetch_json(f"/markets/{ticker}").get("market", {})
        except KalshiClientError as exc:
            print(f"[live] settlement lookup failed for {ticker} ({exc})")
            continue
        if market.get("status") in ("determined", "finalized", "settled") and market.get("result") in ("yes", "no"):
            results[ticker] = market["result"]

    settled = 0
    for order in open_orders:
        result = results.get(order.market_ticker)
        if result is None:
            continue
        contracts = float(order.fill_count)
        price = order.avg_price_cents if order.avg_price_cents is not None else order.limit_price_cents
        payout = round(contracts * 100) if order.side == result else 0
        order.payout_cents = payout
        order.realized_pl_cents = round(payout - contracts * price - (order.fee_cents or 0))
        order.settled_at = timezone.now()
        order.save(update_fields=["payout_cents", "realized_pl_cents", "settled_at"])
        settled += 1
    return settled


# ── Serialising ───────────────────────────────────────────────────────────────

def serialize_order(order: LiveOrder) -> dict:
    return {
        "id": order.id,
        "created_at": order.created_at.isoformat(),
        "slot": order.slot,
        "market_ticker": order.market_ticker,
        "market_label": order.market_label,
        "side": order.side,
        "limit_price_cents": order.limit_price_cents,
        "contracts_requested": order.contracts_requested,
        "fill_count": float(order.fill_count),
        "avg_price_cents": order.avg_price_cents,
        "fee_cents": order.fee_cents,
        "status": order.status,
        "message": order.message,
        "model_prob": order.model_prob,
        "kalshi_prob": order.kalshi_prob,
        "settled": order.settled_at is not None,
        "payout_cents": order.payout_cents,
        "realized_pl_cents": order.realized_pl_cents,
    }


def get_live_state(limit: int = 100) -> dict:
    orders = LiveOrder.objects.order_by("-created_at")[:limit]
    settled = LiveOrder.objects.filter(settled_at__isnull=False)
    realized = [o.realized_pl_cents or 0 for o in settled]
    return {
        "status": live_status(),
        "summary": {
            "orders": LiveOrder.objects.exclude(status="error").count(),
            "contracts_filled": float(LiveOrder.objects.aggregate(t=Sum("fill_count"))["t"] or 0),
            "realized_pl_cents": sum(realized),
            "wins": sum(1 for v in realized if v > 0),
            "losses": sum(1 for v in realized if v < 0),
        },
        "orders": [serialize_order(o) for o in orders],
    }
