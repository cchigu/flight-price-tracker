# Flight Price Tracker

Find the cheapest trips between two airports across flexible dates, watch fixed dates for price
changes, and get an email when a price drops below your trigger.

## Status

| Milestone | What | State |
|---|---|---|
| 1 | Search engine + command line | ✅ done |
| 2 | Webpage (search form, results table, sorting) | next |
| 3 | Saved watches, 15-minute price checks, email alerts, price history | |
| 4 | Deploy to Oracle Cloud Always Free | |

## How a flexible search works

You give a route, a departure window and a trip length range (e.g. **20–25 days**).

1. Every departure day in the window is paired with every trip length, so a 42-day window
   with 20–25 day trips is 42 × 6 = 252 price checks.
2. Each pair is priced with one request (round-trip total), spaced 2–5 seconds apart in shuffled
   order so it doesn't hammer Google.
3. For each departure day only its single best option is kept, **so a departure day never repeats**.
4. The best 10 departure days are returned, then their return-leg details are looked up
   (10 extra requests).

"Trip length" is the number of days between departure and return: leaving Jan 5 with a 20-day trip
means returning Jan 25. "Travel time" is door to door for both legs, layovers included.

### Options (all changeable)

| Option | Values | Default |
|---|---|---|
| Trip type | round-trip, one-way | round-trip |
| Stops | `direct`, `connecting`, `any` (+ max stops per leg) | any |
| Rank / sort by | `price`, `duration` | price |
| Cabin | economy, premium-economy, business, first | economy |
| Passengers | adults, children, infants (seat / lap), up to 9 | 1 adult |
| Currency | any ISO code (USD, INR, EUR…) | USD |
| Results | 1–50 | 10 |

Limits: flights sell about 330 days ahead, and one scan is capped at 800 price checks (about an hour).

## Price source

Prices come from Google Flights through the open-source
[faster-flights](https://pypi.org/project/faster-flights/) scraper. It's free but unofficial:
Google can change its page or refuse requests. When refused, the engine backs off (1, 3, then
7 minutes) and, if it keeps happening, stops and returns what it found so far.

All price sources implement `flighttracker/providers/base.py`, so a paid API can be added later in
one file without touching the rest. `fake` is an offline source with repeatable made-up prices for
testing.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Fort Lauderdale → New Delhi, leaving between Jan 5 and Feb 15, staying 20–25 days
python -m flighttracker.cli FLL DEL --depart-from 2027-01-05 --depart-to 2027-02-15 --days 20-25

# Fort Lauderdale → New Orleans, nonstop only, shortest first, 2 adults
python -m flighttracker.cli FLL MSY --depart-from 2026-12-01 --depart-to 2026-12-31 --days 4-6 \
    --stops direct --sort duration --adults 2

# Try it offline with made-up prices
python -m flighttracker.cli FLL DEL --depart-from 2027-01-05 --depart-to 2027-02-15 --days 20-25 --provider fake
```

Run `python -m flighttracker.cli --help` for every option, including `--json` output.

## Tests

```bash
pip install pytest
python -m pytest
```

## Project layout

```
flighttracker/
  models.py              trips, legs, segments, search options
  engine.py              flexible-date search, throttling, top-N per departure day
  cli.py                 command line
  providers/
    base.py              price-source interface
    google_flights.py    free Google Flights scraper
    fake.py              offline made-up prices
tests/
```
