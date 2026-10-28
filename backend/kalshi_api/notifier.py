"""
Telegram edge alerts.

When the auto-trader finds edges it sends ONE message listing every edge in the cycle, so you can place the
trades by hand on Kalshi. Configure with two environment variables on the backend:
  TELEGRAM_BOT_TOKEN   - from @BotFather
  TELEGRAM_CHAT_ID     - where to post. For a CHANNEL: its @username (public channel) or its numeric id
                         starting with -100 (private channel). The bot must be an admin of the channel
                         with permission to post messages.

Without them everything here quietly does nothing. Sending never raises: a failed alert must not break a
trading cycle.
"""
import json
import os
import urllib.error
from urllib.request import Request, urlopen


TELEGRAM_TIMEOUT_SEC = 10


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


# ── Sending ───────────────────────────────────────────────────────────────────

def notify_all(subject: str, text: str) -> list:
    """Send the alert. Returns [{"channel", "ok", "detail"}] — empty if Telegram is not configured."""
    if not telegram_configured():
        return []
    ok, detail = send_telegram(f"{subject}\n\n{text}")
    return [{"channel": "telegram", "ok": ok, "detail": detail}]


def channels_status() -> dict:
    return {"telegram_configured": telegram_configured()}


# ── Message ───────────────────────────────────────────────────────────────────

def edge_points(leg: dict):
    """The leg's edge in probability points (always positive for a leg the strategy chose), or None."""
    model, kalshi = leg.get("model_prob"), leg.get("kalshi_prob")   # kalshi = Kalshi's implied P(YES)
    if model is None or kalshi is None:
        return None
    return abs(kalshi - model) * 100


def format_edge_alert(legs: list, slot_time: str, mean, contracts: int, traded=None, paper_note: str = ""):
    """
    One message for the whole cycle. `legs` is every edge found, best first. `traded` is the leg the paper
    account bought (marked with ★). Returns (subject, body).
    """
    subject = f"Kalshi edge — {slot_time} ET: {len(legs)} trade{'s' if len(legs) != 1 else ''} to consider"
    lines = [f"Forecast high: {mean:.1f}°F" if mean is not None else "Forecast high: —", ""]
    for number, leg in enumerate(legs, start=1):
        star = "  ★ paper traded" if traded is not None and leg["ticker"] == traded["ticker"] and leg["side"] == traded["side"] else ""
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
