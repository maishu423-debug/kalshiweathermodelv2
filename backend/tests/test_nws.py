"""NWS display data for KMIA: DSM decoding, fetching and caching. Offline (no network)."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import nws  # noqa: E402

# Real DSMMIA messages as published (teletype framing included), newest first.
PARTIAL_0200 = (
    "\x01\nKMIA DS 0200 05/11 770008/ 750159// 85/ 75//9880159/00/00/00/-/-/-/-/\n"
    "-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/-/01050105/01060102=\n\n\x03"
)
FINAL_0411 = (
    "\x01\nKMIA DS 04/11 851425/ 762351// 85/ 76//9891622/150/00/00/00/05/00/00/\n"
    "00/09/52/51/05/02/01/00/00/21/03/01/00/00/00/00/00/00/62/09181028/\n"
    "07261510/13/NN/N/N/NN/EP EW=\n\n\x03"
)
PARTIAL_1600 = (
    "\x01\nKMIA DS 1600 04/11 851425/ 761125// 85/ 76//9891541/146/00/00/00/05/\n"
    "00/00/00/09/52/51/05/02/01/00/00/21/-/-/-/-/-/-/-/-/-/09181028/\n"
    "07261510/13/NN/N/N/NN/EP=\n\n\x03"
)
ISSUED_FINAL = datetime(2026, 11, 5, 5, 16, tzinfo=timezone.utc)      # 00:16 LST on 5 Nov
ISSUED_0200 = datetime(2026, 11, 5, 7, 21, tzinfo=timezone.utc)


# ── DSM decoding ──────────────────────────────────────────────────────────────

def test_final_summary_is_decoded():
    d = nws.parse_dsm(FINAL_0411, ISSUED_FINAL)
    assert d["station"] == "KMIA" and d["kind"] == "final" and d["report_time_lst"] is None
    assert d["date"] == "2026-11-04"
    assert (d["high_f"], d["high_time_lst"]) == (85, "14:25")
    assert (d["low_f"], d["low_time_lst"]) == (76, "23:51")
    assert (d["coop_high_f"], d["coop_low_f"]) == (85, 76)
    assert (d["min_slp_inhg"], d["min_slp_time_lst"]) == (29.89, "16:22")
    assert d["precip_in"] == pytest.approx(1.50)


def test_hourly_precip_adds_up_to_the_daily_total():
    d = nws.parse_dsm(FINAL_0411, ISSUED_FINAL)
    hours = d["hourly_precip"]
    assert len(hours) == 24 and hours[0]["hour_ending"] == 1 and hours[-1]["hour_ending"] == 24
    assert hours[3]["inches"] == pytest.approx(0.05) and hours[8]["inches"] == pytest.approx(0.52)
    assert sum(h["inches"] for h in hours) == pytest.approx(d["precip_in"])


def test_partial_message_has_a_report_time_and_unreached_hours_are_blank():
    d = nws.parse_dsm(PARTIAL_0200, ISSUED_0200)
    assert d["kind"] == "partial" and d["report_time_lst"] == "02:00"
    assert d["date"] == "2026-11-05"
    assert (d["high_f"], d["high_time_lst"]) == (77, "00:08")
    assert (d["low_f"], d["low_time_lst"]) == (75, "01:59")
    assert d["min_slp_inhg"] == 29.88 and d["precip_in"] == 0.0
    hours = d["hourly_precip"]
    assert [h["inches"] for h in hours[:2]] == [0.0, 0.0] and all(h["inches"] is None for h in hours[2:])


def test_afternoon_partial_keeps_the_running_low_not_the_final_one():
    d = nws.parse_dsm(PARTIAL_1600, datetime(2026, 11, 4, 21, 11, tzinfo=timezone.utc))
    assert d["kind"] == "partial" and d["report_time_lst"] == "16:00"
    assert (d["low_f"], d["low_time_lst"]) == (76, "11:25")
    assert d["precip_in"] == pytest.approx(1.46)


def test_trace_and_missing_values():
    text = "KMIA DS 04/11 M/ M// M/ M//M/T/T/M/-/00/" + "00/" * 20 + "00="
    d = nws.parse_dsm(text, ISSUED_FINAL)
    assert d["high_f"] is None and d["low_f"] is None and d["coop_high_f"] is None and d["min_slp_inhg"] is None
    assert d["precip_in"] == 0.0 and d["precip_trace"] is True
    assert d["hourly_precip"][0]["trace"] is True and d["hourly_precip"][1]["inches"] is None


def test_three_digit_and_pressure_above_30_inches():
    text = "KMIA DS 15/07 1021425/ 781000// 102/ 78//0051500/00/" + "00/" * 24 + "="
    d = nws.parse_dsm(text, datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc))
    assert (d["high_f"], d["high_time_lst"]) == (102, "14:25")
    assert d["min_slp_inhg"] == 30.05


def test_year_rolls_back_for_a_december_summary_issued_in_january():
    d = nws.parse_dsm(FINAL_0411.replace("04/11", "31/12"), datetime(2027, 1, 1, 5, 16, tzinfo=timezone.utc))
    assert d["date"] == "2026-12-31"


def test_a_header_line_before_the_message_is_ignored():
    d = nws.parse_dsm("CDUS27 KZMA 050721\n" + FINAL_0411, ISSUED_FINAL)
    assert d is not None and d["high_f"] == 85


@pytest.mark.parametrize("junk", ["", None, "hello", "KMIA DS 99/99 85/", "KMIA DS 04/11 851425/ 762351="])
def test_garbage_is_rejected_without_raising(junk):
    assert nws.parse_dsm(junk, ISSUED_FINAL) is None


# ── Fetching and caching ──────────────────────────────────────────────────────

class Resp:
    def __init__(self, payload=None, text=""):
        self._payload, self.text = payload, text

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _fresh_cache():
    nws._cache.clear()
    nws._text_cache.clear()
    yield
    nws._cache.clear()
    nws._text_cache.clear()


def test_get_dsm_returns_the_latest_and_the_last_final(monkeypatch):
    listing = {"data": [
        {"entered": "2026-11-05T05:16:00Z", "product_id": "P-FINAL"},
        {"entered": "2026-11-05T07:21:00Z", "product_id": "P-0200"},
    ]}
    texts = {"P-FINAL": FINAL_0411, "P-0200": PARTIAL_0200}
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("list.json"):
            return Resp(listing if kwargs["params"]["date"] == "2026-11-05" else {"data": []})
        return Resp(text=texts[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(nws, "_get", fake_get)
    out = nws._load_dsm(datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc))
    assert out["latest"]["product_id"] == "P-0200" and out["latest"]["kind"] == "partial"
    assert out["last_final"]["product_id"] == "P-FINAL" and out["last_final"]["date"] == "2026-11-04"
    assert out["latest"]["issued_at"].startswith("2026-11-05T07:21")
    assert out["latest"]["raw"].startswith("KMIA DS 0200") and out["latest"]["raw"].endswith("=")
    assert "\x01" not in out["latest"]["raw"]
    assert out["stale"] is False


def test_only_todays_listing_is_requested_when_it_already_has_the_final_summary(monkeypatch):
    dates = []

    def fake_get(url, **kwargs):
        if url.endswith("list.json"):
            dates.append(kwargs["params"]["date"])
            return Resp({"data": [{"entered": "2026-11-05T05:16:00Z", "product_id": "P-FINAL"},
                                  {"entered": "2026-11-05T07:21:00Z", "product_id": "P-0200"}]})
        return Resp(text={"P-FINAL": FINAL_0411, "P-0200": PARTIAL_0200}[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(nws, "_get", fake_get)
    nws._load_dsm(datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc))
    assert dates == ["2026-11-05"]


def test_earlier_days_are_searched_while_the_final_summary_is_missing(monkeypatch):
    dates = []

    def fake_get(url, **kwargs):
        if url.endswith("list.json"):
            day = kwargs["params"]["date"]
            dates.append(day)
            rows = {"2026-11-05": [{"entered": "2026-11-05T07:21:00Z", "product_id": "P-0200"}],
                    "2026-11-04": [{"entered": "2026-11-04T21:11:00Z", "product_id": "P-1600"},
                                   {"entered": "2026-11-04T05:16:00Z", "product_id": "P-FINAL"}]}
            return Resp({"data": rows.get(day, [])})
        return Resp(text={"P-FINAL": FINAL_0411, "P-0200": PARTIAL_0200, "P-1600": PARTIAL_1600}[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(nws, "_get", fake_get)
    out = nws._load_dsm(datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc))
    assert dates == ["2026-11-05", "2026-11-04"]
    assert out["latest"]["product_id"] == "P-0200" and out["last_final"]["product_id"] == "P-FINAL"


def test_requests_ask_for_uncached_responses(monkeypatch):
    seen = {}

    class R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(nws.requests, "get", lambda url, headers=None, **kw: seen.update(headers=headers) or R())
    nws._get("https://example.test/x")
    assert seen["headers"]["Cache-Control"] == "no-cache" and seen["headers"]["User-Agent"] == nws.USER_AGENT


def test_refresh_window_is_short():
    assert nws.DSM_TTL_SEC <= 60


def test_get_dsm_with_nothing_published_yet(monkeypatch):
    monkeypatch.setattr(nws, "_get", lambda url, **kw: Resp({"data": []}))
    out = nws._load_dsm(datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc))
    assert out["latest"] is None and out["last_final"] is None


def test_product_text_is_fetched_once(monkeypatch):
    calls = []
    monkeypatch.setattr(nws, "_get", lambda url, **kw: calls.append(url) or Resp(text="x"))
    nws._product_text("P1")
    nws._product_text("P1")
    assert len(calls) == 1


def test_cache_serves_fresh_data_without_reloading():
    loads = []
    loader = lambda: loads.append(1) or {"v": len(loads)}   # noqa: E731
    assert nws._cached("k", 60, loader) == {"v": 1}
    assert nws._cached("k", 60, loader) == {"v": 1}
    assert len(loads) == 1


def test_a_failed_refresh_returns_the_old_copy_marked_stale():
    nws._cache["k"] = (0.0, {"v": 1, "stale": False})       # long expired

    def boom():
        raise RuntimeError("upstream down")

    out = nws._cached("k", 60, boom)
    assert out["v"] == 1 and out["stale"] is True and "upstream down" in out["error"]


def test_a_failed_first_load_raises():
    def boom():
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        nws._cached("never-loaded", 60, boom)
