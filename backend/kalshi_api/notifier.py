"""
Edge alerts by Telegram and/or email.

When the auto-trader finds edges it sends ONE message listing every edge in the cycle, so you can place the
trades by hand on Kalshi. Each channel turns on when its variables are set on the backend; with none set,
everything here quietly does nothing. Sending never raises: a failed alert must not break a trading cycle.

Telegram:
  TELEGRAM_BOT_TOKEN   - from @BotFather
  TELEGRAM_CHAT_ID     - where to post. For a CHANNEL: its @username (public channel) or its numeric id
                         starting with -100 (private channel). The bot must be an admin of the channel
                         with permission to post messages.

Email (Gmail by default — needs 2-step verification and an App Password, not your normal password):
  EMAIL_USERNAME       - the sending address, e.g. you@gmail.com
  EMAIL_APP_PASSWORD   - the 16-character app password
  EMAIL_TO             - optional; comma-separated recipients (defaults to EMAIL_USERNAME)
  EMAIL_SMTP_HOST      - optional; default smtp.gmail.com
  EMAIL_SMTP_PORT      - optional; default 465 (SSL). Any other port uses STARTTLS (e.g. 587).
"""
import json
import os
import smtplib
import urllib.error
from email.message import EmailMessage
from urllib.request import Request, urlopen


TELEGRAM_TIMEOUT_SEC = 10
EMAIL_TIMEOUT_SEC = 15


# ── Telegram ──────────────────────────────────────────────────────────────────

def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def _normalise_chat_id(raw: str) -> str:
    """A channel name written without its @ is accepted; numeric ids (-100…) are used as they are."""
    raw = raw.strip()
    if raw.lstrip("-").isdigit() or raw.startswith("@"):
        return raw
    return "@" + raw


def _hint(detail: str) -> str:
    """Plain-language fix for the usual channel set-up mistakes."""
    lowered = detail.lower()
    if "chat not found" in lowered:
        return " — check TELEGRAM_CHAT_ID: use the channel's @username, or its -100… id for a private channel, and make sure the bot was added to it."
    if "not enough rights" in lowered or "need administrator" in lowered or "forbidden" in lowered:
        return " — make the bot an ADMIN of the channel with permission to post messages."
    if "unauthorized" in lowered:
        return " — check TELEGRAM_BOT_TOKEN."
    return ""


def send_telegram(text: str):
    """Send `text` to the configured chat. Returns (ok, detail) — never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)."
    chat_id = _normalise_chat_id(chat_id)

    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TELEGRAM_TIMEOUT_SEC) as response:
            data = json.loads(response.read().decode("utf-8"))
        return bool(data.get("ok")), "sent" if data.get("ok") else str(data.get("description", data))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("description", str(exc))
        except Exception:
            detail = str(exc)
        return False, f"Telegram {exc.code}: {detail}{_hint(detail)}"
    except Exception as exc:
        return False, f"Telegram request failed: {type(exc).__name__}"


# ── Email ─────────────────────────────────────────────────────────────────────

def email_configured() -> bool:
    return bool(os.environ.get("EMAIL_USERNAME", "").strip() and os.environ.get("EMAIL_APP_PASSWORD", "").strip())


def send_email(subject: str, text: str):
    """Send a plain-text email. Returns (ok, detail) — never raises."""
    username = os.environ.get("EMAIL_USERNAME", "").strip()
    password = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "").strip()   # Google shows it in 4 groups
    if not username or not password:
        return False, "Email is not configured (EMAIL_USERNAME / EMAIL_APP_PASSWORD)."

    recipients = [r.strip() for r in (os.environ.get("EMAIL_TO", "") or username).split(",") if r.strip()]
    host = os.environ.get("EMAIL_SMTP_HOST", "").strip() or "smtp.gmail.com"
    try:
        port = int(os.environ.get("EMAIL_SMTP_PORT", "").strip() or 465)
    except ValueError:
        return False, "EMAIL_SMTP_PORT must be a number."

    message = EmailMessage()
    message["From"] = username
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(text)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=EMAIL_TIMEOUT_SEC) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=EMAIL_TIMEOUT_SEC) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(message)
        return True, "sent"
    except smtplib.SMTPAuthenticationError:
        return False, "Email login refused — use a Gmail App Password (not your normal password) and the matching address."
    except smtplib.SMTPException as exc:   # e.g. a refused recipient; must come before OSError, which it subclasses
        return False, f"Email failed: {type(exc).__name__}: {exc}"[:200]
    except (TimeoutError, ConnectionError, OSError) as exc:
        return False, (
            f"Email could not connect ({type(exc).__name__}). Some hosts, including Railway's Free/Hobby plans, "
            "block outgoing SMTP — if so, use Telegram or an email service over HTTPS."
        )
    except Exception as exc:
        return False, f"Email failed: {type(exc).__name__}: {exc}"[:200]


# ── Sending ───────────────────────────────────────────────────────────────────

def notify_all(subject: str, text: str, channels=None) -> list:
    """
    Send the alert to every configured channel, or only to `channels` (e.g. ("telegram",)).
    Returns [{"channel", "ok", "detail"}] — empty if none of the wanted channels is set up.
    """
    wanted = None if channels is None else set(channels)
    results = []
    if telegram_configured() and (wanted is None or "telegram" in wanted):
        ok, detail = send_telegram(f"{subject}\n\n{text}")
        results.append({"channel": "telegram", "ok": ok, "detail": detail})
    if email_configured() and (wanted is None or "email" in wanted):
        ok, detail = send_email(subject, text)
        results.append({"channel": "email", "ok": ok, "detail": detail})
    return results


def trade_alerts_enabled() -> bool:
    """Per-trade Telegram messages are on by default; TELEGRAM_TRADE_ALERTS=false turns just those off."""
    return os.environ.get("TELEGRAM_TRADE_ALERTS", "true").strip().lower() not in {"0", "false", "no", "off"}


def channels_status() -> dict:
    return {"telegram_configured": telegram_configured(), "email_configured": email_configured()}


# ── Message ───────────────────────────────────────────────────────────────────

def edge_points(leg: dict):
    """The leg's edge in probability points (always positive for a leg the strategy chose), or None."""
    model, kalshi = leg.get("model_prob"), leg.get("kalshi_prob")   # kalshi = Kalshi's implied P(YES)
    if model is None or kalshi is None:
        return None
    return abs(kalshi - model) * 100


def format_edge_alert(legs: list, slot_time: str, mean, contracts: int, traded=None, paper_note: str = ""):
    """
    One message for the whole cycle. `legs` is every edge found, best first. `traded` is the leg, or the
    list of legs, the paper account bought (each marked with ★). Returns (subject, body).
    """
    if traded is None:
        traded = []
    elif isinstance(traded, dict):
        traded = [traded]
    bought = {(t["ticker"], t["side"]) for t in traded}
    subject = f"Kalshi edge — {slot_time} ET: {len(legs)} trade{'s' if len(legs) != 1 else ''} to consider"
    lines = [f"Forecast high: {mean:.1f}°F" if mean is not None else "Forecast high: —", ""]
    for number, leg in enumerate(legs, start=1):
        star = "  ★ paper traded" if (leg["ticker"], leg["side"]) in bought else ""
        lines.append(f"{number}. BUY {leg['side'].upper()} · {leg.get('label') or leg['ticker']}{star}")
        detail = f"   {leg['price_cents']}¢ per contract · suggested size {contracts}"
        points = edge_points(leg)
        if points is not None:
            detail += (
                f"\n   Model {leg['model_prob'] * 100:.0f}% YES vs Kalshi {leg['kalshi_prob'] * 100:.0f}% "
                f"→ edge {points:.0f} pts"
            )
        lines += [detail, f"   {leg['ticker']}", ""]
    if paper_note:
        lines.append(paper_note)
    lines.append("Prices move — check the live price on Kalshi before you order.")
    return subject, "\n".join(lines)


def _usd(cents) -> str:
    return f"${cents / 100:,.2f}"


def format_trade_alert(result: dict, leg: dict, slot_time: str):
    """
    One message for ONE paper trade the bot has just made. `result` is what paper.place_order returned
    ({"trade", "state"}), so the numbers are what was really booked: size, price, cost, the resulting
    position and the paper cash left. Returns (subject, body).
    """
    trade, state = result["trade"], result["state"]
    side = trade["side"].upper()
    label = leg.get("label") or trade.get("market_label") or trade["market_ticker"]
    cost = -trade["cash_delta_cents"]
    lines = [f"BUY {side} · {label}", f"{trade['contracts']:g} contracts @ {trade['price_cents']}¢ = {_usd(cost)}"]
    points = edge_points(leg)
    if points is not None:
        lines.append(f"Model {leg['model_prob'] * 100:.0f}% YES vs Kalshi {leg['kalshi_prob'] * 100:.0f}% → edge {points:.0f} pts")
    position = next((p for p in state.get("positions", [])
                     if p["market_ticker"] == trade["market_ticker"] and p["side"] == trade["side"]
                     and not p.get("settled")), None)
    if position:
        lines.append(f"Position now: {position['contracts']:g} {side} @ avg {position['avg_price_cents']:.1f}¢ "
                     f"({_usd(position['cost_basis_cents'])} at risk)")
    lines.append(f"Paper cash left: {_usd(state['account']['cash_cents'])}")
    lines.append(f"Market: {trade['market_ticker']}")
    return f"📄 Paper trade — {slot_time} ET", "\n".join(lines)


def format_trade_failure(leg: dict, contracts: int, reason: str, slot_time: str):
    """One message for a paper trade the bot tried to make but could not (for example not enough paper cash)."""
    side = leg["side"].upper()
    lines = [f"BUY {side} · {leg.get('label') or leg['ticker']}",
             f"{contracts} contracts @ {leg['price_cents']}¢ was NOT placed.",
             f"Reason: {reason}", f"Market: {leg['ticker']}"]
    return f"⚠ Paper trade NOT placed — {slot_time} ET", "\n".join(lines)
