"""The flexible-date search: "I want a 20-25 day trip leaving sometime in this window."

How a scan works:
  1. Build every (departure, return) pair: each departure day in the window x each trip length.
  2. Price each pair with one request (round-trip totals), slowly, in shuffled order.
  3. For each departure day keep only its single best option (so no day repeats).
  4. Take the best N departure days, then look up return-leg details for just those N.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

from .models import Itinerary, RankBy, SearchOptions, TripResult
from .providers.base import PriceProvider, ProviderBlocked, ProviderError

MAX_DAYS_AHEAD = 330   # airlines sell about 11 months out
MAX_PAIRS = 800        # keeps one scan to roughly an hour at a polite pace

_IATA = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class FlexibleSearch:
    origin: str
    destination: str
    depart_from: date
    depart_to: date
    min_days: int = 7
    max_days: int = 7
    options: SearchOptions = field(default_factory=SearchOptions)
    top_n: int = 10
    rank_by: RankBy = "price"

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", self.origin.strip().upper())
        object.__setattr__(self, "destination", self.destination.strip().upper())

    def validate(self, today: Optional[date] = None) -> None:
        today = today or date.today()
        for code in (self.origin, self.destination):
            if not _IATA.match(code):
                raise ValueError(f"'{code}' is not a 3-letter airport code (e.g. FLL, MSY, DEL).")
        if self.origin == self.destination:
            raise ValueError("Origin and destination must be different.")
        if self.depart_from < today:
            raise ValueError("The departure window can't start in the past.")
        if self.depart_to < self.depart_from:
            raise ValueError("The departure window ends before it starts.")
        if self.round_trip:
            if self.min_days < 1 or self.max_days < self.min_days:
                raise ValueError("Trip length must be at least 1 day, and min can't exceed max.")
        last_day = self.depart_to + timedelta(days=self.max_days if self.round_trip else 0)
        if (last_day - today).days > MAX_DAYS_AHEAD:
            raise ValueError(f"Flights are only sold about {MAX_DAYS_AHEAD} days ahead; the latest "
                             f"date in this search is {last_day}.")
        if not 1 <= self.top_n <= 50:
            raise ValueError("Number of results must be between 1 and 50.")
        n = len(self.date_pairs())
        if n > MAX_PAIRS:
            raise ValueError(f"This search needs {n} price checks; the limit is {MAX_PAIRS}. "
                             "Narrow the departure window or the trip-length range.")

    @property
    def round_trip(self) -> bool:
        return self.options.trip_type == "round-trip"

    def date_pairs(self) -> list[tuple[date, Optional[date]]]:
        days = (self.depart_to - self.depart_from).days + 1
        departs = [self.depart_from + timedelta(days=i) for i in range(days)]
        if not self.round_trip:
            return [(d, None) for d in departs]
        return [(d, d + timedelta(days=n)) for d in departs for n in range(self.min_days, self.max_days + 1)]


@dataclass
class Throttle:
    """Spaces requests out and backs off when the source starts refusing us."""

    min_delay: float = 2.0
    max_delay: float = 5.0
    block_backoff: tuple[float, ...] = (60.0, 180.0, 420.0)
    sleep: Callable[[float], None] = time.sleep

    def pause(self) -> None:
        self.sleep(random.uniform(self.min_delay, self.max_delay))


@dataclass
class SearchReport:
    request: FlexibleSearch
    results: list[TripResult]
    checks_total: int
    checks_done: int = 0
    checks_failed: int = 0
    blocked: bool = False          # stopped early because the source kept refusing
    cancelled: bool = False
    elapsed_s: float = 0.0
    provider: str = ""
    messages: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.blocked and not self.cancelled and self.checks_done == self.checks_total

    def to_dict(self) -> dict:
        r = self.request
        return {
            "origin": r.origin, "destination": r.destination,
            "depart_from": r.depart_from.isoformat(), "depart_to": r.depart_to.isoformat(),
            "min_days": r.min_days, "max_days": r.max_days, "rank_by": r.rank_by,
            "provider": self.provider, "checks_total": self.checks_total, "checks_done": self.checks_done,
            "checks_failed": self.checks_failed, "blocked": self.blocked, "cancelled": self.cancelled,
            "complete": self.complete, "elapsed_s": round(self.elapsed_s, 1), "messages": self.messages,
            "results": [t.to_dict() for t in self.results],
        }


def _passes_stops(it: Itinerary, options: SearchOptions) -> bool:
    if options.stops == "direct":
        return it.is_direct
    if options.stops == "connecting" and it.outbound.stops == 0:
        return False
    if options.max_stops is not None and it.max_stops > options.max_stops:
        return False
    return True


def _key(rank_by: RankBy):
    if rank_by == "duration":
        return lambda t: (t.itinerary.total_duration_min, t.itinerary.price, t.depart_date)
    return lambda t: (t.itinerary.price, t.itinerary.total_duration_min, t.depart_date)


def pick_top(candidates: list[TripResult], top_n: int, rank_by: RankBy) -> list[TripResult]:
    """Best option per departure day, then the best `top_n` days. A departure day never repeats."""
    key = _key(rank_by)
    best_per_day: dict[date, TripResult] = {}
    for c in candidates:
        cur = best_per_day.get(c.depart_date)
        if cur is None or key(c) < key(cur):
            best_per_day[c.depart_date] = c
    return sorted(best_per_day.values(), key=key)[:top_n]


def sort_results(results: list[TripResult], by: RankBy) -> list[TripResult]:
    return sorted(results, key=_key(by))


ProgressFn = Callable[[int, int, Optional[TripResult]], None]


def run_search(
    request: FlexibleSearch,
    provider: PriceProvider,
    throttle: Optional[Throttle] = None,
    progress: Optional[ProgressFn] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    fetch_return_details: bool = True,
    shuffle_seed: Optional[int] = None,
) -> SearchReport:
    request.validate()
    throttle = throttle or Throttle()
    pairs = request.date_pairs()
    random.Random(shuffle_seed).shuffle(pairs)  # spread requests out; looks less like a bot sweep
    report = SearchReport(request=request, results=[], checks_total=len(pairs), provider=provider.name)
    started = time.monotonic()
    opts = request.options
    candidates: list[TripResult] = []
    consecutive_blocks = 0

    i = 0
    while i < len(pairs):
        if should_cancel and should_cancel():
            report.cancelled = True
            report.messages.append("Search cancelled.")
            break
        depart, ret = pairs[i]
        if i:
            throttle.pause()
        try:
            options = [it for it in provider.search(request.origin, request.destination, depart, ret, opts)
                       if _passes_stops(it, opts)]
        except ProviderBlocked as e:
            if consecutive_blocks >= len(throttle.block_backoff):
                report.blocked = True
                report.messages.append(f"Stopped early: the price source kept refusing requests ({e}). "
                                       "Showing what was found so far.")
                break
            wait = throttle.block_backoff[consecutive_blocks]
            consecutive_blocks += 1
            report.messages.append(f"Price source pushed back; waiting {int(wait)}s before retrying.")
            throttle.sleep(wait)
            continue  # retry the same pair
        except ProviderError as e:
            report.checks_failed += 1
            report.checks_done += 1
            report.messages.append(f"{depart}{' → ' + str(ret) if ret else ''}: {e}")
            i += 1
            continue

        consecutive_blocks = 0
        best_here = None
        if options:
            key = _key(request.rank_by)
            best_here = min((TripResult(depart, ret, it) for it in options), key=key)
            candidates.append(best_here)
        report.checks_done += 1
        if progress:
            progress(report.checks_done, report.checks_total, best_here)
        i += 1

    top = pick_top(candidates, request.top_n, request.rank_by)

    if fetch_return_details and request.round_trip and top:
        detailed = []
        for t in top:
            leg = None
            try:
                throttle.pause()
                leg = provider.return_leg(t.itinerary, opts)
            except ProviderError as e:
                report.messages.append(f"Couldn't load return details for {t.depart_date}: {e}")
            if leg is not None:
                it = t.itinerary
                t = TripResult(t.depart_date, t.return_date, Itinerary(
                    it.price, it.currency, it.airlines, it.outbound, leg, it.search_url, it.provider_ref))
            if _passes_stops(t.itinerary, opts):
                detailed.append(t)
        top = sort_results(detailed, request.rank_by)

    report.results = top
    report.elapsed_s = time.monotonic() - started
    if not candidates and report.checks_done:
        report.messages.append("No flights matched these filters. Try allowing connections or widening the dates.")
    return report


def estimate_minutes(request: FlexibleSearch, throttle: Optional[Throttle] = None) -> float:
    """Rough time a scan will take, for showing on the page before starting."""
    throttle = throttle or Throttle()
    per = (throttle.min_delay + throttle.max_delay) / 2 + 1.5  # +~1.5s for the request itself
    extra = request.top_n if request.round_trip else 0
    return (len(request.date_pairs()) + extra) * per / 60
