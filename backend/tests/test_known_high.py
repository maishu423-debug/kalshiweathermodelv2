"""
Today's high = the highest value we can PROVE: the NOAA 5-minute feed as a range (lowest value), raised by the exact NWS
hourly-report high and by today's DSM high when those are higher.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import nws  # noqa: E402

# 2026-11-05 18:00Z = 1:00 PM EST: LST and Eastern dates agree, and the climate day began at 05:00Z.
NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
TODAY = "2026-11-05"


def dsm(high=85, date=TODAY, kind="partial", report="15:00"):
    entry = {"kind": kind, "date": date, "high_f": high, "issued_at": "2026-11-05T20:11:00+00:00",
             "report_time_lst": report, "product_id": "P"}
    return {"latest": entry, "last_final": None, "stale": False}


def reading(minutes_before_now, celsius, now=NOW):
    return (now - timedelta(minutes=minutes_before_now), celsius)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(nws, "_dsm_fail_until", 0.0)
    monkeypatch.setattr(nws, "_nws_fail_until", 0.0)
    nws._cache.clear()
    # by default no extra sources: only the NOAA 5-minute high
    monkeypatch.setattr(nws, "get_dsm", lambda: {"latest": None})
    monkeypatch.setattr(nws, "_load_nws_readings", lambda now=None: {"readings": [], "stale": False})
    yield
    nws._cache.clear()


def use_dsm(monkeypatch, payload):
    monkeypatch.setattr(nws, "get_dsm", lambda: payload)


def use_nws(monkeypatch, rows):
    monkeypatch.setattr(nws, "_load_nws_readings", lambda now=None: {"readings": rows, "stale": False})


# ── step 1: the NOAA 5-minute high is a range; use its lowest value ───────────

@pytest.mark.parametrize("hf,expected", [(84.2, 84.0), (87.8, 87.0), (82.4, 82.0), (80.6, 80.0), (86.0, 86.0), (89.6, 89.0)])
def test_with_no_other_source_the_lowest_possible_value_is_used(hf, expected):
    assert nws.resolve_known_high(hf, NOW) == expected
    decision = nws.last_floor_decision()
    assert decision["source"] == "hfmetar" and decision["used"] is False and decision["effective_high_f"] == expected


def test_no_hfmetar_high_means_nothing_to_resolve():
    assert nws.resolve_known_high(None, NOW) is None


# ── step 2: the NWS hourly report (tenths of a degree C) is exact ─────────────

def test_an_hourly_report_with_tenths_pins_the_high_exactly(monkeypatch):
    use_nws(monkeypatch, [reading(130, 29.4), reading(10, 29.0)])      # 29.4C = 85F; the 5-minute row is 29C
    assert nws.resolve_known_high(84.2, NOW) == 85.0                   # today's real case: 5-minute feed said 84.2F
    decision = nws.last_floor_decision()
    assert decision["source"] == "nws_hourly" and decision["used"] is True and decision["nws_high_f"] == 85
    assert decision["hfmetar_possible_f"] == [84, 85] and "NWS hourly report shows 85F" in decision["reason"]


def test_whole_celsius_rows_from_the_nws_give_only_their_lowest_value(monkeypatch):
    use_nws(monkeypatch, [reading(10, 29.0), reading(70, 29.0)])
    assert nws.resolve_known_high(84.2, NOW) == 84.0
    assert nws.last_floor_decision()["source"] == "hfmetar"


def test_the_highest_nws_report_of_the_day_counts_not_the_latest(monkeypatch):
    use_nws(monkeypatch, [reading(200, 29.4), reading(20, 28.9)])      # it cooled since the 85F report
    assert nws.resolve_known_high(84.2, NOW) == 85.0


def test_an_nws_report_that_is_not_higher_changes_nothing(monkeypatch):
    use_nws(monkeypatch, [reading(60, 28.9)])                          # 28.9C = 84F
    assert nws.resolve_known_high(87.8, NOW) == 87.0


def test_nws_readings_from_before_today_are_ignored(monkeypatch):
    use_nws(monkeypatch, [(datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc), 31.7)])    # yesterday's 89F
    assert nws.resolve_known_high(84.2, NOW) == 84.0


def test_in_summer_readings_before_local_standard_midnight_are_ignored(monkeypatch):
    # 02:00 EDT on 15 Jul = 01:00 LST: the climate day started at 05:00Z, the Eastern day at 04:00Z.
    now = datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)
    use_nws(monkeypatch, [(datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc), 27.2),     # 04:30Z = 81F: still yesterday's LST day
                          (datetime(2026, 7, 15, 5, 30, tzinfo=timezone.utc), 26.1)])    # 05:30Z = 79F: today's climate day
    # both readings are within the 4F sanity cap of the 78.8F feed, so only the day window can exclude the 81F one
    assert nws.resolve_known_high(78.8, now) == 79.0
    assert nws.last_floor_decision()["nws_high_f"] == 79


# ── the DSM, as before: it raises the high when higher ────────────────────────

def test_a_higher_dsm_high_raises_the_high(monkeypatch):
    use_dsm(monkeypatch, dsm(high=85))
    assert nws.resolve_known_high(84.2, NOW) == 85.0
    assert nws.last_floor_decision()["source"] == "dsm"


@pytest.mark.parametrize("hf,dsm_high,expected", [(87.8, 87, 87.0), (87.8, 84, 87.0), (84.2, 84, 84.0), (90.0, 90, 90.0)])
def test_a_dsm_that_is_not_higher_is_not_used(monkeypatch, hf, dsm_high, expected):
    use_dsm(monkeypatch, dsm(high=dsm_high))
    assert nws.resolve_known_high(hf, NOW) == expected
    assert nws.last_floor_decision()["used"] is False


def test_the_highest_of_all_sources_wins(monkeypatch):
    use_nws(monkeypatch, [reading(130, 29.4)])        # 85F
    use_dsm(monkeypatch, dsm(high=86))
    assert nws.resolve_known_high(84.2, NOW) == 86.0
    assert nws.last_floor_decision()["source"] == "dsm"


def test_yesterdays_dsm_is_ignored(monkeypatch):
    use_dsm(monkeypatch, dsm(high=88, date="2026-11-04", kind="final", report=None))
    assert nws.resolve_known_high(84.2, NOW) == 84.0
    assert "not today" in nws.last_floor_decision()["reason"]


def test_in_summer_between_midnight_and_1am_edt_no_dsm_date_can_match(monkeypatch):
    now = datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc)           # 00:30 EDT = 23:30 LST the day before
    for date in ("2026-07-14", "2026-07-15"):
        use_dsm(monkeypatch, dsm(high=90, date=date))
        assert nws.resolve_known_high(88.0, now) == 88.0


# ── guards ────────────────────────────────────────────────────────────────────

def test_an_implausibly_high_bound_is_rejected(monkeypatch):
    use_dsm(monkeypatch, dsm(high=90))
    use_nws(monkeypatch, [reading(60, 33.3)])                          # 91.9F
    assert nws.resolve_known_high(80.6, NOW) == 80.0
    assert nws.last_floor_decision()["reason"].count("implausibly") == 2


def test_a_bound_exactly_at_the_limit_is_accepted(monkeypatch):
    use_dsm(monkeypatch, dsm(high=84))
    assert nws.resolve_known_high(80.0, NOW) == 84.0


# ── a failing source must never break anything ────────────────────────────────

def test_failing_sources_fall_back_to_the_noaa_range_and_back_off(monkeypatch):
    calls = {"dsm": 0, "nws": 0}

    def dsm_boom():
        calls["dsm"] += 1
        raise RuntimeError("iem down")

    def nws_boom(now=None):
        calls["nws"] += 1
        raise RuntimeError("nws down")

    monkeypatch.setattr(nws, "get_dsm", dsm_boom)
    monkeypatch.setattr(nws, "_load_nws_readings", nws_boom)
    assert nws.resolve_known_high(84.2, NOW) == 84.0
    assert nws.resolve_known_high(84.2, NOW) == 84.0
    assert calls == {"dsm": 1, "nws": 1}                               # backed off: pollers are not slowed down
    assert "unavailable" in nws.last_floor_decision()["reason"]
    monkeypatch.setattr(nws, "_dsm_fail_until", time.monotonic() - 1)
    use_dsm(monkeypatch, dsm(high=85))
    assert nws.resolve_known_high(84.2, NOW) == 85.0                   # recovers after the back-off


@pytest.mark.parametrize("payload", [{}, {"latest": None}, {"latest": {"high_f": None, "date": TODAY}}, None])
def test_empty_dsm_payloads_leave_the_noaa_range_alone(monkeypatch, payload):
    use_dsm(monkeypatch, payload)
    assert nws.resolve_known_high(84.2, NOW) == 84.0


def test_garbage_in_a_source_cannot_raise(monkeypatch):
    use_dsm(monkeypatch, {"latest": {"high_f": "hot", "date": TODAY}})
    assert nws.resolve_known_high(84.2, NOW) == 84.2                   # unexpected error: the input comes back unchanged


# ── wired into every place the high is used ───────────────────────────────────

def _real_today_or_skip():
    now = datetime.now(timezone.utc)
    if now.astimezone(nws.LST).date() != now.astimezone(nws.ET).date():
        pytest.skip("LST and Eastern dates differ right now (summer, 00:00-01:00 EDT): the DSM is correctly ignored")
    return now


def test_live_monitor_uses_the_resolved_high(monkeypatch):
    from kalshi_api import temp_monitor

    now = _real_today_or_skip()
    observations = [{"observed_at": (now - timedelta(minutes=m)).isoformat(), "temp_f": f} for m, f in ((40, 84.2), (10, 82.4))]
    monkeypatch.setattr(temp_monitor, "fetch_recent_observations", lambda now=None, reuse_seconds=0: (observations, {}))
    use_nws(monkeypatch, [(now - timedelta(minutes=30), 29.4)])        # exact 85F from an hourly report
    out = temp_monitor.fetch_current_high()
    assert out["t0_f"] == 82.4 and out["today_high_f"] == 85.0         # current temp untouched; the high is 85, not 84.2


def test_live_monitor_without_extra_sources_uses_the_lowest_possible_high(monkeypatch):
    from kalshi_api import temp_monitor

    now = _real_today_or_skip()
    observations = [{"observed_at": (now - timedelta(minutes=10)).isoformat(), "temp_f": 87.8}]
    monkeypatch.setattr(temp_monitor, "fetch_recent_observations", lambda now=None, reuse_seconds=0: (observations, {}))
    assert temp_monitor.fetch_current_high()["today_high_f"] == 87.0


def test_model_input_uses_the_resolved_high(monkeypatch):
    import weather
    from kalshi_api import forecast

    now = _real_today_or_skip()
    obs = {"observed_at": (now - timedelta(minutes=5)).isoformat(), "temp_f": 84.2, "relative_humidity": 0.7,
           "cloud_fraction": 0.2, "wind_speed_mph": 5.0, "wind_dir_deg": 90.0, "pressure_hpa": 1015.0}
    shared = {"metadata": {"status": "healthy", "source": "TEST"}, "current": obs, "recent_observations": [obs],
              "today": {"high_f": 84.2}, "features": {"pressure_trend_hpa_per_hr": 0.1}}
    monkeypatch.setattr(weather, "build_live_shared_input", lambda: shared)
    use_nws(monkeypatch, [(now - timedelta(minutes=30), 29.4)])
    data, _ = forecast._fetch_hfmetar_shared()
    assert data["asos_current"]["today_high_f"] == 85.0                # what every model script floors its paths at
    assert data["asos_current"]["t0_f"] == 84.2


def test_the_recorded_high_is_not_pushed_up_by_a_rounded_up_current_reading(monkeypatch):
    from kalshi_api import price_tracker as pt

    monkeypatch.setattr(pt, "_daily_high_f", None)
    monkeypatch.setattr(pt, "_date_str", None)
    monkeypatch.setattr(pt, "_current_f", None)
    pt.update_temp(87.8)                                               # 31C: really 87 or 88
    snap = pt.get_temp_snapshot()
    assert snap["current_f"] == 87.8 and snap["daily_high_f"] == 87.0
    pt.update_temp(86.0)
    assert pt.get_temp_snapshot()["daily_high_f"] == 87.0              # never goes down
    pt.set_daily_high_nws(88.0)                                        # an exact bound (e.g. a DSM) still raises it
    assert pt.get_temp_snapshot()["daily_high_f"] == 88.0


def test_the_strategy_uses_the_lowest_provable_high():
    from kalshi_api import algorithm as alg

    assert alg._nws_known_high_f(87.8, None) == 87
    assert alg._nws_known_high_f(87.8, 88.0) == 88                     # an exact whole-F bound wins
    assert alg._nws_known_high_f(84.2, 85.0) == 85
    assert alg._nws_known_high_f(None, None) is None
    pdf = alg._compute_daily_high_pdf({}, alg._nws_known_high_f(87.8, None), {85: 0.2, 87: 0.5, 90: 0.3})
    assert min(pdf) == 87 and pdf[87] == pytest.approx(0.7)            # not 88: the 87-88 bucket keeps its mass at 87


def test_a_bucket_is_only_dead_when_the_day_has_provably_passed_it(monkeypatch):
    from kalshi_api import algorithm as alg

    monkeypatch.setattr(alg, "get_temp_snapshot", lambda: {"current_f": 84.2, "daily_high_f": 84.0})
    monkeypatch.setattr(alg, "get_all_velocities", lambda tickers: {})
    markets = [
        {"ticker": "b83", "floor_strike": 83, "cap_strike": 84, "yes_ask_cents": 40, "no_ask_cents": 61},   # "83 to 84"
        {"ticker": "b85", "floor_strike": 85, "cap_strike": 86, "yes_ask_cents": 40, "no_ask_cents": 61},   # "85 to 86"
    ]
    dead = {a["ticker"]: a["is_dead"] for a in alg.analyze_markets(markets)}
    # 84.2F could be 85F, but it could also be 84F: the 83-84 bucket is not provably lost, so it stays alive
    assert dead == {"b83": False, "b85": False}


# ── the unrounded high, shown next to the recorded high ───────────────────────

def test_the_raw_high_is_the_hottest_unrounded_reading(monkeypatch):
    use_nws(monkeypatch, [reading(130, 29.4), reading(10, 29.0)])           # 29.4C = 84.92F, the 5-minute rows are 29C
    high, raw = nws.resolve_known_high_detail(84.2, NOW)
    assert high == 85.0 and raw == pytest.approx(84.92)                      # "85F" on screen, 84.9F beside it
    assert nws.last_floor_decision()["raw_high_f"] == pytest.approx(84.92)


def test_the_raw_high_without_the_nws_is_the_noaa_reading():
    assert nws.resolve_known_high_detail(84.2, NOW) == (84.0, 84.2)


def test_a_cooler_nws_reading_does_not_lower_the_raw_high(monkeypatch):
    use_nws(monkeypatch, [reading(60, 28.9)])                                # 84.02F
    assert nws.resolve_known_high_detail(87.8, NOW) == (87.0, 87.8)


def test_no_high_means_no_raw_high():
    assert nws.resolve_known_high_detail(None, NOW) == (None, None)


def test_on_an_error_the_raw_high_is_the_input(monkeypatch):
    use_dsm(monkeypatch, {"latest": {"high_f": "hot", "date": TODAY}})
    assert nws.resolve_known_high_detail(84.2, NOW) == (84.2, 84.2)


def test_resolve_known_high_still_returns_just_the_high(monkeypatch):
    use_nws(monkeypatch, [reading(130, 29.4)])
    assert nws.resolve_known_high(84.2, NOW) == 85.0


def test_the_tracker_keeps_the_hottest_unrounded_reading_and_resets_each_day(monkeypatch):
    from kalshi_api import price_tracker as pt

    for name in ("_daily_high_f", "_date_str", "_current_f", "_daily_high_raw_f", "_raw_date_str"):
        monkeypatch.setattr(pt, name, None)
    pt.update_temp(84.2)                                                     # a reading is also a raw candidate
    assert pt.get_temp_snapshot()["daily_high_raw_f"] == 84.2
    pt.set_daily_high_raw(84.92)
    assert pt.get_temp_snapshot()["daily_high_raw_f"] == 84.92
    pt.set_daily_high_raw(80.0)
    pt.update_temp(82.4)
    assert pt.get_temp_snapshot()["daily_high_raw_f"] == 84.92              # never goes down within a day
    pt.set_daily_high_raw(None)                                              # ignored
    assert pt.get_temp_snapshot()["daily_high_raw_f"] == 84.92
    monkeypatch.setattr(pt, "_today_key", lambda: "2099-01-01")              # the next day
    assert pt.get_temp_snapshot()["daily_high_raw_f"] is None               # yesterday's value is not shown
    pt.set_daily_high_raw(79.0)
    assert pt.get_temp_snapshot()["daily_high_raw_f"] == 79.0               # and a new day starts fresh


def test_the_page_data_carries_the_rounded_and_the_unrounded_high(monkeypatch):
    from kalshi_api import algorithm as alg

    monkeypatch.setattr(alg, "get_temp_snapshot",
                        lambda: {"current_f": 84.2, "daily_high_f": 85.0, "daily_high_raw_f": 84.92})
    monkeypatch.setattr(alg, "get_forecast_cache", lambda: {"results": {}})
    assert alg.get_full_state([])["temp"] == {"current_f": 84.2, "daily_high_f": 85, "daily_high_raw_f": 84.92}
    monkeypatch.setattr(alg, "get_temp_snapshot", lambda: {"current_f": 84.2, "daily_high_f": 85.0})
    assert alg.get_full_state([])["temp"]["daily_high_raw_f"] is None       # older snapshots without it still work


def test_live_monitor_passes_the_raw_high_to_the_tracker(monkeypatch):
    from kalshi_api import price_tracker as pt
    from kalshi_api import temp_monitor

    now = _real_today_or_skip()
    for name in ("_daily_high_f", "_date_str", "_current_f", "_daily_high_raw_f", "_raw_date_str"):
        monkeypatch.setattr(pt, name, None)
    observations = [{"observed_at": (now - timedelta(minutes=10)).isoformat(), "temp_f": 84.2}]
    monkeypatch.setattr(temp_monitor, "fetch_recent_observations", lambda now=None, reuse_seconds=0: (observations, {}))
    use_nws(monkeypatch, [(now - timedelta(minutes=30), 29.4)])
    out = temp_monitor.fetch_current_high()
    assert out["today_high_f"] == 85.0 and out["today_high_raw_f"] == pytest.approx(84.92)
    temp_monitor.apply_current_high(out)
    snap = pt.get_temp_snapshot()
    assert snap["daily_high_f"] == 85.0 and snap["daily_high_raw_f"] == pytest.approx(84.92)


def test_model_input_carries_the_raw_high(monkeypatch):
    import weather
    from kalshi_api import forecast
    from kalshi_api import price_tracker as pt

    now = _real_today_or_skip()
    for name in ("_daily_high_f", "_date_str", "_current_f", "_daily_high_raw_f", "_raw_date_str"):
        monkeypatch.setattr(pt, name, None)
    obs = {"observed_at": (now - timedelta(minutes=5)).isoformat(), "temp_f": 84.2, "relative_humidity": 0.7,
           "cloud_fraction": 0.2, "wind_speed_mph": 5.0, "wind_dir_deg": 90.0, "pressure_hpa": 1015.0}
    shared = {"metadata": {"status": "healthy", "source": "TEST"}, "current": obs, "recent_observations": [obs],
              "today": {"high_f": 84.2}, "features": {"pressure_trend_hpa_per_hr": 0.1}}
    monkeypatch.setattr(weather, "build_live_shared_input", lambda: shared)
    use_nws(monkeypatch, [(now - timedelta(minutes=30), 29.4)])
    data, _ = forecast._fetch_hfmetar_shared()
    assert data["asos_current"]["today_high_f"] == 85.0
    assert data["asos_current"]["today_high_raw_f"] == pytest.approx(84.92)
    forecast._apply_asos_current(data["asos_current"])
    assert pt.get_temp_snapshot()["daily_high_raw_f"] == pytest.approx(84.92)


# ── the unrounded value is only shown when it agrees with the recorded (rounded) high ─────────────

@pytest.mark.parametrize("raw,recorded_f,shown", [
    (84.92, 85.0, 84.92),     # 29.4C hourly report: rounds to the recorded 85, so both are shown
    (85.4, 85.0, 85.4),
    (75.2, 76.0, None),       # whole-C feed value (24C) beside a DSM-based 76: they disagree, so only 76 is shown
    (87.8, 87.0, None),       # the bot uses the lowest provable value (87) while the feed value rounds to 88
    (84.2, 84.0, 84.2),       # a whole-C value that does round to the recorded high is fine
    (None, 85.0, None),
])
def test_the_unrounded_value_is_shown_only_when_it_rounds_to_the_recorded_high(monkeypatch, raw, recorded_f, shown):
    from kalshi_api import algorithm as alg

    monkeypatch.setattr(alg, "get_temp_snapshot",
                        lambda: {"current_f": 80.0, "daily_high_f": recorded_f, "daily_high_raw_f": raw})
    monkeypatch.setattr(alg, "get_forecast_cache", lambda: {"results": {}})
    temp = alg.get_full_state([])["temp"]
    assert temp["daily_high_f"] == int(recorded_f)
    assert temp["daily_high_raw_f"] == shown
    if shown is not None:
        assert alg.nws_round_temp_f(temp["daily_high_raw_f"]) == temp["daily_high_f"]   # never contradicts the rounded value
