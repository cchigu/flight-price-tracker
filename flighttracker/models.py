"""Core data types shared by the search engine, providers and (later) the web app."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Literal, Optional

Cabin = Literal["economy", "premium-economy", "business", "first"]
StopsFilter = Literal["direct", "connecting", "any"]
RankBy = Literal["price", "duration"]
TripType = Literal["round-trip", "one-way"]


@dataclass(frozen=True)
class Passengers:
    adults: int = 1
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0

    def __post_init__(self) -> None:
        total = self.adults + self.children + self.infants_in_seat + self.infants_on_lap
        if self.adults < 1:
            raise ValueError("At least one adult is required.")
        if total > 9:
            raise ValueError("Google Flights allows at most 9 passengers.")
        if self.infants_on_lap > self.adults:
            raise ValueError("Each lap infant needs an adult.")


@dataclass(frozen=True)
class SearchOptions:
    """Everything about a search except the route and dates. All of it is user-changeable."""

    trip_type: TripType = "round-trip"
    cabin: Cabin = "economy"
    passengers: Passengers = field(default_factory=Passengers)
    currency: str = "USD"
    language: str = "en-US"
    stops: StopsFilter = "any"
    # Only used when stops is "connecting" or "any". None = no limit.
    max_stops: Optional[int] = None

    def provider_max_stops(self) -> Optional[int]:
        """The stop limit to send to the price source (it filters every leg)."""
        if self.stops == "direct":
            return 0
        return self.max_stops


@dataclass(frozen=True)
class Segment:
    """One plane ride (e.g. FLL -> ATL)."""

    from_airport: str
    to_airport: str
    depart: datetime  # local time at from_airport
    arrive: datetime  # local time at to_airport
    duration_min: int
    airline: str = ""
    flight_number: str = ""
    aircraft: str = ""


@dataclass(frozen=True)
class Leg:
    """One direction of travel, made of one or more segments."""

    segments: tuple[Segment, ...]

    @property
    def stops(self) -> int:
        return max(len(self.segments) - 1, 0)

    @property
    def layovers_min(self) -> list[int]:
        # A layover starts and ends at the same airport, so local times subtract correctly.
        return [
            int((nxt.depart - cur.arrive).total_seconds() // 60)
            for cur, nxt in zip(self.segments, self.segments[1:])
        ]

    @property
    def total_duration_min(self) -> int:
        """Door-to-door time for this leg: time in the air plus layovers."""
        return sum(s.duration_min for s in self.segments) + sum(self.layovers_min)

    @property
    def via(self) -> list[str]:
        return [s.to_airport for s in self.segments[:-1]]


@dataclass(frozen=True)
class Itinerary:
    """A priced option. For round trips, price is the round-trip total."""

    price: int
    currency: str
    airlines: tuple[str, ...]
    outbound: Leg
    inbound: Optional[Leg] = None
    search_url: str = ""
    # Opaque data the provider needs to look up the return leg later.
    provider_ref: object = field(default=None, compare=False, repr=False)

    @property
    def total_duration_min(self) -> int:
        """Outbound plus return travel time (return counted only once it is known)."""
        total = self.outbound.total_duration_min
        if self.inbound is not None:
            total += self.inbound.total_duration_min
        return total

    @property
    def max_stops(self) -> int:
        legs = [self.outbound] + ([self.inbound] if self.inbound else [])
        return max(leg.stops for leg in legs)

    @property
    def is_direct(self) -> bool:
        return self.max_stops == 0


@dataclass(frozen=True)
class TripResult:
    """One row of the top-10 table: a departure day, its return day and the best option found."""

    depart_date: date
    return_date: Optional[date]
    itinerary: Itinerary

    @property
    def trip_days(self) -> Optional[int]:
        if self.return_date is None:
            return None
        return (self.return_date - self.depart_date).days

    def to_dict(self) -> dict:
        it = self.itinerary

        def leg_dict(leg: Optional[Leg]) -> Optional[dict]:
            if leg is None:
                return None
            return {
                "stops": leg.stops,
                "via": leg.via,
                "duration_min": leg.total_duration_min,
                "layovers_min": leg.layovers_min,
                "segments": [
                    {**asdict(s), "depart": s.depart.isoformat(), "arrive": s.arrive.isoformat()}
                    for s in leg.segments
                ],
            }

        return {
            "depart_date": self.depart_date.isoformat(),
            "return_date": self.return_date.isoformat() if self.return_date else None,
            "trip_days": self.trip_days,
            "price": it.price,
            "currency": it.currency,
            "airlines": list(it.airlines),
            "total_duration_min": it.total_duration_min,
            "max_stops": it.max_stops,
            "outbound": leg_dict(it.outbound),
            "inbound": leg_dict(it.inbound),
            "search_url": it.search_url,
        }
