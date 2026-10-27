"""Offline price source with believable, repeatable fake fares.

Used by the tests and by `--provider fake` so the app can be tried without touching Google.
The same route and dates always give the same prices.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta
from typing import Optional

from ..models import Itinerary, Leg, SearchOptions, Segment
from .base import PriceProvider

# Rough base round-trip fares and nonstop times for a few routes; anything else gets a default.
_DOMESTIC = ["Delta", "American", "United", "Spirit", "JetBlue", "Southwest"]
_LONG_HAUL = ["Qatar Airways", "Turkish Airlines", "British Airways", "Emirates", "Air India", "United"]
_ROUTES = {
    frozenset({"FLL", "MSY"}): {"base": 190, "nonstop_min": 125, "hubs": ["ATL", "IAH", "BNA"],
                                "nonstop": True, "airlines": _DOMESTIC},
    frozenset({"FLL", "DEL"}): {"base": 980, "nonstop_min": 900, "hubs": ["JFK", "DOH", "IST", "LHR", "DXB"],
                                "nonstop": False, "airlines": _LONG_HAUL},
}


def _rng(*parts) -> random.Random:
    seed = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


class FakeProvider(PriceProvider):
    name = "fake"

    def __init__(self):
        self.calls = 0

    def _route(self, a: str, b: str) -> dict:
        return _ROUTES.get(frozenset({a, b}), {"base": 450, "nonstop_min": 240, "hubs": ["ATL", "ORD", "DFW"],
                                               "nonstop": True, "airlines": _DOMESTIC})

    def _leg(self, rng, origin, dest, day: date, stops: int, route) -> Leg:
        start = datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(6, 21), minutes=rng.choice([0, 15, 30, 45]))
        if stops == 0:
            dur = route["nonstop_min"] + rng.randint(-10, 15)
            return Leg((Segment(origin, dest, start, start + timedelta(minutes=dur), dur, "XX", str(rng.randint(100, 2999))),))
        hubs = rng.sample(route["hubs"], k=min(stops, len(route["hubs"])))
        stops_path = [origin, *hubs, dest]
        segs, t = [], start
        per = int(route["nonstop_min"] * 1.1 / len(stops_path[1:])) + 30
        for a, b in zip(stops_path, stops_path[1:]):
            dur = per + rng.randint(-20, 40)
            segs.append(Segment(a, b, t, t + timedelta(minutes=dur), dur, "XX", str(rng.randint(100, 2999))))
            t = t + timedelta(minutes=dur + rng.randint(55, 300))  # layover
        return Leg(tuple(segs))

    def _price(self, rng, route, depart: date, return_date: Optional[date], stops: int, options: SearchOptions) -> int:
        base = route["base"]
        lead = (depart - date.today()).days
        price = base
        price *= 1.0 + max(0, 30 - lead) * 0.02          # last-minute premium
        price *= 1.15 if depart.weekday() in (4, 5, 6) else 0.95  # weekend departures cost more
        if depart.month == 12 and depart.day >= 15:
            price *= 1.35                                 # holidays
        price *= {0: 1.25, 1: 1.0, 2: 0.9}.get(stops, 0.9)
        price *= {"economy": 1, "premium-economy": 1.8, "business": 3.8, "first": 6}[options.cabin]
        if return_date is None:
            price *= 0.6
        # Real fares swing day to day for each date pair, not just between options.
        price *= _rng("day", route["base"], depart, return_date).uniform(0.82, 1.35)
        price *= rng.uniform(0.95, 1.15)
        pax = options.passengers.adults + options.passengers.children + options.passengers.infants_in_seat
        pax += 0.1 * options.passengers.infants_on_lap
        return int(round(price * pax))

    def search(self, origin, destination, depart, return_date, options) -> list[Itinerary]:
        self.calls += 1
        route = self._route(origin, destination)
        rng = _rng(origin, destination, depart, return_date, options.cabin)
        allowed = options.provider_max_stops()
        stop_choices = [s for s in (0, 1, 2) if (s > 0 or route["nonstop"]) and (allowed is None or s <= allowed)]
        out = []
        for i in range(rng.randint(4, 9)):
            stops = rng.choice(stop_choices) if stop_choices else None
            if stops is None:
                break
            orng = _rng(origin, destination, depart, return_date, i)
            out.append(Itinerary(
                price=self._price(orng, route, depart, return_date, stops, options),
                currency=options.currency,
                airlines=(orng.choice(route["airlines"]),),
                outbound=self._leg(orng, origin, destination, depart, stops, route),
                search_url=self.search_url(origin, destination, depart, return_date, options),
                provider_ref=(origin, destination, return_date, stops, i),
            ))
        return out

    def return_leg(self, itinerary, options) -> Optional[Leg]:
        self.calls += 1
        origin, destination, return_date, stops, i = itinerary.provider_ref
        if return_date is None:
            return None
        route = self._route(origin, destination)
        return self._leg(_rng("ret", origin, destination, return_date, i), destination, origin, return_date, stops, route)

    def search_url(self, origin, destination, depart, return_date, options) -> str:
        q = f"Flights from {origin} to {destination} on {depart.isoformat()}"
        if return_date:
            q += f" returning {return_date.isoformat()}"
        return "https://www.google.com/travel/flights?q=" + q.replace(" ", "+")
