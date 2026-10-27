"""Free price source: Google Flights, read through the open-source faster-flights scraper.

This is unofficial. Google can change its page or start refusing requests at any time, which
is why it sits behind the PriceProvider interface and raises ProviderBlocked when refused.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..models import Itinerary, Leg, SearchOptions, Segment
from .base import PriceProvider, ProviderBlocked, ProviderError

try:  # Imported lazily so tests and the fake provider work without the scraper installed.
    import fast_flights as ff
except ImportError:  # pragma: no cover
    ff = None


_BLOCK_HINTS = ("blocked", "rate-limited", "HTTP 429", "HTTP 403", "unusual traffic", "captcha")


def _to_dt(sd) -> datetime:
    y, m, d = sd.date
    hh, mm = (list(sd.time) + [0, 0])[:2]
    return datetime(int(y), int(m), int(d), int(hh or 0), int(mm or 0))


def _to_leg(flight) -> Leg:
    return Leg(
        segments=tuple(
            Segment(
                from_airport=s.from_airport.code,
                to_airport=s.to_airport.code,
                depart=_to_dt(s.departure),
                arrive=_to_dt(s.arrival),
                duration_min=int(s.duration or 0),
                airline=getattr(s, "airline_code", "") or "",
                flight_number=getattr(s, "flight_number", "") or "",
                aircraft=getattr(s, "plane_type", "") or "",
            )
            for s in flight.flights
        )
    )


class GoogleFlightsProvider(PriceProvider):
    name = "google-flights"

    def __init__(self, proxy: Optional[str] = None):
        if ff is None:
            raise RuntimeError("faster-flights is not installed. Run: pip install faster-flights")
        self.proxy = proxy

    # ---- query building -------------------------------------------------------------
    def _query(self, origin, destination, depart, return_date, options: SearchOptions):
        max_stops = options.provider_max_stops()
        legs = [ff.FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=destination,
                               max_stops=max_stops)]
        trip = "one-way"
        if return_date is not None and options.trip_type == "round-trip":
            trip = "round-trip"
            legs.append(ff.FlightQuery(date=return_date.isoformat(), from_airport=destination,
                                       to_airport=origin, max_stops=max_stops))
        p = options.passengers
        return ff.create_query(
            flights=legs,
            trip=trip,
            seat=options.cabin,
            passengers=ff.Passengers(adults=p.adults, children=p.children,
                                     infants_in_seat=p.infants_in_seat, infants_on_lap=p.infants_on_lap),
            language=options.language,
            currency=options.currency,
            max_stops=max_stops,
        )

    def search_url(self, origin, destination, depart, return_date, options) -> str:
        return self._query(origin, destination, depart, return_date, options).url()

    # ---- searching ------------------------------------------------------------------
    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # the scraper raises plain RuntimeError/ValueError
            msg = str(e)
            if any(h.lower() in msg.lower() for h in _BLOCK_HINTS):
                raise ProviderBlocked(msg) from e
            raise ProviderError(msg) from e

    def search(self, origin, destination, depart, return_date, options) -> list[Itinerary]:
        q = self._query(origin, destination, depart, return_date, options)
        results = self._call(ff.get_flights, q, proxy=self.proxy)
        diag = getattr(results, "diagnostics", None)
        if diag is not None and getattr(diag, "status", "") == "blocked":
            raise ProviderBlocked("Google Flights refused the request.")
        url = q.url()
        out = []
        for f in results:
            if not f.price or not f.flights:
                continue  # sold out / price not shown
            out.append(Itinerary(
                price=int(f.price),
                currency=options.currency,
                airlines=tuple(f.airlines),
                outbound=_to_leg(f),
                inbound=None,
                search_url=url,
                provider_ref=(q, f),
            ))
        return out

    def return_leg(self, itinerary: Itinerary, options: SearchOptions) -> Optional[Leg]:
        if not isinstance(itinerary.provider_ref, tuple):
            return None
        q, flight = itinerary.provider_ref
        if not getattr(flight, "select_token", None):
            return None
        rq = ff.select_flight(q, flight)
        returns = [r for r in self._call(ff.get_return_flights, rq, proxy=self.proxy) if r.flights]
        if options.stops == "direct":
            returns = [r for r in returns if len(r.flights) == 1]
        if not returns:
            return None
        # Return options are priced as the round-trip total; the cheapest matches the scan price.
        best = min(returns, key=lambda r: (r.price or 10**9, sum(s.duration or 0 for s in r.flights)))
        return _to_leg(best)
