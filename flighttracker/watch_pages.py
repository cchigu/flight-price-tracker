"""The Watches page and the API the search page uses to start watching."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .accounts import _form
from .auth import esc, page
from .checker import money, nice_day
from .watches import REALERT_DROP, Watch, WatchStore, describe

WATCH_NOTICES = {
    "deleted": "Watch deleted.",
    "saved": "Alert price saved.",
    "paused": "Watch paused. It won't be checked until you resume it.",
    "resumed": "Watch resumed.",
    "checking": "Checking now. Refresh in a minute to see the price.",
}


class WatchIn(BaseModel):
    search: dict
    trigger: Optional[int] = Field(None, ge=1, le=1_000_000)


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "not checked yet"
    s = int(time.time() - ts)
    if s < 90:
        return "checked just now"
    if s < 5400:
        return f"checked {s // 60} min ago"
    if s < 172800:
        return f"checked {s // 3600} h ago"
    return f"checked {s // 86400} days ago"


def until(ts: float) -> str:
    s = int(ts - time.time())
    if s <= 60:
        return "next check in under a minute"
    if s < 5400:
        return f"next check in {s // 60} min"
    return f"next check in {round(s / 3600)} h"


def sparkline(points: list[tuple[float, Optional[int]]], trigger: Optional[int], cur: str) -> str:
    """A small price-over-time line with the alert price drawn as a dashed line."""
    pts = [(t, p) for t, p in points if p is not None]
    if len(pts) < 2:
        return '<p class="hint">The price chart appears after a few checks.</p>'
    w, h, pad = 320, 70, 6
    t0, t1 = pts[0][0], pts[-1][0]
    prices = [p for _, p in pts] + ([trigger] if trigger else [])
    lo, hi = min(prices), max(prices)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    x = lambda t: pad + (w - 2 * pad) * ((t - t0) / (t1 - t0) if t1 > t0 else 1)
    y = lambda p: pad + (h - 2 * pad) * (1 - (p - lo) / (hi - lo))
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(p):.1f}" for i, (t, p) in enumerate(pts))
    trig = ""
    if trigger:
        ty = y(trigger)
        trig = f'<line x1="{pad}" x2="{w - pad}" y1="{ty:.1f}" y2="{ty:.1f}" class="spark-trigger"/>'
    lx, ly = x(pts[-1][0]), y(pts[-1][1])
    first = datetime.fromtimestamp(t0)
    label = (f"Price from {money(min(p for _, p in pts), cur)} to {money(max(p for _, p in pts), cur)} "
             f"since {first:%b} {first.day}")
    return (f'<figure class="spark"><svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">{trig}'
            f'<path d="{path}" class="spark-line"/><circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" class="spark-dot"/></svg>'
            f'<figcaption class="hint">{esc(label)}{", dashed line is your alert price" if trigger else ""}</figcaption></figure>')


def watch_card(w: Watch, store: WatchStore) -> str:
    cur = w.currency
    every = "every 15 minutes" if w.kind == "fixed" else "every 6 hours"
    if w.ended:
        status = "Ended: these dates have passed."
    elif not w.active:
        status = "Paused."
    else:
        status = f"Checked {every}, {ago(w.last_checked)}, {until(w.next_check)}."
    price = money(w.last_price, cur) if w.last_checked else "Not checked yet"
    hit = w.trigger is not None and w.last_price is not None and w.last_price <= w.trigger
    trip_line = ""
    trips = (w.last_result or {}).get("trips") or []
    if trips:
        t = trips[0]
        when = nice_day(t["depart_date"]) + (f" to {nice_day(t['return_date'])}" if t.get("return_date") else "")
        stops = "Nonstop" if t["max_stops"] == 0 else f"{t['max_stops']} stop{'s' if t['max_stops'] > 1 else ''}"
        link = f' <a href="{esc(t["search_url"])}" target="_blank" rel="noopener">Open on Google Flights</a>' if t.get("search_url") else ""
        cheapest = "Cheapest" if w.kind == "flexible" else "Best option"
        trip_line = f'<p class="hint">{cheapest}: {esc(when)}, {esc(", ".join(t["airlines"]))}, {stops}.{link}</p>'
    err = f'<p class="form-error">{esc(w.last_error)}</p>' if w.last_error and not w.ended else ""
    toggle = ("" if w.ended else
              f'<form method="post" action="/watches/{w.id}/{"pause" if w.active else "resume"}"><button class="link" type="submit">{"Pause" if w.active else "Resume"}</button></form>')
    check = (f'<form method="post" action="/watches/{w.id}/check"><button class="link" type="submit">Check now</button></form>'
             if w.active and not w.ended else "")
    return f"""<li class="watch{' hit' if hit else ''}">
  <div class="watch-head">
    <div><h2>{esc(w.label)}</h2><p class="hint">{esc(status)}</p></div>
    <div class="watch-price"><strong>{esc(price)}</strong>{'<span class="tag">Below alert</span>' if hit else ''}</div>
  </div>
  {trip_line}{err}
  {sparkline(store.history(w.id), w.trigger, cur)}
  <div class="watch-actions">
    <form method="post" action="/watches/{w.id}/trigger" class="trigger-form">
      <label class="field inline-field"><span>Email me at or below</span>
        <span class="unit"><input type="number" name="trigger" min="1" inputmode="numeric" value="{w.trigger or ''}" placeholder="No alert"><em>{esc(cur)}</em></span></label>
      <button class="secondary" type="submit">Save</button>
    </form>
    <div class="watch-links">{check}{toggle}
      <form method="post" action="/watches/{w.id}/delete" onsubmit="return confirm('Delete this watch and its price history?')"><button class="link danger" type="submit">Delete</button></form>
    </div>
  </div>
</li>"""


def register(app: FastAPI, store: WatchStore, checker) -> None:
    def me(request: Request):
        return request.state.user

    def owned(request: Request, watch_id: int) -> Optional[Watch]:
        u, w = me(request), store.get(watch_id)
        return w if (u is not None and w is not None and w.user_id == u.id) else None

    # ---- API used by the search page --------------------------------------------------------------
    @app.post("/api/watches", status_code=201)
    def create_watch(body: WatchIn, request: Request):
        u = me(request)
        if u is None:
            raise HTTPException(403, "Turn on sign-in (set a password) to save watches.")
        from .web import SearchIn  # avoid a circular import at load time
        try:
            search = SearchIn(**body.search)
        except Exception as e:
            raise HTTPException(422, "Those search settings aren't valid.") from e
        req = search.to_request()
        try:
            req.validate()
        except ValueError as e:
            raise HTTPException(422, str(e))
        params = search.model_dump(mode="json")
        params["origin"], params["destination"] = req.origin, req.destination
        params["currency"] = params["currency"].upper()
        try:
            w = store.add(u.id, params, body.trigger)
        except ValueError as e:
            raise HTTPException(409, str(e))
        every = "every 15 minutes" if w.kind == "fixed" else "every 6 hours"
        msg = f"Watching {describe(params)}. Checked {every}."
        if not u.email:
            msg += " Add your email under Account to get alerts."
        elif body.trigger is None:
            msg += " Set an alert price on the Watches page to get emails."
        return {"id": w.id, "kind": w.kind, "message": msg}

    # ---- the page ---------------------------------------------------------------------------------
    @app.get("/watches")
    def watches(request: Request):
        u = me(request)
        if u is None:
            return RedirectResponse("/", status_code=303)
        items = store.for_user(u.id)
        warn = ""
        if not u.email:
            warn = '<p class="notice warn">Add your email under <a href="/account">Your account</a> so alerts can reach you.</p>'
        elif not checker.mailer.configured():
            warn = ('<p class="notice warn">Email sending isn\'t set up yet, so alerts can\'t go out. '
                    + ('Set it up under <a href="/account">Your account</a>.' if u.is_owner else 'Ask the owner to set it up.') + '</p>')
        empty = """<section class="card"><h2>No watches yet</h2>
<p class="hint">On the search page, set an alert price under “Price alert” and choose <strong>Watch this search</strong>,
or open a trip in the results and choose <strong>Watch these dates</strong>.</p></section>"""
        done = request.query_params.get("done", "")
        notice = f'<p class="notice" role="status">{esc(WATCH_NOTICES[done])}</p>' if done in WATCH_NOTICES else ""
        body = f"""<h1>Watches</h1>
<p class="lede">Fixed dates are checked every 15 minutes, flexible dates every 6 hours. You get an email the first time a price
reaches your alert price, then again only if it drops a further {REALERT_DROP} or more, or after it goes back above and drops below again.</p>
{notice}{warn}
{'<ul class="watches">' + ''.join(watch_card(w, store) for w in items) + '</ul>' if items else empty}"""
        return page("Watches", body, 200, u)

    def back(done: str):
        return RedirectResponse(f"/watches?done={done}", status_code=303)

    @app.post("/watches/{watch_id}/trigger")
    async def set_trigger(request: Request, watch_id: int):
        w = owned(request, watch_id)
        if w is None:
            return RedirectResponse("/watches", status_code=303)
        raw = (await _form(request)).get("trigger", "").strip()
        trigger = int(raw) if raw.isdigit() and int(raw) > 0 else None
        # A new alert price starts fresh: the next check can email if it's already below.
        store.update(w.id, trigger=trigger, below=0, last_alert_price=None)
        return back("saved")

    @app.post("/watches/{watch_id}/pause")
    def pause(request: Request, watch_id: int):
        w = owned(request, watch_id)
        if w:
            store.update(w.id, active=0)
        return back("paused")

    @app.post("/watches/{watch_id}/resume")
    def resume(request: Request, watch_id: int):
        w = owned(request, watch_id)
        if w:
            store.update(w.id, active=1, next_check=time.time())
        return back("resumed")

    @app.post("/watches/{watch_id}/check")
    def check_now(request: Request, watch_id: int):
        w = owned(request, watch_id)
        if w:
            store.update(w.id, next_check=time.time())
        return back("checking")

    @app.post("/watches/{watch_id}/delete")
    def delete(request: Request, watch_id: int):
        w = owned(request, watch_id)
        if w:
            store.delete(w.id)
        return back("deleted")
