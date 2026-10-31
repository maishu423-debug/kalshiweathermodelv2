"""A cycle paper-buys every leg the strategy finds (100 contracts each), sends ONE Telegram message per trade made
and ONE edge summary (Telegram/email). Nothing here may send a real order."""
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATABASE_PATH"] = ":memory:"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0, interactive=False)

from kalshi_api import algorithm, autotrader, client, forecast, notifier  # noqa: E402
from kalshi_api.models import AutoTradeRun, PaperPosition, PaperTrade  # noqa: E402
from kalshi_api.paper import get_account  # noqa: E402


def leg(ticker="KXHIGHMIA-26OCT29-B87.5", side="yes", price=40, model=0.5, kalshi=0.4, label="87° to 88°"):
    return {"ticker": ticker, "label": label, "side": side, "price_cents": price,
            "model_prob": model, "kalshi_prob": kalshi}


# ── Which edges are listed ────────────────────────────────────────────────────

def test_all_legs_lists_yes_first_then_nos_by_edge():
    yes = leg(ticker="Y", side="yes")
    small = leg(ticker="A", side="no", model=0.30, kalshi=0.35)
    big = leg(ticker="B", side="no", model=0.05, kalshi=0.30)
    assert [x["ticker"] for x in autotrader.all_legs({"yes_trade": yes, "no_trades": [small, big]})] == ["Y", "B", "A"]
    assert [x["ticker"] for x in autotrader.all_legs({"yes_trade": None, "no_trades": [small, big]})] == ["B", "A"]
    assert autotrader.all_legs({"yes_trade": None, "no_trades": []}) == []


# ── The message ───────────────────────────────────────────────────────────────

def test_message_lists_every_edge_and_marks_the_paper_trade():
    legs = [leg(ticker="Y", side="yes", price=40, model=0.50, kalshi=0.40, label="87° to 88°"),
            leg(ticker="B", side="no", price=30, model=0.05, kalshi=0.30, label="89° or above"),
            leg(ticker="A", side="no", price=55, model=0.30, kalshi=0.45, label="83° to 84°")]
    subject, body = notifier.format_edge_alert(legs, "12:30 PM", 88.4, 50, traded=legs[0], paper_note="Paper account: x")
    assert subject == "Kalshi edge — 12:30 PM ET: 3 trades to consider"
    assert "1. BUY YES · 87° to 88°  ★ paper traded" in body
    assert "2. BUY NO · 89° or above" in body and "3. BUY NO · 83° to 84°" in body
    assert body.count("★") == 1
    assert "Model 5% YES vs Kalshi 30% → edge 25 pts" in body and "30¢ per contract · suggested size 50" in body
    assert "Forecast high: 88.4°F" in body and "Paper account: x" in body


def test_every_bought_leg_is_starred():
    legs = [leg(ticker="Y", side="yes"), leg(ticker="B", side="no", model=0.05, kalshi=0.30),
            leg(ticker="A", side="no", model=0.30, kalshi=0.45)]
    _, body = notifier.format_edge_alert(legs, "12:30 PM", 88.0, 100, traded=[legs[0], legs[2]])
    assert body.count("★ paper traded") == 2
    assert "1. BUY YES · 87° to 88°  ★ paper traded" in body and "3. BUY NO · 87° to 88°  ★ paper traded" in body
    assert "2. BUY NO · 87° to 88°  ★" not in body


def test_single_edge_subject_is_singular():
    subject, _ = notifier.format_edge_alert([leg()], "1:00 PM", None, 50)
    assert subject == "Kalshi edge — 1:00 PM ET: 1 trade to consider"


def test_nothing_is_starred_when_there_was_no_paper_trade():
    _, body = notifier.format_edge_alert([leg()], "1:00 PM", 88.0, 50, traded=None)
    assert "★" not in body


# ── Telegram ──────────────────────────────────────────────────────────────────

def test_not_configured_means_no_network_call(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EMAIL_USERNAME", "EMAIL_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(notifier, "urlopen", lambda *a, **k: pytest.fail("must not call Telegram"))
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("must not send email"))
    assert notifier.notify_all("s", "b") == []
    assert notifier.channels_status() == {"telegram_configured": False, "email_configured": False}


def test_telegram_request_shape(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        seen["url"], seen["body"] = request.full_url, json.loads(request.data)
        return Resp()

    monkeypatch.setattr(notifier, "urlopen", fake_urlopen)
    assert notifier.send_telegram("hello") == (True, "sent")
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert seen["body"]["chat_id"] == "999" and seen["body"]["text"] == "hello"


@pytest.mark.parametrize("configured, sent_as", [
    ("@my_alerts", "@my_alerts"),            # public channel
    ("my_alerts", "@my_alerts"),             # @ forgotten
    ("-1001234567890", "-1001234567890"),    # private channel id
    (" 999 ", "999"),                        # personal chat, stray spaces
])
def test_channel_ids_are_accepted_in_every_form(monkeypatch, configured, sent_as):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", configured)
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr(notifier, "urlopen", lambda request, timeout: seen.update(body=json.loads(request.data)) or Resp())
    assert notifier.send_telegram("x")[0] is True
    assert seen["body"]["chat_id"] == sent_as


@pytest.mark.parametrize("description, expected", [
    ("Bad Request: chat not found", "@username"),
    ("Forbidden: bot is not a member of the channel chat", "ADMIN"),
    ("Bad Request: not enough rights to send text messages to the chat", "ADMIN"),
    ("Unauthorized", "TELEGRAM_BOT_TOKEN"),
])
def test_setup_mistakes_get_a_plain_hint(monkeypatch, description, expected):
    import io
    import urllib.error

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@c")

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                     io.BytesIO(json.dumps({"ok": False, "description": description}).encode()))

    monkeypatch.setattr(notifier, "urlopen", refuse)
    ok, detail = notifier.send_telegram("x")
    assert ok is False and description in detail and expected in detail
    assert "t/sendMessage" not in detail   # the token/URL never appears in the message


def test_a_failing_telegram_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(notifier, "urlopen", boom)
    ok, detail = notifier.send_telegram("hi")
    assert ok is False and "failed" in detail


def test_notify_all_sends_subject_and_body_to_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.delenv("EMAIL_USERNAME", raising=False)
    seen = []
    monkeypatch.setattr(notifier, "send_telegram", lambda text: seen.append(text) or (True, "sent"))
    results = notifier.notify_all("Subject", "Body")
    assert results == [{"channel": "telegram", "ok": True, "detail": "sent"}]
    assert seen == ["Subject\n\nBody"]


# ── Email ─────────────────────────────────────────────────────────────────────

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.started_tls, self.login_args, self.sent = host, port, False, None, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture
def email_env(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setenv("EMAIL_USERNAME", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    for name in ("EMAIL_TO", "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)


def test_email_goes_to_gmail_over_ssl_with_the_app_password(email_env):
    assert notifier.send_email("Subject line", "Body text") == (True, "sent")
    server = FakeSMTP.instances[0]
    assert (server.host, server.port, server.started_tls) == ("smtp.gmail.com", 465, False)
    assert server.login_args == ("me@gmail.com", "abcdefghijklmnop")   # spaces in the app password are dropped
    message = server.sent[0]
    assert message["To"] == "me@gmail.com" and message["Subject"] == "Subject line"
    assert "Body text" in message.get_content()


def test_email_can_go_to_several_recipients_on_the_starttls_port(email_env, monkeypatch):
    monkeypatch.setenv("EMAIL_TO", "a@x.com, b@y.com")
    monkeypatch.setenv("EMAIL_SMTP_PORT", "587")
    assert notifier.send_email("s", "b")[0] is True
    server = FakeSMTP.instances[0]
    assert server.port == 587 and server.started_tls is True
    assert server.sent[0]["To"] == "a@x.com, b@y.com"


def test_blocked_or_unreachable_smtp_explains_itself_and_never_raises(email_env, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", boom)
    ok, detail = notifier.send_email("s", "b")
    assert ok is False and "could not connect" in detail and "block outgoing SMTP" in detail


def test_wrong_gmail_password_gives_a_helpful_message(email_env, monkeypatch):
    def refuse(*args, **kwargs):
        raise notifier.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", refuse)
    ok, detail = notifier.send_email("s", "b")
    assert ok is False and "App Password" in detail


def test_a_refused_recipient_is_not_reported_as_a_connection_problem(email_env, monkeypatch):
    def refuse(*args, **kwargs):
        raise notifier.smtplib.SMTPRecipientsRefused({"bad@x.com": (550, b"no such user")})

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", refuse)
    ok, detail = notifier.send_email("s", "b")
    assert ok is False and detail.startswith("Email failed: SMTPRecipientsRefused")


def test_notify_all_reaches_both_channels(email_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(notifier, "send_telegram", lambda text: (True, "sent"))
    results = notifier.notify_all("S", "B")
    assert [(r["channel"], r["ok"]) for r in results] == [("telegram", True), ("email", True)]
    assert FakeSMTP.instances[0].sent[0]["Subject"] == "S"


def test_one_channel_failing_does_not_stop_the_other(email_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(notifier, "send_telegram", lambda text: (False, "Telegram 400: chat not found"))
    results = notifier.notify_all("S", "B")
    assert [(r["channel"], r["ok"]) for r in results] == [("telegram", False), ("email", True)]


# ── One message per paper trade ───────────────────────────────────────────────

def booked(side="no", ticker="A", label="89° or above", contracts=100, price=30, cash=997_000,
           position=None):
    """What paper.place_order returns for a buy."""
    position = position or {"market_ticker": ticker, "market_label": label, "side": side, "contracts": float(contracts),
                            "avg_price_cents": float(price), "cost_basis_cents": contracts * price, "settled": False}
    return {"trade": {"id": 1, "market_ticker": ticker, "market_label": label, "action": "buy", "side": side,
                      "price_cents": price, "contracts": float(contracts), "cash_delta_cents": -contracts * price,
                      "realized_pl_cents": None, "created_at": "2026-10-29T16:30:00+00:00"},
            "state": {"account": {"cash_cents": cash}, "positions": [position]}}


def test_trade_message_shows_what_was_booked():
    subject, body = notifier.format_trade_alert(
        booked(), leg(ticker="A", side="no", price=30, model=0.05, kalshi=0.30, label="89° or above"), "12:30 PM")
    assert subject == "📄 Paper trade — 12:30 PM ET"
    assert "BUY NO · 89° or above" in body and "100 contracts @ 30¢ = $30.00" in body
    assert "Model 5% YES vs Kalshi 30% → edge 25 pts" in body
    assert "Position now: 100 NO @ avg 30.0¢ ($30.00 at risk)" in body
    assert "Paper cash left: $9,970.00" in body and "Market: A" in body


def test_trade_message_shows_the_whole_position_and_survives_missing_probabilities():
    pos = {"market_ticker": "A", "market_label": "x", "side": "yes", "contracts": 300.0, "avg_price_cents": 41.5,
           "cost_basis_cents": 12450, "settled": False}
    _, body = notifier.format_trade_alert(booked(side="yes", position=pos), {"ticker": "A", "side": "yes", "label": "x"}, "1:00 PM")
    assert "Position now: 300 YES @ avg 41.5¢ ($124.50 at risk)" in body and "edge" not in body


def test_failed_trade_message_says_why():
    subject, body = notifier.format_trade_failure(leg(ticker="B", side="no", price=40, label="83° to 84°"), 100,
                                                  "Insufficient paper cash.", "12:30 PM")
    assert subject == "⚠ Paper trade NOT placed — 12:30 PM ET"
    assert "BUY NO · 83° to 84°" in body and "100 contracts @ 40¢ was NOT placed." in body
    assert "Reason: Insufficient paper cash." in body


def test_notify_all_can_be_limited_to_telegram(email_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(notifier, "send_telegram", lambda text: (True, "sent"))
    assert [r["channel"] for r in notifier.notify_all("S", "B", channels=("telegram",))] == ["telegram"]
    assert all(not i.sent for i in FakeSMTP.instances)                      # no email went out
    assert [r["channel"] for r in notifier.notify_all("S", "B")] == ["telegram", "email"]


def test_trade_alerts_switch(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TRADE_ALERTS", raising=False)
    assert notifier.trade_alerts_enabled() is True
    for off in ("false", "0", "No", "OFF"):
        monkeypatch.setenv("TELEGRAM_TRADE_ALERTS", off)
        assert notifier.trade_alerts_enabled() is False
    monkeypatch.setenv("TELEGRAM_TRADE_ALERTS", "true")
    assert notifier.trade_alerts_enabled() is True


# ── The whole cycle ───────────────────────────────────────────────────────────

READY = {"status": "ready", "reason": "Forecast 88.0°F", "mean": 88.0, "yes_trade": None,
         "no_trades": [leg(ticker="A", side="no", price=30, model=0.05, kalshi=0.30, label="89° or above"),
                       leg(ticker="B", side="no", price=40, model=0.25, kalshi=0.40, label="83° to 84°")]}


def _cycle(monkeypatch, rec, auto=True, channel_ok=True, cash_cents=1_000_000):
    """Run one cycle with Kalshi, the forecast and the alert channels faked. Returns (messages, run, paper trade count)."""
    AutoTradeRun.objects.all().delete()
    PaperTrade.objects.all().delete()
    PaperPosition.objects.all().delete()
    account = get_account()
    account.auto_enabled = auto
    account.cash_cents = cash_cents
    account.save()
    sent = []

    def fake_notify(subject, body, channels=None):
        sent.append({"subject": subject, "body": body, "channels": channels})
        return [{"channel": "telegram", "ok": channel_ok, "detail": "sent" if channel_ok else "boom"}]

    monkeypatch.setattr(autotrader, "notify_all", fake_notify)
    monkeypatch.setattr(forecast, "get_forecast_cache",
                        lambda: {"last_run_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    monkeypatch.setattr(client, "get_miami_temperature_payload", lambda include_orderbook=False: {"markets": []})
    monkeypatch.setattr(algorithm, "get_full_state", lambda markets: {"recommendation": rec})
    run = autotrader.run_cycle("2026-10-29T12:30")
    return sent, run, PaperTrade.objects.count()


def edge_msgs(sent):
    return [m for m in sent if m["subject"].startswith("Kalshi edge")]


def trade_msgs(sent):
    return [m for m in sent if m["subject"].startswith("📄")]


def failure_msgs(sent):
    return [m for m in sent if m["subject"].startswith("⚠")]


def test_cycle_buys_every_leg_at_100_contracts_and_sends_the_edge_summary(monkeypatch):
    sent, run, paper = _cycle(monkeypatch, READY)
    assert autotrader.CONTRACTS_PER_TRADE == 100
    assert paper == 2 and len(edge_msgs(sent)) == 1                        # two paper trades, one edge summary
    trades = list(PaperTrade.objects.order_by("id"))
    assert [t.market_ticker for t in trades] == ["A", "B"] and all(float(t.contracts) == 100 for t in trades)
    edge = edge_msgs(sent)[0]
    assert edge["subject"] == "Kalshi edge — 12:30 PM ET: 2 trades to consider" and edge["channels"] is None
    assert "1. BUY NO · 89° or above  ★ paper traded" in edge["body"] and "2. BUY NO · 83° to 84°  ★ paper traded" in edge["body"]
    assert run.status == "traded" and run.trades_count == 2


def test_every_paper_trade_gets_its_own_telegram_message(monkeypatch):
    sent, run, paper = _cycle(monkeypatch, READY)
    msgs = trade_msgs(sent)
    assert paper == 2 and len(msgs) == 2
    assert all(m["channels"] == ("telegram",) for m in msgs)               # per-trade messages go to Telegram only
    first, second = msgs[0]["body"], msgs[1]["body"]
    assert "BUY NO · 89° or above" in first and "100 contracts @ 30¢ = $30.00" in first and "Paper cash left: $9,970.00" in first
    assert "Position now: 100 NO @ avg 30.0¢ ($30.00 at risk)" in first
    assert "BUY NO · 83° to 84°" in second and "100 contracts @ 40¢ = $40.00" in second and "Paper cash left: $9,930.00" in second
    assert [m["subject"] for m in sent][-1].startswith("Kalshi edge")      # the summary follows the confirmations


def test_a_second_buy_in_the_same_market_reports_the_combined_position(monkeypatch):
    _cycle(monkeypatch, READY)
    later = []
    monkeypatch.setattr(autotrader, "notify_all",
                        lambda subject, body, channels=None: later.append((subject, body)) or [])
    autotrader.run_cycle("2026-10-29T12:45")                                # next slot, same two legs
    first = next(b for s, b in later if s.startswith("📄") and "89° or above" in b)
    assert "100 contracts @ 30¢ = $30.00" in first and "Position now: 200 NO @ avg 30.0¢ ($60.00 at risk)" in first


def test_cycle_buys_the_yes_leg_as_well(monkeypatch):
    rec = {**READY, "yes_trade": leg(ticker="Y", side="yes", price=40, model=0.6, kalshi=0.4, label="87° to 88°")}
    sent, run, paper = _cycle(monkeypatch, rec)
    assert paper == 3 and len(trade_msgs(sent)) == 3
    assert {(t.market_ticker, t.side) for t in PaperTrade.objects.all()} == {("Y", "yes"), ("A", "no"), ("B", "no")}
    assert "BUY YES · 87° to 88°" in trade_msgs(sent)[0]["body"]
    assert "1. BUY YES · 87° to 88°  ★ paper traded" in edge_msgs(sent)[0]["body"]


def test_a_trade_that_cannot_be_placed_is_reported_and_the_others_still_go_through(monkeypatch):
    # cash for the first leg only: 100 x 30c = $30 fits in $45, then 100 x 40c = $40 does not
    sent, run, paper = _cycle(monkeypatch, READY, cash_cents=4500)
    assert paper == 1 and run.status == "traded" and run.trades_count == 1
    assert len(trade_msgs(sent)) == 1 and len(failure_msgs(sent)) == 1
    failure = failure_msgs(sent)[0]
    assert failure["subject"] == "⚠ Paper trade NOT placed — 12:30 PM ET" and failure["channels"] == ("telegram",)
    assert "83° to 84°" in failure["body"] and "Insufficient paper cash" in failure["body"]
    assert "failed" in run.message and "83° to 84°" in run.message
    assert edge_msgs(sent)[0]["body"].count("★ paper traded") == 1


def test_no_trade_messages_when_nothing_was_traded(monkeypatch):
    # tick() does not start a cycle while Auto is off, but a cycle that does run must still send the edge summary only.
    sent, run, paper = _cycle(monkeypatch, READY, auto=False)
    assert paper == 0 and trade_msgs(sent) == [] and failure_msgs(sent) == []
    assert len(edge_msgs(sent)) == 1 and "no paper trades" in edge_msgs(sent)[0]["body"] and "★" not in edge_msgs(sent)[0]["body"]


def test_trade_messages_can_be_switched_off_without_losing_the_edge_summary(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRADE_ALERTS", "false")
    sent, run, paper = _cycle(monkeypatch, READY)
    assert paper == 2 and trade_msgs(sent) == [] and len(edge_msgs(sent)) == 1


def test_no_edge_means_no_messages(monkeypatch):
    waiting = {"status": "waiting", "reason": "No edge", "mean": 88.0, "yes_trade": None, "no_trades": []}
    sent, run, paper = _cycle(monkeypatch, waiting)
    assert sent == [] and paper == 0 and run.status == "skipped"


def test_a_failed_send_is_noted_but_does_not_stop_the_paper_trades(monkeypatch):
    sent, run, paper = _cycle(monkeypatch, READY, channel_ok=False)
    assert paper == 2 and "telegram alert failed: boom" in run.message and run.status == "traded"


def test_a_message_that_cannot_even_be_built_never_stops_trading(monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("oops")
    monkeypatch.setattr(autotrader, "format_trade_alert", broken)
    sent, run, paper = _cycle(monkeypatch, READY)
    assert paper == 2 and run.status == "traded" and "alert error: KeyError" in run.message
    assert len(edge_msgs(sent)) == 1                                        # the summary still goes out


def test_a_slot_alerts_only_once(monkeypatch):
    sent, run, paper = _cycle(monkeypatch, READY)
    count = len(sent)
    assert autotrader.run_cycle("2026-10-29T12:30") is None                # same slot again: nothing happens
    assert len(sent) == count
