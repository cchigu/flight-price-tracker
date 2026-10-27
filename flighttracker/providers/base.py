"""The swappable price-source interface.

Anything that can price a flight (the free Google Flights scraper today, a paid API later)
implements PriceProvider. The search engine only talks to this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from ..models import Itinerary, Leg, SearchOptions


class ProviderError(Exception):
    """A search failed, but trying again later may work."""


class ProviderBlocked(ProviderError):
    """The source is refusing our requests (rate-limited or blocked). Back off."""


class PriceProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search(
        self,
        origin: str,
        destination: str,
        depart: date,
        return_date: Optional[date],
        options: SearchOptions,
    ) -> list[Itinerary]:
        """Return priced options for one set of dates.

        For round trips the price is the round-trip total. The return leg may be left
        empty (inbound=None) if it costs an extra request; use return_leg() for that.
        """

    def return_leg(self, itinerary: Itinerary, options: SearchOptions) -> Optional[Leg]:
        """Look up the cheapest matching return leg for a round-trip itinerary, if supported."""
        return None

    def search_url(
        self,
        origin: str,
        destination: str,
        depart: date,
        return_date: Optional[date],
        options: SearchOptions,
    ) -> str:
        """A link the user can open to see or book these dates."""
        return ""
