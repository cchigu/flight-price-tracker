"""Checks the Google Flights adapter's query building and parsing without going online."""

from datetime import date

import pytest

ff = pytest.importorskip("fast_flights")
from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight
from fast_flights.parser import MetaList

from flighttracker.models import Passengers, SearchOptions
from flighttracker.providers import google_flights as gf
from flighttracker.providers.base import ProviderBlocked, ProviderError


def seg(a, b, d, t1, t2, dur):
    return SingleFlight(Airport(a, a), Airport(b, b), SimpleDatetime(d, t1), SimpleDatetime(d, t2), dur, "A321", "DL", "123")


def fake_results():
    r = MetaList([
        Flights("", 412, ["Delta"], [seg("FLL", "ATL", (2027, 1, 10), (8, 0), (10, 0), 120),
                                      seg("ATL", "MSY", (2027, 1, 10), (11, 30), (12, 45), 75)],
                CarbonEmission(0, 0), select_token="tok", select_data=None),
        Flights("", 0, ["Spirit"], [seg("FLL", "MSY", (2027, 1, 10), (9, 0), (10, 5), 125)], CarbonEmission(0, 0)),
    ])
    return r


def test_parses_round_trip_options(monkeypatch):
    seen = {}

    def fake_get(q, proxy=None):
        seen["url"] = q.url()
        return fake_results()

    monkeypatch.setattr(gf.ff, "get_flights", fake_get)
    p = gf.GoogleFlightsProvider()
    out = p.search("FLL", "MSY", date(2027, 1, 10), date(2027, 1, 30),
                   SearchOptions(cabin="business", passengers=Passengers(adults=2), currency="EUR"))
    assert len(out) == 1  # the option with no price is skipped
    it = out[0]
    assert it.price == 412 and it.currency == "EUR"
    assert it.outbound.stops == 1 and it.outbound.via == ["ATL"]
    assert it.outbound.total_duration_min == 120 + 90 + 75
    assert "curr=EUR" in seen["url"] and it.search_url == seen["url"]


def test_direct_sends_zero_stop_limit():
    p = gf.GoogleFlightsProvider()
    q = p._query("FLL", "MSY", date(2027, 1, 10), date(2027, 1, 20), SearchOptions(stops="direct"))
    assert all(f.max_stops == 0 for f in q._flights)
    assert len(q._flights) == 2


def test_block_and_error_mapping(monkeypatch):
    p = gf.GoogleFlightsProvider()

    def blocked(*a, **k):
        raise RuntimeError("Google Flights returned HTTP 429. The request may have been blocked or rate-limited.")

    def broken(*a, **k):
        raise ValueError("parse failure")

    monkeypatch.setattr(gf.ff, "get_flights", blocked)
    with pytest.raises(ProviderBlocked):
        p.search("FLL", "MSY", date(2027, 1, 10), None, SearchOptions())
    monkeypatch.setattr(gf.ff, "get_flights", broken)
    with pytest.raises(ProviderError):
        p.search("FLL", "MSY", date(2027, 1, 10), None, SearchOptions())
