# Flight Price Tracker

Find the cheapest trips between two airports across flexible dates, watch fixed dates for price
changes, and get an email when a price drops below your trigger.

## Status

| Milestone | What | State |
|---|---|---|
| 1 | Search engine + command line | ✅ done |
| 2 | Webpage: search form, live fare calendar, sortable results | ✅ done |
| 3 | Saved watches, 15-minute price checks, email alerts, price history | next |
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

## Open it in your browser (no install)

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/cchigu/flight-price-tracker?quickstart=1)

Click the button and choose **Create codespace**. GitHub builds a small cloud computer for you,
installs everything and opens the webpage in a new tab (allow pop-ups if it doesn't appear, or
open the **Ports** tab and click the globe icon next to port 8000). The first start takes 2–3
minutes; later starts take seconds. The page link is private to your GitHub account.

A codespace stops by itself after 30 idle minutes, so this is for searching while you're at the
page. Searches that keep watching prices around the clock come with the Oracle server
(milestone 4). GitHub's free plan includes 120 core-hours a month (about 60 hours on the default
2-core machine).

## Run the webpage on your own computer

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m flighttracker.web          # then open http://localhost:8000
```

The page has every option above. While a search runs, a calendar of your departure window fills
in with each day's cheapest fare (darker = cheaper) and yellow tags mark the top picks. Results can
be re-sorted by price or travel time, and each trip expands to show every flight and layover with
a link to open it on Google Flights. The page remembers your last search and recent routes, and a
search keeps running on the server if you close the tab.

Only one search runs at a time, to keep the pace polite; others wait in line.

Settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `FT_PROVIDER` | `google-flights` | Price source, or `fake` for made-up prices |
| `FT_MIN_DELAY` / `FT_MAX_DELAY` | `2` / `5` | Seconds between requests |
| `FT_HOST` / `FT_PORT` | `127.0.0.1` / `8000` | Where the server listens |

Try the page without touching Google: `FT_PROVIDER=fake FT_MIN_DELAY=0.05 FT_MAX_DELAY=0.1 python -m flighttracker.web`

## Run from the command line

```bash

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
pip install -r requirements-dev.txt
python -m pytest
```

## Project layout

```
flighttracker/
  models.py              trips, legs, segments, search options
  engine.py              flexible-date search, throttling, top-N per departure day
  cli.py                 command line
  web.py                 web server: page, background searches, progress API
  static/                the webpage (HTML, CSS, JS, bundled open-license fonts)
  providers/
    base.py              price-source interface
    google_flights.py    free Google Flights scraper
    fake.py              offline made-up prices
tests/
```
