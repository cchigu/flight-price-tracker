"""Background price checks for saved watches, plus alert and daily-summary emails.

Runs in a thread inside the web server. Every 30 seconds it looks for watches that are due:
  fixed dates   every 15 minutes (one price request)
  flexible      every 6 hours (a full scan of the window)
Checks share the same one-at-a-time lane as people's searches, so Google only ever sees one
thing happening at a time. If Google refuses requests, all checks pause for 30 minutes.
"""

from __future__ import annotations

import html
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .engine import _passes_stops, run_search
from .models import TripResult
from .providers.base import ProviderBlocked, ProviderError
from .watches import (FIXED_EVERY_S, FLEX_EVERY_S, REALERT_DROP, Watch, WatchStore, decide_alert, ended,
                      request_from_params)

TICK_S = 30
BLOCK_PAUSE_S = 30 * 60
SUMMARY_HOUR = 8   # local time on the server
ERROR_RETRY_S = 10 * 60


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[checker]", msg, flush=True)


def nice_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a}, {d:%b} {d.day}"


def money(n: Optional[int], cur: str) -> str:
    if n is None:
        return "no price"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "CAD": "CA$"}.get(cur, cur + " ")
    return f"{sym}{n:,}"


class Checker:
    def __init__(self, store: WatchStore, users, runner, mailer,
                 clock: Callable[[], float] = time.time, today: Callable[[], date] = date.today):
        self.store, self.users, self.runner, self.mailer = store, users, runner, mailer
        self.clock, self.today = clock, today
        self.paused_until = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- thread ---------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="watch-checker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log("Started.")
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # keep running whatever happens
                log("Unexpected error:\n" + traceback.format_exc())

    def tick(self) -> None:
        now = self.clock()
        if now >= self.paused_until:
            for w in self.store.due(now):
                if self._stop.is_set():
                    return
                self.check(w)
                if self.clock() < self.paused_until:
                    break
        self.send_summaries()

    # ---- one watch ------------------------------------------------------------------------------
    def check(self, w: Watch) -> None:
        now = self.clock()
        if ended(w.params, self.today()):
            self.store.update(w.id, ended=1)
            log(f"Watch {w.id} ended (its dates have passed).")
            return
        every = FIXED_EVERY_S if w.kind == "fixed" else FLEX_EVERY_S
        req = request_from_params(w.params, self.today())
        try:
            with self.runner.lane:
                if w.kind == "fixed":
                    price, result = self._check_fixed(req)
                else:
                    price, result = self._check_flexible(req)
        except ProviderBlocked as e:
            self.paused_until = self.clock() + BLOCK_PAUSE_S
            self.store.update(w.id, next_check=self.paused_until, last_error="Google Flights is refusing requests right now; trying again in 30 minutes.")
            log(f"Blocked while checking watch {w.id}: {e}. Pausing checks for 30 minutes.")
            return
        except ProviderError as e:
            self.store.update(w.id, next_check=now + ERROR_RETRY_S, last_error=f"Check failed: {e}")
            log(f"Watch {w.id} check failed: {e}")
            return

        checked = self.clock()
        self.store.add_history(w.id, price, checked)
        send, below, last_alert = decide_alert(price, w.trigger, w.below, w.last_alert_price)
        self.store.update(w.id, last_checked=checked, next_check=checked + every, last_price=price,
                          last_result=result, below=int(below), last_alert_price=last_alert,
                          last_error="" if price is not None else "No flights matched on the last check.")
        log(f"Watch {w.id} ({w.label}): {money(price, w.currency)}" + (" -> alert" if send else ""))
        if send and not self._send_alert(self.store.get(w.id), price, result):
            # Not delivered (no email set, or sending failed): don't count it, so the next check tries again.
            self.store.update(w.id, below=int(w.below), last_alert_price=w.last_alert_price)

    def _check_fixed(self, req) -> tuple[Optional[int], Optional[dict]]:
        opts = req.options
        depart = req.depart_from
        ret = depart + timedelta(days=req.min_days) if req.round_trip else None
        provider = self.runner.provider
        options = [it for it in provider.search(req.origin, req.destination, depart, ret, opts) if _passes_stops(it, opts)]
        if not options:
            return None, None
        best = min(options, key=lambda it: (it.price, it.total_duration_min))
        return best.price, {"trips": [TripResult(depart, ret, best).to_dict()]}

    def _check_flexible(self, req) -> tuple[Optional[int], Optional[dict]]:
        rep = run_search(req, self.runner.provider, self.runner.throttle, fetch_return_details=False)
        if rep.blocked and not rep.results:
            raise ProviderBlocked("refused during scan")
        if rep.blocked:
            self.paused_until = self.clock() + BLOCK_PAUSE_S
        trips = sorted(rep.results, key=lambda t: t.itinerary.price)[:5]
        if not trips:
            return None, None
        return trips[0].itinerary.price, {"trips": [t.to_dict() for t in trips], "partial": not rep.complete}

    # ---- email ----------------------------------------------------------------------------------
    def _link(self, path: str = "/watches") -> str:
        base = self.store.get_setting("public_url", "")
        return base + path if base else ""

    def _send_alert(self, w: Watch, price: int, result: Optional[dict]) -> bool:
        """True if the email went out."""
        user = self.users.get(w.user_id)
        if user is None or not user.email:
            self.store.update(w.id, last_error="The price hit your alert, but there's no email on your account to send it to.")
            log(f"Watch {w.id} hit its alert price but {getattr(user, 'name', '?')} has no email set.")
            return False
        cur = w.currency
        trip = (result or {}).get("trips", [None])[0]
        when = ""
        if trip:
            when = nice_day(trip["depart_date"]) + (f" to {nice_day(trip['return_date'])}" if trip.get("return_date") else "")
        subject = f"{money(price, cur)} {w.params['origin']} to {w.params['destination']}: below your {money(w.trigger, cur)} alert"
        lines = [
            f"{w.label}",
            f"Now {money(price, cur)}, at or below your alert price of {money(w.trigger, cur)}.",
        ]
        if trip:
            stops = "nonstop" if trip["max_stops"] == 0 else f"{trip['max_stops']} stop(s)"
            lines.append(f"Cheapest: {when}, {', '.join(trip['airlines'])}, {stops}.")
            if trip.get("search_url"):
                lines.append(f"Open on Google Flights: {trip['search_url']}")
        if self._link():
            lines.append(f"Your watches: {self._link()}")
        lines.append("")
        lines.append(f"You'll get another email if it drops a further {money(REALERT_DROP, cur)} or more, "
                     "or if it goes back above your alert price and then drops below it again.")
        text = "\n".join(lines)
        html_body = "<div style=\"font-family:system-ui,sans-serif;font-size:15px;line-height:1.5;color:#172033\">" + \
            "".join(f"<p style=\"margin:0 0 10px\">{html.escape(l)}</p>" for l in lines if l) + "</div>"
        try:
            self.mailer.send(user.email, subject, text, html_body)
            log(f"Alert sent to {user.email} for watch {w.id}.")
            return True
        except Exception as e:
            self.store.update(w.id, last_error=f"Couldn't send the alert email: {e}")
            log(f"Couldn't send alert for watch {w.id}: {e}")
            return False

    def send_summaries(self) -> None:
        now = datetime.fromtimestamp(self.clock())
        if now.hour < SUMMARY_HOUR:
            return
        day = now.date().isoformat()
        for user in self.users.all():
            if not user.daily_summary or not user.email or user.last_summary == day:
                continue
            watches = [w for w in self.store.for_user(user.id) if w.active and not w.ended]
            if not watches:
                continue
            lines = [f"Your {len(watches)} watched {'trip' if len(watches) == 1 else 'trips'} this morning:", ""]
            for w in watches:
                hist = [p for _, p in self.store.history(w.id, self.clock() - 86400) if p is not None]
                change = ""
                if len(hist) >= 2 and hist[-1] != hist[0]:
                    diff = hist[-1] - hist[0]
                    change = f" ({'down' if diff < 0 else 'up'} {money(abs(diff), w.currency)} since yesterday)"
                alert = f", alert at {money(w.trigger, w.currency)}" if w.trigger else ""
                lines.append(f"- {w.label}: {money(w.last_price, w.currency)}{change}{alert}")
            if self._link():
                lines += ["", f"See them all: {self._link()}"]
            lines += ["", "Turn this email off under Account."]
            text = "\n".join(lines)
            try:
                self.mailer.send(user.email, f"Flight prices for {now:%b} {now.day}", text)
                self.users.mark_summary_sent(user.id, day)
                log(f"Daily summary sent to {user.email}.")
            except Exception as e:
                log(f"Couldn't send daily summary to {user.email}: {e}")
                self.users.mark_summary_sent(user.id, day)  # don't retry every 30 seconds
