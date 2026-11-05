"""
NWS data for KMIA: the Daily Summary Message (DSM) and NWS observations. They have ONE effect on the
models: resolve_known_high() works out the best provable lower bound for today's high from the NOAA
5-minute feed (whole degrees C, so only known to within 1-2 F), the NWS hourly reports (tenths of a degree C, so exact)
and the DSM (exact). See its docstring for the rules and guards.

  * DSM         the ASOS Daily Summary Message for Miami (product DSMMIA). NWS's own API (api.weather.gov) has no
                Miami DSM, so it is read from the Iowa Mesonet's mirror of the NWS text stream.

It is cached in memory. If a refresh fails, the last good copy is returned and marked "stale".

DSM format (one message, groups separated by "/", whitespace ignored; ASOS daily summary users guide):

    KMIA DS [HHMM] DD/MM  HHH+HHMM/ LLL+HHMM//  coopHigh/ coopLow//  minSLP+HHMM/ precip24h/ p01/../p24/ ...
                                                                    ^ hourly precip, hundredths of an inch

  * HHMM right after DS is the report time of an intermediate (partial-day) message; a final summary has none.
  * High / low are whole degrees F with the time they occurred. All times are LOCAL STANDARD TIME (ASOS clocks
    do not follow daylight saving), and the DSM "day" is midnight to midnight LST.
  * Precipitation is in hundredths of an inch ("T" = trace, "-" = hour not reached yet, "M" = missing).
  * Wind, sky cover and the trailing codes are not decoded; they are always visible in "raw".
"""
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from weather.asos_resolution import lower_bound_whole_f, possible_whole_f

STATION = "KMIA"
DSM_PIL = "DSMMIA"
IEM_BASE = "https://mesonet.agron.iastate.edu"
NWS_BASE = "https://api.weather.gov"
USER_AGENT = "kalshi-trading-bot/1.0"
HTTP_TIMEOUT_SEC = 15

# How long a fetched copy is reused. Kept short (the page polls every 60 s) so a new DSM shows up quickly;
# with several viewers the upstream service still sees at most one listing request per TTL.
DSM_TTL_SEC = 30
DSM_LOOKBACK_PRODUCTS = 6      # newest products inspected when looking for the latest and the last final summary
LST = timezone(timedelta(hours=-5), "LST")   # fixed offset: ASOS local standard time, never daylight saving
ET = ZoneInfo("America/New_York")
DSM_MAX_EXCESS_F = 4.0         # an extra bound more than this above the HFMETAR high is treated as bad data
NWS_READINGS_TTL_SEC = 30
NWS_READINGS_HOURS = 26
FLOOR_FAILURE_BACKOFF_SEC = 60  # after a failed DSM lookup, don't try again for this long (never slow the pollers)


# ── DSM parsing (pure) ────────────────────────────────────────────────────────

_HEADER = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4})\s+DS(?:\s+COR)?(?:\s+(\d{4}))?\s+(\d{2})/(\d{2})\s+(.*)$", re.S)


def _clean(text: str) -> str:
    """Drop the teletype framing characters; a message wraps after a '/', so such line breaks join the groups."""
    text = text.replace("\x01", "").replace("\x03", "").replace("\r", "")
    text = re.sub(r"/[ \t]*\n[ \t]*", "/", text)
    return text.replace("\n", " ").strip().split("=")[0].strip()


def _temp_and_time(group: str):
    """'851425' -> (85, '1425'); 'M' / '' / '-' -> (None, None)."""
    group = group.strip()
    m = re.fullmatch(r"(-?\d+)(\d{4})", group)
    if m:
        return int(m.group(1)), m.group(2)
    if re.fullmatch(r"-?\d+", group):
        return int(group), None
    return None, None


def _temp(group: str):
    group = group.strip()
    return int(group) if re.fullmatch(r"-?\d+", group) else None


def _hhmm(value):
    return None if not value else f"{value[:2]}:{value[2:]}"


def _precip(group: str):
    """Hundredths of an inch -> (inches or None, trace?)."""
    group = group.strip()
    if group == "T":
        return 0.0, True
    if re.fullmatch(r"\d{1,4}", group):
        return int(group) / 100.0, False
    return None, False          # "-" (hour not reached yet) or "M"


def _min_slp(group: str):
    """'9891622' -> (29.89 inHg, '1622'). The value is hundredths of inHg with the leading '2'/'3' dropped."""
    m = re.fullmatch(r"(\d{3})(\d{4})?", group.strip())
    if not m:
        return None, None
    value = int(m.group(1))
    return round((20 + value / 100.0) if value >= 500 else (30 + value / 100.0), 2), m.group(2)


def parse_dsm(text: str, issued_at: datetime = None):
    """
    One DSM message -> dict, or None if it does not look like a DSM. `issued_at` (UTC) is when the message was
    issued; it is used only to pick the year. Never raises on odd input.
    """
    if not text:
        return None
    match = _HEADER.search(_clean(text))
    if not match:
        return None
    station, report_time, day, month, body = match.groups()
    f = [part.strip() for part in body.split("/")]
    if len(f) < 8:
        return None

    high, high_time = _temp_and_time(f[0])
    low, low_time = _temp_and_time(f[1])
    slp, slp_time = _min_slp(f[6]) if len(f) > 6 else (None, None)
    total, total_trace = _precip(f[7])
    hourly = []
    for hour_ending, group in enumerate(f[8:32], start=1):
        inches, trace = _precip(group)
        hourly.append({"hour_ending": hour_ending, "inches": inches, "trace": trace})

    # The day/month of the summary, with the year taken from when the message was issued.
    issued = (issued_at or datetime.now(timezone.utc)).astimezone(LST)
    try:
        summary_date = date(issued.year, int(month), int(day))
        if summary_date > issued.date() + timedelta(days=1):    # e.g. a 12/31 summary issued on 1 Jan
            summary_date = date(issued.year - 1, int(month), int(day))
    except ValueError:
        return None

    return {
        "station": station,
        "kind": "partial" if report_time else "final",
        "report_time_lst": _hhmm(report_time),
        "date": summary_date.isoformat(),
        "high_f": high, "high_time_lst": _hhmm(high_time),
        "low_f": low, "low_time_lst": _hhmm(low_time),
        "coop_high_f": _temp(f[3]), "coop_low_f": _temp(f[4]),
        "min_slp_inhg": slp, "min_slp_time_lst": _hhmm(slp_time),
        "precip_in": total, "precip_trace": total_trace,
        "hourly_precip": hourly,
    }


# ── caching ───────────────────────────────────────────────────────────────────

_cache = {}                 # key -> (monotonic time, value)
_locks = {}
_locks_guard = threading.Lock()
_text_cache = {}            # product_id -> text (a published product never changes)


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _cached(key, ttl, loader):
    """Fresh cached value, else loader(). On a loader failure return the old value flagged stale, else re-raise."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    with _lock_for(key):
        hit = _cache.get(key)                       # another thread may have refreshed while we waited
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            value = loader()
        except Exception as exc:
            if hit:
                return {**hit[1], "stale": True, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            raise
        _cache[key] = (time.monotonic(), value)
        return value


def _get(url, **kwargs):
    # no-cache: ask any intermediate cache for a fresh copy (both services advertise max-age hints)
    headers = {"User-Agent": USER_AGENT, "Cache-Control": "no-cache", "Pragma": "no-cache", **kwargs.pop("headers", {})}
    response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SEC, **kwargs)
    response.raise_for_status()
    return response


# ── DSM ───────────────────────────────────────────────────────────────────────

def _dsm_products_for(day):
    """[(issued_at UTC, product_id)] newest first for one UTC date."""
    payload = _get(f"{IEM_BASE}/api/1/nws/afos/list.json", params={"pil": DSM_PIL, "date": day.isoformat()}).json()
    found = []
    for row in payload.get("data", []):
        try:
            issued = datetime.strptime(row["entered"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            found.append((issued, row["product_id"]))
        except (KeyError, ValueError):
            continue
    return sorted(found, reverse=True)


def _product_text(product_id):
    if product_id not in _text_cache:
        if len(_text_cache) > 50:
            _text_cache.clear()
        _text_cache[product_id] = _get(f"{IEM_BASE}/api/1/nwstext/{product_id}").text
    return _text_cache[product_id]


def _load_dsm(now=None):
    now = now or datetime.now(timezone.utc)
    latest, last_final = None, None
    inspected = 0
    # Today's listing first; earlier days are only requested while the last final summary is still missing.
    for offset in range(3):
        for issued, product_id in _dsm_products_for((now - timedelta(days=offset)).date()):
            if inspected >= DSM_LOOKBACK_PRODUCTS or (latest and last_final):
                break
            inspected += 1
            text = _product_text(product_id)
            parsed = parse_dsm(text, issued)
            if parsed is None:
                continue
            entry = {**parsed, "issued_at": issued.isoformat(), "product_id": product_id, "raw": _clean(text) + "="}
            if latest is None:
                latest = entry
            if entry["kind"] == "final" and last_final is None:
                last_final = entry
        if inspected >= DSM_LOOKBACK_PRODUCTS or (latest and last_final):
            break
    return {
        "station": STATION, "source": "NWS ASOS Daily Summary (DSMMIA) via Iowa Mesonet",
        "fetched_at": now.isoformat(), "latest": latest, "last_final": last_final, "stale": False,
        "note": "DSM times are local standard time (no daylight saving); the DSM day runs midnight to midnight LST.",
    }


def get_dsm():
    return _cached("dsm", DSM_TTL_SEC, _load_dsm)


# ── today's high: the best lower bound we can prove ───────────────────────────

_dsm_fail_until = 0.0
_nws_fail_until = 0.0
_decision_lock = threading.Lock()
_decision = {"used": False, "reason": "not evaluated yet"}
_decision_logged = None


def last_floor_decision():
    """The most recent resolve_known_high() outcome, for display (a copy)."""
    with _decision_lock:
        return dict(_decision)


def _record(raised, reason, **fields):
    global _decision_logged
    entry = {"used": raised, "reason": reason, "evaluated_at": datetime.now(timezone.utc).isoformat(), **fields}
    key = (fields.get("source"), fields.get("effective_high_f"))
    with _decision_lock:
        _decision.clear()
        _decision.update(entry)
        changed = key != _decision_logged
        _decision_logged = key
    if changed:                                     # log changes only, not every poll
        print(f"[known-high] {reason}", flush=True)


def _day_start(now, tz):
    local = now.astimezone(tz)
    return datetime(local.year, local.month, local.day, tzinfo=tz).astimezone(timezone.utc)


def _todays_dsm_high(now):
    """(entry, reason): the newest DSM if it is for TODAY'S climate day, else (None, why not). Never raises."""
    global _dsm_fail_until
    if time.monotonic() < _dsm_fail_until:
        return None, "DSM source unavailable (retrying shortly)"
    try:
        data = get_dsm()
    except Exception:
        _dsm_fail_until = time.monotonic() + FLOOR_FAILURE_BACKOFF_SEC
        return None, "DSM source unavailable"
    latest = (data or {}).get("latest")
    if not latest or latest.get("high_f") is None:
        return None, "no DSM published yet"
    # The DSM day is midnight-to-midnight local STANDARD time; the HFMETAR high is for the Eastern calendar day.
    # They only coincide from 1 AM EDT (summer), so require both to be today, otherwise it is another day's DSM.
    today = {now.astimezone(LST).date().isoformat(), now.astimezone(ET).date().isoformat()}
    if {latest["date"]} != today:
        return None, f"newest DSM is for {latest['date']}, not today"
    return latest, "ok"


def _load_nws_readings(now=None):
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=NWS_READINGS_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _get(f"{NWS_BASE}/stations/{STATION}/observations", params={"start": start, "limit": 500},
                   headers={"Accept": "application/geo+json"}).json()
    rows = []
    for feature in (payload or {}).get("features", []):
        p = feature.get("properties") or {}
        try:
            when = datetime.fromisoformat(p["timestamp"]).astimezone(timezone.utc)
            celsius = float((p.get("temperature") or {}).get("value"))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((when, celsius))
    return {"readings": rows, "fetched_at": now.isoformat(), "stale": False}


def _nws_high_today(now):
    """
    (whole-F lower bound of today's high from the NWS readings, reason). Hourly and special reports carry tenths of a
    degree C, which fixes the F value exactly (29.4C = 85F); the 5-minute rows are whole C and only give their lowest
    possible F value. Only readings from today's climate day count. Never raises.
    """
    global _nws_fail_until
    if time.monotonic() < _nws_fail_until:
        return None, "NWS readings unavailable (retrying shortly)"
    try:
        data = _cached("nws_readings", NWS_READINGS_TTL_SEC, _load_nws_readings)
    except Exception:
        _nws_fail_until = time.monotonic() + FLOOR_FAILURE_BACKOFF_SEC
        return None, "NWS readings unavailable"
    start = max(_day_start(now, LST), _day_start(now, ET))
    best = None
    for when, celsius in data.get("readings", []):
        if when < start or when > now + timedelta(minutes=5):
            continue
        low = lower_bound_whole_f(celsius * 9.0 / 5.0 + 32.0)
        best = low if best is None else max(best, low)
    return best, ("ok" if best is not None else "no NWS readings yet today")


def resolve_known_high(hfmetar_high_f, now=None):
    """
    Today's high to use, in whole degrees F (a LOWER bound: the highest value we can prove).

      * The NOAA 5-minute feed reports whole degrees C, so its high only narrows the true high to one or two whole-F
        values (84.2F means 84 or 85). Start from the lowest, so a bucket that is still alive is never marked dead.
      * The NWS hourly reports (tenths of a degree C: exact) and today's DSM (exact) are each a proven lower bound,
        because a day's high only goes up. When one is higher, use it.
      * Guards: a DSM must be for today's climate day; a bound more than DSM_MAX_EXCESS_F above the HFMETAR high is
        rejected as bad data; if a source fails it is simply left out. Never raises: on any error the HFMETAR value
        is returned unchanged.
    """
    try:
        if hfmetar_high_f is None:
            _record(False, "no HFMETAR high yet", source="none")
            return None
        now = now or datetime.now(timezone.utc)
        possible = possible_whole_f(hfmetar_high_f)
        low, high = min(possible), max(possible)
        best, source = float(low), "hfmetar"
        info = {"hfmetar_high_f": hfmetar_high_f, "hfmetar_possible_f": [low, high]}
        notes = []

        nws_high, nws_why = _nws_high_today(now)
        dsm_entry, dsm_why = _todays_dsm_high(now)
        bounds = []
        if nws_high is not None:
            info["nws_high_f"] = nws_high
            bounds.append(("nws_hourly", float(nws_high), f"NWS report {nws_high}F"))
        else:
            notes.append(nws_why)
        if dsm_entry is not None:
            info.update(dsm_high_f=dsm_entry["high_f"], dsm_issued_at=dsm_entry.get("issued_at"),
                        dsm_report_time_lst=dsm_entry.get("report_time_lst"))
            bounds.append(("dsm", float(dsm_entry["high_f"]), f"DSM {dsm_entry['high_f']}F"))
        else:
            notes.append(dsm_why)

        for name, value, label in bounds:
            if value - hfmetar_high_f > DSM_MAX_EXCESS_F:
                notes.append(f"{label} is implausibly far above HFMETAR {hfmetar_high_f}F (ignored)")
            elif value > best:
                best, source = value, name

        range_txt = f"{low}F" if low == high else f"{low} or {high}F"
        if source == "hfmetar":
            reason = (f"NOAA 5-minute feed {hfmetar_high_f}F = {range_txt}; using {int(best)}F"
                      if low != high else f"NOAA 5-minute feed {hfmetar_high_f}F is exactly {int(best)}F")
            if bounds:
                reason += f" ({', '.join(lbl for _, _, lbl in bounds)}: not higher)"
        else:
            label = {"nws_hourly": "NWS hourly report", "dsm": "DSM"}[source]
            reason = f"{label} shows {int(best)}F; NOAA 5-minute feed only shows {hfmetar_high_f}F ({range_txt})"
        if notes:
            reason += " - " + "; ".join(notes)
        info["effective_high_f"] = best
        _record(best > low, reason, source=source, **info)
        return best
    except Exception as exc:                        # this must never break the weather pipeline
        _record(False, f"error: {type(exc).__name__}", source="error")
        return hfmetar_high_f
