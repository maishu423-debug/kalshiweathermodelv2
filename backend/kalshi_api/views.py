import hmac
import json
import os

from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .algorithm import get_full_state
from .autotrader import get_status as autotrader_status
from .client import KalshiClientError, get_miami_temperature_payload
from .forecast import trigger_refresh, MODELS
from .price_tracker import record_price, nws_round_temp_f
from .paper import PaperTradeError, get_state, parse_order, place_order, reset_account, set_auto_enabled
from .sheets_history import get_recent_snapshots, get_recent_trades
from .stream import stream_events
from .temp_monitor import refresh_temp_now


@require_GET
def health(_request):
    return JsonResponse({"ok": True})


@require_GET
def debug_pem(_request):
    import os
    from cryptography.hazmat.primitives import serialization
    pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")
    key_bytes = pem.replace("\\n", "\n").encode("utf-8")
    try:
        serialization.load_pem_private_key(key_bytes, password=None)
        status = "ok"
        error = None
    except Exception as exc:
        status = "error"
        error = str(exc)
    return JsonResponse({
        "pem_len": len(pem),
        "pem_set": bool(pem),
        "first_40": repr(pem[:40]),
        "last_40": repr(pem[-40:]),
        "status": status,
        "error": error,
    })


@require_GET
def debug_ws(_request):
    """Test Kalshi WebSocket handshake — step by step connectivity diagnosis."""
    import asyncio
    import socket
    import ssl
    import requests as req
    import websockets
    from .auth import create_websocket_headers
    from .stream import KALSHI_WS_URL

    WS_HOST = "external-api-ws.kalshi.com"
    WS_PORT = 443

    result = {"ws_url": KALSHI_WS_URL}

    # Step 1: DNS
    try:
        ip = socket.gethostbyname(WS_HOST)
        result["dns_ok"] = True
        result["ip"] = ip
    except Exception as exc:
        result["dns_ok"] = False
        result["dns_error"] = str(exc)
        return JsonResponse(result)

    # Step 2: TCP + TLS (10 s)
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, WS_PORT), timeout=10) as raw_sock:
            with ctx.wrap_socket(raw_sock, server_hostname=WS_HOST):
                result["tcp_tls_ok"] = True
    except Exception as exc:
        result["tcp_tls_ok"] = False
        result["tcp_tls_error"] = type(exc).__name__ + ": " + str(exc)
        return JsonResponse(result)

    # Step 3: HTTP probe — GET the WS endpoint with and without auth headers (reveals if server responds at all)
    http_url = KALSHI_WS_URL.replace("wss://", "https://")
    try:
        r_no_auth = req.get(http_url, timeout=8)
        result["http_no_auth_status"] = r_no_auth.status_code
        result["http_no_auth_body"]   = r_no_auth.text[:200]
    except Exception as exc:
        result["http_no_auth_error"] = type(exc).__name__ + ": " + str(exc)

    try:
        headers = create_websocket_headers()
        result["headers_built"] = True
        r_auth = req.get(http_url, headers=headers, timeout=8)
        result["http_auth_status"] = r_auth.status_code
        result["http_auth_body"]   = r_auth.text[:200]
    except Exception as exc:
        result["http_auth_error"] = type(exc).__name__ + ": " + str(exc)

    # Step 4: WebSocket handshake (no auth first, to see if server responds with 401)
    async def _test_no_auth():
        try:
            async with websockets.connect(KALSHI_WS_URL, open_timeout=8) as ws:
                result["ws_no_auth_connected"] = True
        except Exception as exc:
            result["ws_no_auth_error"] = type(exc).__name__ + ": " + str(exc)

    async def _test_with_auth():
        try:
            hdrs = create_websocket_headers()
            from .client import fetch_json, get_today_event_ticker
            event_payload  = fetch_json(f"/events/{get_today_event_ticker()}")
            market_tickers = [m["ticker"] for m in event_payload.get("markets", [])][:2]
            async with websockets.connect(
                KALSHI_WS_URL,
                additional_headers=hdrs,
                open_timeout=15,
            ) as ws:
                import json as _json
                await ws.send(_json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["ticker"], "market_tickers": market_tickers},
                }))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                result["connected"] = True
                result["first_message"] = raw[:400] if isinstance(raw, str) else repr(raw[:400])
        except Exception as exc:
            result["connected"] = False
            result["ws_error"] = type(exc).__name__ + ": " + str(exc)

    # Step 5: Authenticated REST call — proves key ID + private key are a matching pair
    try:
        import time as _time
        from .auth import get_api_key_id, get_private_key, sign_pss_text
        _pk = get_private_key()
        _ts = str(int(_time.time() * 1000))
        _path = "/trade-api/v2/portfolio/balance"
        _sig = sign_pss_text(_pk, _ts + "GET" + _path)
        _rest_r = req.get(
            "https://external-api.kalshi.com" + _path,
            headers={
                "KALSHI-ACCESS-KEY": get_api_key_id(),
                "KALSHI-ACCESS-SIGNATURE": _sig,
                "KALSHI-ACCESS-TIMESTAMP": _ts,
            },
            timeout=8,
        )
        result["rest_auth_status"] = _rest_r.status_code
        result["rest_auth_body"]   = _rest_r.text[:300]
    except Exception as exc:
        result["rest_auth_error"] = type(exc).__name__ + ": " + str(exc)

    try:
        asyncio.run(_test_no_auth())
    except Exception as exc:
        result["asyncio_no_auth_error"] = str(exc)

    try:
        asyncio.run(_test_with_auth())
    except Exception as exc:
        result["asyncio_auth_error"] = str(exc)

    return JsonResponse(result)


@require_GET
def miami_temperature(request):
    selected_market_ticker = request.GET.get("market_ticker")

    try:
        payload = get_miami_temperature_payload(selected_market_ticker)
    except KalshiClientError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    return JsonResponse(payload)


@require_GET
def miami_temperature_stream(_request):
    response = StreamingHttpResponse(stream_events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@require_GET
def paper_state(_request):
    return JsonResponse(get_state())


@csrf_exempt
@require_POST
def paper_order(request):
    try:
        result = place_order(parse_order(request.body))
    except PaperTradeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse(result)


@csrf_exempt
@require_POST
def paper_reset(_request):
    return JsonResponse(reset_account())


# ── Algorithm endpoints ──────────────────────────────────────────────────────

_ALL_MODEL_NAMES = {m["name"] for m in MODELS}


def _parse_enabled_models(raw: str | None):
    """Parse comma-separated model names; return None if all enabled."""
    if not raw:
        return None
    names = {n.strip() for n in raw.split(",") if n.strip() in _ALL_MODEL_NAMES}
    return names if names else None


@require_GET
def algorithm_state(request):
    """Full algorithm state: forecasts, momentum analysis, recommendation."""
    enabled_models = _parse_enabled_models(request.GET.get("enabled_models"))

    try:
        payload = get_miami_temperature_payload(include_orderbook=False)
    except KalshiClientError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    # Feed current prices into the velocity tracker
    for m in payload["markets"]:
        record_price(m["ticker"], m.get("yes_bid_cents"), m.get("yes_ask_cents"))

    paper = get_state()
    state = get_full_state(payload["markets"], paper, enabled_models=enabled_models)
    return JsonResponse(state)


@require_GET
def autotrader_state(_request):
    """Paper account, positions, trades, bot cycles and schedule status."""
    return JsonResponse({**get_state(), "bot": autotrader_status()})


@csrf_exempt
@require_POST
def autotrader_toggle(request):
    try:
        body = json.loads(request.body.decode()) if request.body else {}
    except json.JSONDecodeError:
        body = {}
    set_auto_enabled(bool(body.get("enabled")))
    return JsonResponse({**get_state(), "bot": autotrader_status()})


@require_GET
def live_state(_request):
    """Real-money trading: switch, limits, today's usage and every live order."""
    from .live_trading import get_live_state

    return JsonResponse(get_live_state())


@csrf_exempt
@require_POST
def live_toggle(request):
    """Turn real-money trading on or off. Turning it on needs Kalshi credentials."""
    from .live_trading import credentials_configured, get_live_state, set_live_enabled

    try:
        body = json.loads(request.body.decode()) if request.body else {}
    except json.JSONDecodeError:
        body = {}
    enabled = body.get("enabled") is True
    if enabled and not credentials_configured():
        return JsonResponse(
            {"error": "Kalshi API key is not configured on the server (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PEM)."},
            status=400,
        )
    set_live_enabled(enabled)
    return JsonResponse(get_live_state())


@csrf_exempt
@require_POST
def autotrader_reset(_request):
    reset_account()
    return JsonResponse({**get_state(), "bot": autotrader_status()})


@require_GET
def forecast_history(request):
    """Recent forecast-cycle snapshots: current temp vs. model forecast at each timestamp."""
    limit = int(request.GET.get("limit", 100))
    return JsonResponse({"history": get_recent_snapshots(limit)})


@require_GET
def trade_history(request):
    """Recent paper trades, persisted to Google Sheets (survives Render restarts)."""
    limit = int(request.GET.get("limit", 100))
    return JsonResponse({"history": get_recent_trades(limit)})


@csrf_exempt
@require_POST
def algorithm_refresh(_request):
    """Kick off a manual forecast refresh (runs in background)."""
    trigger_refresh()
    return JsonResponse({"message": "Forecast refresh started."})


@csrf_exempt
@require_POST
def algorithm_cron_refresh(request):
    """Secret-protected forecast refresh for external cron schedulers."""
    expected = os.environ.get("CRON_REFRESH_SECRET", "").strip()
    provided = (
        request.GET.get("secret")
        or request.headers.get("X-Cron-Secret")
        or ""
    ).strip()

    if not expected:
        return JsonResponse({"error": "CRON_REFRESH_SECRET is not configured."}, status=503)
    if not hmac.compare_digest(provided, expected):
        return JsonResponse({"error": "Invalid cron secret."}, status=403)

    trigger_refresh()
    return JsonResponse({"message": "Cron forecast refresh started."})


@csrf_exempt
@require_POST
def algorithm_refresh_temp(_request):
    """Immediately fetch the latest weather reading and return updated temp."""
    asos = refresh_temp_now()
    if asos is None:
        return JsonResponse({"error": "Temperature refresh failed."}, status=502)
    high_f = asos.get("today_high_f")
    if high_f is None:
        high_f = asos.get("t0_f")
    return JsonResponse({
        "current_f":    round(float(asos["t0_f"]), 1),
        "daily_high_f": nws_round_temp_f(high_f),
        "observed_at":  asos.get("observed_at"),
    })
