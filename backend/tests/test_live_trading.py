"""Live trading: one leg per cycle, 50-per-order / 200-per-day limits, YES/NO order mapping, direction guard."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATABASE_PATH"] = ":memory:"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0, interactive=False)

from kalshi_api import autotrader, client, live_trading as lt  # noqa: E402
from kalshi_api.client import KalshiClientError  # noqa: E402
from kalshi_api.models import LiveOrder  # noqa: E402
from kalshi_api.paper import get_account  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    LiveOrder.objects.all().delete()
    account = get_account()
    account.live_enabled = False
    account.save()
    monkeypatch.setattr(lt.time, "sleep", lambda _s: None)


def leg(ticker="KXHIGHMIA-26OCT28-B87.5", side="yes", price=40, model=0.5, kalshi=0.4):
    return {"ticker": ticker, "label": "87° to 88°", "side": side, "price_cents": price,
            "model_prob": model, "kalshi_prob": kalshi}


class FakeKalshi:
    """Stands in for Kalshi: records orders, fills them, and keeps a position in step."""
    def __init__(self, monkeypatch, fill=None, position_moves=True, error=None):
        self.orders, self.position, self.fill, self.moves, self.error = [], 0.0, fill, position_moves, error
        monkeypatch.setattr(client, "place_kalshi_buy", self.place)
        monkeypatch.setattr(lt, "_position_count", lambda _t: self.position)

    def place(self, ticker, side, count, price, client_order_id):
        if self.error:
            raise self.error
        self.orders.append((ticker, side, count, price, client_order_id))
        filled = count if self.fill is None else min(self.fill, count)
        direction = 1 if side == "yes" else -1
        if filled:
            self.position += direction * filled * (1 if self.moves else -1)   # moves=False: wrong way
        yes_leg = price if side == "yes" else 100 - price
        return {"order_id": "o-1", "fill_count": f"{filled}.00", "remaining_count": f"{count - filled}.00",
                "average_fill_price": f"{yes_leg / 100:.4f}", "average_fee_paid": "0.0200"}


# ── One leg per cycle ─────────────────────────────────────────────────────────

def test_pick_leg_prefers_yes():
    rec = {"yes_trade": leg(side="yes"), "no_trades": [leg(side="no", model=0.05, kalshi=0.5)]}
    assert autotrader.pick_leg(rec)["side"] == "yes"


def test_pick_leg_takes_biggest_no_edge_when_no_yes():
    small = leg(ticker="A", side="no", model=0.30, kalshi=0.35)
    big = leg(ticker="B", side="no", model=0.05, kalshi=0.30)
    assert autotrader.pick_leg({"yes_trade": None, "no_trades": [small, big]})["ticker"] == "B"


def test_pick_leg_none_without_edge():
    assert autotrader.pick_leg({"yes_trade": None, "no_trades": []}) is None


# ── Order mapping: Kalshi V2 only knows the YES book ─────────────────────────

@pytest.fixture
def posted(monkeypatch):
    bodies = []
    monkeypatch.setattr(client, "post_json", lambda path, body: bodies.append((path, body)) or {})
    return bodies


def test_yes_is_a_bid_at_the_yes_price(posted):
    client.place_kalshi_buy("T", "yes", 50, 45, "cid")
    path, body = posted[0]
    assert path == "/portfolio/events/orders"
    assert (body["side"], body["price"], body["count"]) == ("bid", "0.4500", "50.00")
    assert body["time_in_force"] == "immediate_or_cancel" and body["client_order_id"] == "cid"


def test_no_is_an_ask_at_the_complement_price(posted):
    client.place_kalshi_buy("T", "no", 50, 30, "cid")
    body = posted[0][1]
    assert (body["side"], body["price"]) == ("ask", "0.7000")   # NO at 30c == ask at 70c on the YES book


@pytest.mark.parametrize("price", [0, 100, -5])
def test_rejects_prices_outside_1_to_99(posted, price):
    with pytest.raises(KalshiClientError):
        client.place_kalshi_buy("T", "yes", 50, price, "cid")
    assert posted == []


# ── Limits ────────────────────────────────────────────────────────────────────

def test_each_order_is_50_contracts(monkeypatch):
    fake = FakeKalshi(monkeypatch)
    order = lt.submit_live_buy("2026-10-28T12:30", leg())
    assert fake.orders[0][2] == 50 and order.status == "filled" and lt.contracts_used_today() == 50


def test_daily_limit_of_200_then_stops(monkeypatch):
    fake = FakeKalshi(monkeypatch)
    for i in range(4):
        lt.submit_live_buy(f"2026-10-28T1{i}:00", leg())
    assert lt.contracts_used_today() == 200
    with pytest.raises(lt.LiveTradeSkipped, match="daily limit"):
        lt.submit_live_buy("2026-10-28T15:30", leg())
    assert len(fake.orders) == 4


def test_last_order_is_trimmed_to_the_remaining_allowance(monkeypatch):
    fake = FakeKalshi(monkeypatch)
    for i in range(3):
        lt.submit_live_buy(f"2026-10-28T1{i}:00", leg())
    LiveOrder.objects.filter(slot="2026-10-28T10:00").update(contracts_requested=80)   # 80+50+50 = 180 used
    lt.submit_live_buy("2026-10-28T14:00", leg())
    assert fake.orders[-1][2] == 20 and lt.contracts_used_today() == 200


def test_rejected_orders_do_not_use_the_allowance(monkeypatch):
    FakeKalshi(monkeypatch, error=KalshiClientError("Kalshi API 400: invalid input"))
    order = lt.submit_live_buy("2026-10-28T12:30", leg())
    assert order.status == "error" and lt.contracts_used_today() == 0


def test_unknown_outcome_still_counts(monkeypatch):
    FakeKalshi(monkeypatch, error=KalshiClientError("Kalshi POST failed for /portfolio/events/orders"))
    order = lt.submit_live_buy("2026-10-28T12:30", leg())
    assert order.status == "unknown" and lt.contracts_used_today() == 50


def test_a_slot_never_sends_twice(monkeypatch):
    fake = FakeKalshi(monkeypatch)
    lt.submit_live_buy("2026-10-28T12:30", leg())
    with pytest.raises(lt.LiveTradeSkipped, match="already"):
        lt.submit_live_buy("2026-10-28T12:30", leg())
    assert len(fake.orders) == 1


# ── Fills and the direction guard ────────────────────────────────────────────

def test_partial_fill_is_recorded(monkeypatch):
    FakeKalshi(monkeypatch, fill=30)
    order = lt.submit_live_buy("2026-10-28T12:30", leg())
    assert order.status == "partial" and float(order.fill_count) == 30
    assert order.avg_price_cents == pytest.approx(40) and order.fee_cents == pytest.approx(60)


def test_no_fill_is_recorded(monkeypatch):
    FakeKalshi(monkeypatch, fill=0)
    assert lt.submit_live_buy("2026-10-28T12:30", leg()).status == "unfilled"


def test_no_side_average_price_is_converted_back_to_the_no_price(monkeypatch):
    FakeKalshi(monkeypatch)
    order = lt.submit_live_buy("2026-10-28T12:30", leg(side="no", price=30))
    assert order.avg_price_cents == pytest.approx(30)


def test_wrong_direction_switches_live_trading_off(monkeypatch):
    FakeKalshi(monkeypatch, position_moves=False)
    lt.set_live_enabled(True)
    order = lt.submit_live_buy("2026-10-28T12:30", leg(side="no", price=30))
    assert order.status == "mismatch"
    assert get_account().live_enabled is False


def test_correct_direction_keeps_live_trading_on(monkeypatch):
    FakeKalshi(monkeypatch)
    lt.set_live_enabled(True)
    order = lt.submit_live_buy("2026-10-28T12:30", leg(side="no", price=30))
    assert order.status == "filled" and get_account().live_enabled is True


def test_unreadable_position_does_not_switch_anything_off(monkeypatch):
    FakeKalshi(monkeypatch)
    monkeypatch.setattr(lt, "_position_count", lambda _t: None)
    lt.set_live_enabled(True)
    lt.submit_live_buy("2026-10-28T12:30", leg())
    assert get_account().live_enabled is True


# ── Settlement ────────────────────────────────────────────────────────────────

def test_settlement_computes_profit_and_loss(monkeypatch):
    FakeKalshi(monkeypatch)
    lt.submit_live_buy("2026-10-28T12:30", leg(side="yes", price=40))
    monkeypatch.setattr(client, "fetch_json", lambda path, params=None: {"market": {"status": "finalized", "result": "yes"}})
    assert lt.settle_live_orders() == 1
    order = LiveOrder.objects.get()
    # 50 contracts: pays $50.00, cost 50 * 40c = $20.00, fee 50 * 2c = $1.00  ->  +$29.00
    assert order.payout_cents == 5000 and order.realized_pl_cents == 2900


def test_losing_order_loses_its_cost(monkeypatch):
    FakeKalshi(monkeypatch)
    lt.submit_live_buy("2026-10-28T12:30", leg(side="yes", price=40))
    monkeypatch.setattr(client, "fetch_json", lambda path, params=None: {"market": {"status": "finalized", "result": "no"}})
    lt.settle_live_orders()
    assert LiveOrder.objects.get().realized_pl_cents == -2100   # -$20.00 cost -$1.00 fee


def test_live_switch_defaults_to_off():
    assert get_account().live_enabled is False


# ── The whole cycle ───────────────────────────────────────────────────────────

def _cycle(monkeypatch, rec, live, auto):
    from datetime import datetime, timezone
    from kalshi_api import algorithm, forecast
    from kalshi_api.models import AutoTradeRun, PaperTrade

    AutoTradeRun.objects.all().delete()
    PaperTrade.objects.all().delete()
    account = get_account()
    account.live_enabled, account.auto_enabled = live, auto
    account.save()
    fake = FakeKalshi(monkeypatch)
    monkeypatch.setattr(forecast, "get_forecast_cache", lambda: {"last_run_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    monkeypatch.setattr(client, "get_miami_temperature_payload", lambda include_orderbook=False: {"markets": []})
    monkeypatch.setattr(algorithm, "get_full_state", lambda markets: {"recommendation": rec})
    run = autotrader.run_cycle("2026-10-28T12:30")
    return fake, run, PaperTrade.objects.count()


READY = {"status": "ready", "reason": "Forecast 88.0°F", "mean": 88.0, "yes_trade": None,
         "no_trades": [leg(ticker="A", side="no", price=30, model=0.05, kalshi=0.30),
                       leg(ticker="B", side="no", price=40, model=0.25, kalshi=0.40)]}


def test_cycle_places_exactly_one_live_order_for_the_best_leg(monkeypatch):
    fake, run, paper = _cycle(monkeypatch, READY, live=True, auto=True)
    assert len(fake.orders) == 1 and fake.orders[0][0] == "A" and fake.orders[0][2] == 50   # biggest NO edge, 50 contracts
    assert paper == 1                                                                     # paper mirrors the same single trade
    assert run.status == "traded" and "LIVE filled" in run.message


def test_cycle_sends_nothing_to_kalshi_when_live_is_off(monkeypatch):
    fake, run, paper = _cycle(monkeypatch, READY, live=False, auto=True)
    assert fake.orders == [] and paper == 1 and "LIVE" not in run.message


def test_live_works_without_the_paper_bot(monkeypatch):
    fake, run, paper = _cycle(monkeypatch, READY, live=True, auto=False)
    assert len(fake.orders) == 1 and paper == 0 and run.status == "traded"


def test_cycle_stops_quietly_at_the_daily_limit(monkeypatch):
    for i in range(4):
        LiveOrder.objects.create(client_order_id=f"x{i}", slot=f"s{i}", et_date=lt.today_et(), market_ticker="T",
                                 side="yes", limit_price_cents=40, contracts_requested=50, status="filled")
    fake, run, paper = _cycle(monkeypatch, READY, live=True, auto=False)
    assert fake.orders == [] and "daily limit" in run.message
