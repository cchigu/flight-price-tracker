"""Run a flexible-date search from the terminal.

Example:
  python -m flighttracker.cli FLL DEL --depart-from 2027-01-05 --depart-to 2027-02-15 --days 20-25
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from .engine import FlexibleSearch, Throttle, estimate_minutes, run_search
from .models import Passengers, SearchOptions
from .providers import get_provider


def _range(text: str) -> tuple[int, int]:
    parts = [p.strip() for p in text.replace("–", "-").split("-") if p.strip()]
    if len(parts) == 1:
        n = int(parts[0])
        return n, n
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    raise argparse.ArgumentTypeError("Use a number or a range like 20-25.")


def _fmt_min(m: int) -> str:
    return f"{m // 60}h {m % 60:02d}m"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Find the cheapest trips across flexible dates.")
    p.add_argument("origin", help="From airport code, e.g. FLL")
    p.add_argument("destination", help="To airport code, e.g. MSY or DEL")
    p.add_argument("--depart-from", type=date.fromisoformat, required=True, help="Earliest departure YYYY-MM-DD")
    p.add_argument("--depart-to", type=date.fromisoformat, required=True, help="Latest departure YYYY-MM-DD")
    p.add_argument("--days", type=_range, default=(7, 7), help="Trip length in days, e.g. 20-25 (default 7)")
    p.add_argument("--one-way", action="store_true", help="One-way instead of round trip")
    p.add_argument("--stops", choices=["direct", "connecting", "any"], default="any")
    p.add_argument("--max-stops", type=int, default=None, help="With connecting/any: most stops per leg")
    p.add_argument("--cabin", choices=["economy", "premium-economy", "business", "first"], default="economy")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0)
    p.add_argument("--infants-seat", type=int, default=0)
    p.add_argument("--infants-lap", type=int, default=0)
    p.add_argument("--currency", default="USD")
    p.add_argument("--sort", choices=["price", "duration"], default="price")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--provider", default="google-flights", help="google-flights (live) or fake (offline demo)")
    p.add_argument("--min-delay", type=float, default=2.0, help="Seconds between requests (min)")
    p.add_argument("--max-delay", type=float, default=5.0, help="Seconds between requests (max)")
    p.add_argument("--no-return-details", action="store_true", help="Skip the extra return-leg lookups")
    p.add_argument("--json", action="store_true", help="Print results as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    options = SearchOptions(
        trip_type="one-way" if a.one_way else "round-trip",
        cabin=a.cabin,
        passengers=Passengers(a.adults, a.children, a.infants_seat, a.infants_lap),
        currency=a.currency.upper(),
        stops=a.stops,
        max_stops=a.max_stops,
    )
    req = FlexibleSearch(a.origin, a.destination, a.depart_from, a.depart_to,
                         a.days[0], a.days[1], options, a.top, a.sort)
    try:
        req.validate()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    throttle = Throttle(a.min_delay, a.max_delay)
    if a.provider == "fake":
        throttle = Throttle(0, 0, sleep=lambda s: None)
    provider = get_provider(a.provider)
    n = len(req.date_pairs())
    if not a.json:
        print(f"{req.origin} → {req.destination}: {n} price checks, about "
              f"{estimate_minutes(req, throttle):.0f} min using {provider.name}.", file=sys.stderr)

    live = sys.stderr.isatty() and not a.json
    best_so_far = {"price": None}

    def progress(done, total, best):
        if best is not None and (best_so_far["price"] is None or best.itinerary.price < best_so_far["price"]):
            best_so_far["price"] = best.itinerary.price
        if live:
            cheapest = f", cheapest so far {req.options.currency} {best_so_far['price']}" if best_so_far["price"] else ""
            print(f"\r  checked {done}/{total}{cheapest}   ", end="", file=sys.stderr, flush=True)

    report = run_search(req, provider, throttle, progress, fetch_return_details=not a.no_return_details)
    if live:
        print(file=sys.stderr)

    if a.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    for m in report.messages[-5:]:
        print(f"  note: {m}", file=sys.stderr)
    if not report.results:
        print("No trips found.")
        return 1
    print(f"\nTop {len(report.results)} by {a.sort} (each departure day once):\n")
    print(f"{'#':>2}  {'Depart':<10}  {'Return':<10}  {'Days':>4}  {'Price':>8}  {'Stops':>5}  {'Travel time':>12}  Airlines")
    for i, t in enumerate(report.results, 1):
        it = t.itinerary
        ret = t.return_date.isoformat() if t.return_date else "-"
        days = str(t.trip_days) if t.trip_days is not None else "-"
        time_txt = _fmt_min(it.total_duration_min) + ("" if it.inbound or not req.round_trip else "*")
        print(f"{i:>2}  {t.depart_date.isoformat():<10}  {ret:<10}  {days:>4}  "
              f"{it.currency} {it.price:>4}  {it.max_stops:>5}  {time_txt:>12}  {', '.join(it.airlines)}")
    if any(r.itinerary.inbound is None for r in report.results) and req.round_trip:
        print("\n* outbound only; return details weren't available.")
    print(f"\nDone in {report.elapsed_s / 60:.1f} min. "
          f"{report.checks_done}/{report.checks_total} checks, {report.checks_failed} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
