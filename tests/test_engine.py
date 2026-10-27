from datetime import date, datetime, timedelta

import pytest

from flighttracker.engine import FlexibleSearch, Throttle, pick_top, run_search
from flighttracker.models import Itinerary, Leg, Passengers, SearchOptions, Segment, TripResult
from flighttracker.providers.base import PriceProvider, ProviderBlocked, ProviderError
from flighttracker.providers.fake import FakeProvider

TODAY = date.today()
NO_WAIT = Throttle(0, 0, block_backoff=(0, 0, 0), sleep=lambda s: None)


def req(**kw):
    base = dict(origin="fll", destination="del", depart_from=TODAY + timedelta(days=60),
                depart_to=TODAY + timedelta(days=89), min_days=20, max_days=25)
    base.update(kw)
    return FlexibleSearch(**base)


def leg(dep: datetime, *durs_and_layovers):
    """leg(dep, 120) = nonstop 2h; leg(dep, 120, 60, 90) = 2h flight, 1h layover, 1.5h flight."""
    segs, t, items = [], dep, list(durs_and_layovers)
    airports = ["AAA", "HUB", "HB2", "BBB"]
    for idx in range(0, len(items), 2):
        d = items[idx]
        segs.append(Segment(airports[idx // 2], airports[idx // 2 + 1], t, t + timedelta(minutes=d), d))
        t = t + timedelta(minutes=d + (items[idx + 1] if idx + 1 < len(items) else 0))
    return Leg(tuple(segs))


def test_date_pairs_count_and_lengths():
    r = req()
    pairs = r.date_pairs()
    assert len(pairs) == 30 * 6
    assert {(b - a).days for a, b in pairs} == set(range(20, 26))


def test_codes_are_uppercased_and_validated():
    r = req()
    assert (r.origin, r.destination) == ("FLL", "DEL")
    with pytest.raises(ValueError):
        req(origin="FL").validate()
    with pytest.raises(ValueError):
        req(destination="FLL").validate()


@pytest.mark.parametrize("kw", [
    dict(depart_from=TODAY - timedelta(days=1)),
    dict(depart_to=TODAY + timedelta(days=10)),       # ends before it starts
    dict(min_days=26),                                  # min > max
    dict(depart_to=TODAY + timedelta(days=320)),       # too far ahead with 25-day return
    dict(depart_from=TODAY + timedelta(days=1), depart_to=TODAY + timedelta(days=200)),  # too many checks
])
def test_bad_requests_rejected(kw):
    with pytest.raises(ValueError):
        req(**kw).validate()


def test_layovers_count_toward_duration():
    dep = datetime(2027, 1, 10, 8, 0)
    l = leg(dep, 120, 60, 90)
    assert l.stops == 1
    assert l.layovers_min == [60]
    assert l.total_duration_min == 270


def test_pick_top_never_repeats_a_departure_day():
    d = TODAY + timedelta(days=50)
    dep = datetime.combine(d, datetime.min.time())
    mk = lambda day_off, ret_off, price: TripResult(
        d + timedelta(days=day_off), d + timedelta(days=ret_off),
        Itinerary(price, "USD", ("X",), leg(dep, 100)))
    cands = [mk(0, 20, 300), mk(0, 21, 250), mk(0, 22, 400), mk(1, 21, 260), mk(2, 22, 999)]
    top = pick_top(cands, 10, "price")
    assert [t.depart_date for t in top] == [d, d + timedelta(days=1), d + timedelta(days=2)]
    assert top[0].itinerary.price == 250  # cheapest return kept for that day
    assert len({t.depart_date for t in top}) == len(top)


def test_full_scan_with_fake_provider():
    p = FakeProvider()
    r = req()
    rep = run_search(r, p, NO_WAIT, shuffle_seed=1)
    assert rep.complete
    assert len(rep.results) == 10
    assert len({t.depart_date for t in rep.results}) == 10
    prices = [t.itinerary.price for t in rep.results]
    assert prices == sorted(prices)
    assert all(t.itinerary.inbound is not None for t in rep.results)
    assert all(20 <= t.trip_days <= 25 for t in rep.results)
    assert p.calls == 180 + 10  # one per pair + return details for the top 10


def test_same_inputs_give_same_answer():
    a = run_search(req(), FakeProvider(), NO_WAIT, shuffle_seed=1)
    b = run_search(req(), FakeProvider(), NO_WAIT, shuffle_seed=99)  # order of requests shouldn't matter
    assert [t.to_dict() for t in a.results] == [t.to_dict() for t in b.results]


def test_sort_by_duration():
    rep = run_search(req(destination="MSY", min_days=5, max_days=7), FakeProvider(), NO_WAIT, shuffle_seed=1)
    rep_d = run_search(req(destination="MSY", min_days=5, max_days=7, rank_by="duration"),
                       FakeProvider(), NO_WAIT, shuffle_seed=1)
    durs = [t.itinerary.total_duration_min for t in rep_d.results]
    assert durs == sorted(durs)
    assert durs[0] <= min(t.itinerary.total_duration_min for t in rep.results)


def test_direct_only():
    rep = run_search(req(destination="MSY", min_days=5, max_days=6,
                         options=SearchOptions(stops="direct")), FakeProvider(), NO_WAIT)
    assert rep.results and all(t.itinerary.is_direct for t in rep.results)


def test_direct_only_with_no_nonstops_says_so():
    rep = run_search(req(options=SearchOptions(stops="direct")), FakeProvider(), NO_WAIT)
    assert rep.results == []
    assert any("No flights matched" in m for m in rep.messages)


def test_connecting_only():
    rep = run_search(req(destination="MSY", min_days=5, max_days=6,
                         options=SearchOptions(stops="connecting", max_stops=1)), FakeProvider(), NO_WAIT)
    assert rep.results
    assert all(t.itinerary.outbound.stops == 1 for t in rep.results)


def test_one_way_and_options_change_prices():
    one = run_search(req(options=SearchOptions(trip_type="one-way")), FakeProvider(), NO_WAIT)
    assert all(t.return_date is None for t in one.results)
    biz = run_search(req(options=SearchOptions(cabin="business", passengers=Passengers(adults=2))),
                     FakeProvider(), NO_WAIT)
    eco = run_search(req(), FakeProvider(), NO_WAIT)
    assert biz.results[0].itinerary.price > 3 * eco.results[0].itinerary.price


class Flaky(PriceProvider):
    """Blocks a few times, errors once, otherwise acts like the fake source."""
    name = "flaky"

    def __init__(self, blocks: int):
        self.inner, self.blocks, self.n = FakeProvider(), blocks, 0

    def search(self, *a):
        self.n += 1
        if self.n == 2:
            raise ProviderError("timeout")
        if 3 <= self.n < 3 + self.blocks:
            raise ProviderBlocked("HTTP 429")
        return self.inner.search(*a)

    def return_leg(self, it, o):
        return self.inner.return_leg(it, o)


def test_recovers_from_short_block_and_counts_failures():
    rep = run_search(req(), Flaky(blocks=2), NO_WAIT, shuffle_seed=1)
    assert rep.checks_done == rep.checks_total
    assert rep.checks_failed == 1
    assert not rep.blocked


def test_gives_up_after_long_block_but_keeps_partial_results():
    rep = run_search(req(), Flaky(blocks=1000), NO_WAIT, shuffle_seed=1)
    assert rep.blocked
    assert rep.checks_done < rep.checks_total
    assert any("Stopped early" in m for m in rep.messages)


def test_cancel():
    seen = {"n": 0}

    def cancel():
        seen["n"] += 1
        return seen["n"] > 5

    rep = run_search(req(), FakeProvider(), NO_WAIT, should_cancel=cancel)
    assert rep.cancelled and rep.checks_done == 5
