"""
Paper-trading bot.

Every 15 minutes between 12:30 PM and 3:30 PM ET (inclusive, 13 cycles a day) it
reads the latest model forecast and today's Kalshi bracket ladder, then BUYS
CONTRACTS_PER_TRADE contracts of YES on the highest-probability bracket when our
model is more bullish than Kalshi, and of NO on each other live bracket where our
model is less bullish than Kalshi (see algorithm.get_recommendation). It never
sells; positions are settled from Kalshi's resolved result (paper.settle_positions),
which also fills the profit/loss into the Google Sheet trade history.

Runs as a daemon thread started from apps.py. Each cycle is keyed by its slot
("2026-10-17T12:30"), stored in AutoTradeRun, so a restart can't repeat a cycle.
Run with a single worker/replica (same constraint as the other background tasks).
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.db import IntegrityError, close_old_connections

from .models import AutoTradeRun
from .paper import PaperTradeError, get_account, parse_order, place_order, settle_positions


ET = ZoneInfo("America/New_York")

CONTRACTS_PER_TRADE = 100
INTERVAL_MINUTES = 15
WINDOW_START = (12, 30)
WINDOW_END = (15, 30)
SLOT_GRACE_SEC = 5 * 60          # a cycle may start up to 5 min after its slot
MAX_FORECAST_AGE_SEC = 15 * 60   # skip the cycle if the model forecast is older than this
LOOP_SLEEP_SEC = 20
SETTLE_EVERY_SEC = 5 * 60

_lock = threading.Lock()
_started = False


# ── Schedule ──────────────────────────────────────────────────────────────────

def _slots_for(day: datetime) -> list:
    start = day.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = day.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    slots = []
    t = start
    while t <= end:
        slots.append(t)
        t += timedelta(minutes=INTERVAL_MINUTES)
    return slots


def current_slot(now: datetime):
    """The most recent slot that is due (within the grace period), else None."""
    due = [s for s in _slots_for(now) if s <= now and (now - s).total_seconds() <= SLOT_GRACE_SEC]
    return due[-1] if due else None


def next_slot(now: datetime) -> datetime:
    for s in _slots_for(now):
        if s > now:
            return s
    return _slots_for(now + timedelta(days=1))[0]


def window_open(now: datetime) -> bool:
    slots = _slots_for(now)
    return slots[0] <= now <= slots[-1] + timedelta(seconds=SLOT_GRACE_SEC)


def slot_key(slot: datetime) -> str:
    return slot.strftime("%Y-%m-%dT%H:%M")


def get_status() -> dict:
    now = datetime.now(ET)
    return {
        "enabled": get_account().auto_enabled,
        "window_open": window_open(now),
        "now": now.isoformat(),
        "next_run_at": next_slot(now).isoformat(),
        "contracts_per_trade": CONTRACTS_PER_TRADE,
        "interval_minutes": INTERVAL_MINUTES,
        "window": f"{WINDOW_START[0]}:{WINDOW_START[1]:02d} – {WINDOW_END[0]}:{WINDOW_END[1]:02d} ET",
    }


# ── One cycle ─────────────────────────────────────────────────────────────────

def _forecast_age_sec(last_run_at):
    if not last_run_at:
        return None
    ran = datetime.fromisoformat(last_run_at).replace(tzinfo=timezone.utc)  # cache stores naive UTC
    return (datetime.now(timezone.utc) - ran).total_seconds()


def run_cycle(key: str):
    """Execute one trading cycle for slot `key`. Returns the AutoTradeRun, or None if already run."""
    try:
        run = AutoTradeRun.objects.create(slot=key, status="running")
    except IntegrityError:
        return None

    def finish(status, message, mean=None, trades=0):
        run.status, run.message, run.forecast_mean, run.trades_count = status, message, mean, trades
        run.save()
        print(f"[autotrader] {key} {status}: {message}")
        return run

    try:
        from .algorithm import get_full_state
        from .client import KalshiClientError, get_miami_temperature_payload
        from .forecast import get_forecast_cache

        cache = get_forecast_cache()
        age = _forecast_age_sec(cache.get("last_run_at"))
        if age is None:
            return finish("skipped", "No model forecast available yet.")
        if age > MAX_FORECAST_AGE_SEC:
            return finish("skipped", f"Model forecast is stale ({int(age // 60)} min old).")

        try:
            payload = get_miami_temperature_payload(include_orderbook=False)
        except KalshiClientError as exc:
            return finish("error", f"Could not load Kalshi markets: {exc}")

        rec = get_full_state(payload["markets"])["recommendation"]
        mean = rec.get("mean")
        if rec["status"] != "ready":
            return finish("skipped", rec["reason"], mean)

        legs = ([rec["yes_trade"]] if rec.get("yes_trade") else []) + rec.get("no_trades", [])
        placed, notes = 0, []
        for leg in legs:
            try:
                place_order(parse_order(json.dumps({
                    "action":        "buy",
                    "side":          leg["side"],
                    "market_ticker": leg["ticker"],
                    "market_label":  leg["label"],
                    "price_cents":   leg["price_cents"],
                    "contracts":     CONTRACTS_PER_TRADE,
                    "model_prob":    leg.get("model_prob"),
                    "kalshi_prob":   leg.get("kalshi_prob"),
                }).encode()))
                placed += 1
            except PaperTradeError as exc:
                notes.append(f"{leg['side'].upper()} {leg['label']}: {exc}")

        message = rec["reason"] + (" — failed: " + "; ".join(notes) if notes else "")
        return finish("traded" if placed else "error", message, mean, placed)
    except Exception as exc:  # never leave a run stuck in "running"
        return finish("error", f"{type(exc).__name__}: {exc}")


def tick(now: datetime = None):
    now = now or datetime.now(ET)
    if not get_account().auto_enabled:
        return
    slot = current_slot(now)
    if slot is not None:
        run_cycle(slot_key(slot))  # no-op if this slot already ran


# ── Background thread ─────────────────────────────────────────────────────────

def _loop():
    time.sleep(45)  # let Django and the first forecast start up
    last_settle = 0.0
    while True:
        try:
            close_old_connections()
            tick()
            if time.time() - last_settle >= SETTLE_EVERY_SEC:
                last_settle = time.time()
                n = settle_positions()
                if n:
                    print(f"[autotrader] settled {n} position(s)")
        except Exception as exc:
            print(f"[autotrader] loop error ({type(exc).__name__}: {exc})")
        finally:
            close_old_connections()
        time.sleep(LOOP_SLEEP_SEC)


def start_autotrader():
    """Call once at Django startup (AppConfig.ready)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True, name="autotrader").start()
