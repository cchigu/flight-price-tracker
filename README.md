# Flight Price Tracker

Find the cheapest trips between two airports across flexible dates, watch fixed dates for price
changes, and get an email when a price drops below your trigger.

## Status

| Milestone | What | State |
|---|---|---|
| 1 | Search engine + command line | ✅ done |
| 2 | Webpage: search form, live fare calendar, sortable results | ✅ done |
| 3 | Saved watches, 15-minute price checks, email alerts, price history | next |
| 4 | Permanent link: Windows PC + Tailscale Funnel, or Oracle Cloud (password, auto-update) | ✅ done |

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

## Logins

Turned on by setting `FT_PASSWORD` (both installers do). On first start that becomes the owner's
password, with the sign-in name **admin** (change it with `FT_OWNER` before the first start).

- The owner opens **People** to add a login for someone: pick their name, keep or change the
  suggested starting password, and send them the link, name and password shown. **Reset password**
  and **Remove** sign that person out everywhere.
- Everyone can change their password and set an email for alerts under **Account**.
- Searches are private to each person, and each person runs one search at a time. Searches from
  everyone still take turns, one at a time, to keep the pace polite.

Logins are stored in `data/tracker.db` (passwords as salted scrypt hashes). After the first start,
the owner's password is changed under Account; editing `FT_PASSWORD` no longer changes it.

## Permanent link from your Windows PC (free)

Runs the tracker on your own computer and gives it a fixed private address like
`https://your-pc.your-tailnet.ts.net` through [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
(free for personal use). It works from your phone or any computer, but only while the PC is on and awake.

Open **PowerShell** and paste:

```powershell
irm https://raw.githubusercontent.com/cchigu/flight-price-tracker/main/deploy/windows/install.ps1 | iex
```

The installer:
1. installs Python, Git and Tailscale if they're missing (Windows asks for permission)
2. downloads the code to `%USERPROFILE%\flight-price-tracker` and asks you for a password for the page
3. starts the tracker hidden in the background, and again every time you sign in to Windows
4. signs you in to Tailscale (free account) and turns on Funnel. The first time, Tailscale shows
   a link to approve Funnel; open it, approve, and the installer finishes
5. offers to stop the PC from sleeping while it's plugged in
6. prints your link and copies it to the clipboard

It checks GitHub every 10 minutes and installs new code by itself, and restarts the tracker if it
ever stops. Logs are in `%USERPROFILE%\flight-price-tracker\logs`. Run the same command again to
repair it or change the password. To remove it:

```powershell
irm https://raw.githubusercontent.com/cchigu/flight-price-tracker/main/deploy/windows/uninstall.ps1 | iex
```

## Permanent link on Oracle Cloud (free)

`deploy/oracle/setup.sh` turns a fresh Ubuntu 24.04 server into a private, always-on copy of the
tracker at `https://<server-ip-with-dashes>.sslip.io`, behind a password, with a free HTTPS
certificate. Every 10 minutes it checks GitHub `main` and installs any new code by itself, so
pushing to this repo is all it takes to update the live site.

1. Sign up at oracle.com/cloud/free (card is for identity only; Always Free resources aren't charged).
2. **Compute → Instances → Create instance**
   - Image: **Canonical Ubuntu 24.04**
   - Shape: **VM.Standard.A1.Flex** (1 OCPU, 6 GB) or **VM.Standard.E2.1.Micro**; both are Always Free
   - Networking: keep the defaults with **Assign a public IPv4 address** on
   - SSH keys: let it generate a key pair and download the private key (only needed for fixing things)
   - **Advanced options → Management → Paste cloud-init script**: paste
     [`deploy/oracle/cloud-init.sh`](deploy/oracle/cloud-init.sh) after changing the password in it
3. Open the web ports: on the instance page click the **Subnet** link → **Security** (or
   **Security Lists**) → **Default Security List** → **Add Ingress Rules**, source `0.0.0.0/0`,
   TCP, destination port `80,443`.
4. About 5 minutes after the instance shows **Running**, open
   `https://<public-ip-with-dashes>.sslip.io` (e.g. `129.146.10.20` becomes
   `https://129-146-10-20.sslip.io`) and sign in.

On the server: `journalctl -u flight-tracker -f` shows the app log, `sudo flight-tracker-update`
updates right away, and the setup log is `/var/log/flight-tracker-setup.log`. To change the
password, edit `FT_PASSWORD` in `/etc/flight-tracker.env` and run `sudo systemctl restart flight-tracker`.

## Open it in GitHub Codespaces (no install)

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
  auth.py                password sign-in
  supervisor.py          keeps the server running on a home PC and pulls updates from GitHub
  static/                the webpage (HTML, CSS, JS, bundled open-license fonts)
  providers/
    base.py              price-source interface
    google_flights.py    free Google Flights scraper
    fake.py              offline made-up prices
tests/
```
