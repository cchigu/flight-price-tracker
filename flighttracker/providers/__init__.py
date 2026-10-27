from .base import PriceProvider, ProviderBlocked, ProviderError


def get_provider(name: str = "google-flights", **kwargs) -> PriceProvider:
    """Pick a price source by name. Add a paid API here later without touching the engine."""
    if name in ("google", "google-flights"):
        from .google_flights import GoogleFlightsProvider
        return GoogleFlightsProvider(**kwargs)
    if name == "fake":
        from .fake import FakeProvider
        return FakeProvider()
    raise ValueError(f"Unknown price source: {name}")


__all__ = ["PriceProvider", "ProviderBlocked", "ProviderError", "get_provider"]
