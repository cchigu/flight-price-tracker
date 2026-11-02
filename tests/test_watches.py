from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from flighttracker.checker import Checker
from flighttracker.engine import Throttle
from flighttracker.mailer import FakeMailer
from flighttracker.models import Itinerary, Leg, Segment
from flighttracker.providers.base import PriceProvider, ProviderBlocked
from flighttracker.providers.fake import FakeProvider
from flighttracker.users import UserStore
from flighttracker.watches import (WatchStore, decide_alert, describe, ended, kind_of,
                                   request_from_params)
from flighttracker.web import SearchRunner, create_app

TODAY = date.today()
NO_WAIT = Throttle(0, 0, block_backoff=(0, 0, 0), sleep=lambda s: None)


def params(**kw):
    p = {"origin": "FLL", "destination": "MSY", "depart_from": (TODAY + timedelta(days=30)).isoformat(),
         "depart_to": (TODAY + timedelta(days=30)).isoformat(), "min_days": 5, "max_days": 5,
         "trip_type": "round-trip", "stops": "any", "max_stops": None, "cabin": "economy",
         "passengers": {"adults": 1, "children": 0, "infants_in_seat": 0, "infants_on_lap": 0},
         "currency": "USD", "top_n": 10, "rank_by": "price"}
    p.update(kw)
    return p


# ---- the alert rule ---------------------------------------------------------------------------
@pytest.mark.parametrize("price, trigger, below, last, expect", [
    (300, 250, False, None, (False, False, None)),   # above: nothing
    (250, 250, False, None, (True, True, 250)),      # reaches trigger: email
    (240, 250, True, 250, (False, True, 250)),       # small further drop: quiet
    (225, 250, True, 250, (True, True, 225)),        # $25 further: email
    (260, 250, True, 225, (False, False, None)),     # back above: re-arm
    (249, 250, False, None, (True, True, 249)),      # dips again: email
    (None, 250, True, 240, (False, True, 240)),      # no price found: keep state
    (100, None, False, None, (False, False, None)),  # no alert price set
])
def test_decide_alert(price, trigger, below, last, expect):
    assert decide_alert(price, trigger, below, last) == expect


def test_kinds_labels_and_windows():
    fixed = params()
    flex = params(depart_to=(TODAY + timedelta(days=60)).isoformat(), min_days=20, max_days=25)
    one_way = params(trip_type="one-way", min_days=1, max_days=9)
    assert kind_of(fixed) == "fixed" and kind_of(flex) == "flexible" and kind_of(one_way) == "fixed"
    d = TODAY + timedelta(days=30)
    assert describe(fixed).startswith("FLL to MSY, ") and " to " in describe(fixed)
    assert "20–25 days" in describe(flex)
    assert "one way" in describe(one_way)
    # A window that has started is moved to today; one that has passed has ended.
    started = params(depart_from=(TODAY - timedelta(days=3)).isoformat(), depart_to=(TODAY + timedelta(days=3)).isoformat())
    assert request_from_params(started).depart_from == TODAY
    assert ended(params(depart_from="2020-01-01", depart_to="2020-01-02"))
    assert not ended(fixed)
    assert request_from_params(fixed).depart_from == d


def test_store_limits_and_duplicates(tmp_path):
    s = WatchStore(tmp_path)
    w = s.add(1, params(), 200)
    assert w.kind == "fixed" and w.trigger == 200 and w.active
    with pytest.raises(ValueError, match="already watching"):
        s.add(1, params(), 150)
    s.add(2, params(), 150)  # someone else can watch the same thing
    for i in range(3):
        s.add(1, params(depart_to=(TODAY + timedelta(days=40 + i)).isoformat()), None)
    with pytest.raises(ValueError, match="up to 3 flexible"):
        s.add(1, params(depart_to=(TODAY + timedelta(days=50)).isoformat()), None)
    s.delete_for_user(1)
    assert s.for_user(1) == [] and len(s.for_user(2)) == 1


# ---- the checker ------------------------------------------------------------------------------
class Scripted(PriceProvider):
    """Returns the next price from a list for every search."""
    name = "scripted"

    def __init__(self, prices):
        self.prices, self.calls = list(prices), 0

    def search(self, origin, destination, depart, return_date, options):
        self.calls += 1
        p = self.prices.pop(0)
        if p == "blocked":
            raise ProviderBlocked("HTTP 429")
        if p is None:
            return []
        t = datetime.combine(depart, datetime.min.time()).replace(hour=9)
        leg = Leg((Segment(origin, destination, t, t + timedelta(hours=2), 120, "DL", "100"),))
        return [Itinerary(p, options.currency, ("Delta",), leg, search_url="https://example.com/flights")]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def setup(tmp_path, prices, email="me@example.com"):
    users = UserStore(tmp_path)
    users.ensure_owner("admin", "ownerpw")
    owner = users.by_name("admin")
    if email:
        users.set_email(owner.id, email)
    store = WatchStore(tmp_path)
    runner = SearchRunner(Scripted(prices), NO_WAIT)
    mail = FakeMailer()
    clock = Clock(datetime.combine(TODAY, datetime.min.time()).replace(hour=6).timestamp())
    return users, owner, store, Checker(store, users, runner, mail, clock=clock), mail, clock


def test_fixed_watch_alerts_by_the_rules(tmp_path):
    users, owner, store, chk, mail, clock = setup(tmp_path, [300, 250, 240, 225, 260, 249, None])
    w = store.add(owner.id, params(), 250, now=clock.t)
    subjects = []
    for _ in range(7):
        chk.tick()
        subjects.append(len(mail.sent))
        clock.t += 15 * 60 + 1
    # emails after the 2nd (250), 4th (225) and 6th (249) checks only
    assert subjects == [0, 1, 1, 2, 2, 3, 3]
    assert "$250" in mail.sent[0]["subject"] and mail.sent[0]["to"] == "me@example.com"
    assert "Open on Google Flights: https://example.com/flights" in mail.sent[0]["text"]
    w = store.get(w.id)
    assert w.last_price is None and "No flights matched" in w.last_error
    assert [p for _, p in store.history(w.id, since=0)] == [300, 250, 240, 225, 260, 249, None]


def test_not_due_and_paused_watches_are_skipped(tmp_path):
    users, owner, store, chk, mail, clock = setup(tmp_path, [300, 300])
    w = store.add(owner.id, params(), 250, now=clock.t)
    chk.tick()
    chk.tick()  # 0 seconds later: not due
    assert chk.runner.provider.calls == 1
    store.update(w.id, active=0)
    clock.t += 3600
    chk.tick()
    assert chk.runner.provider.calls == 1


def test_block_pauses_all_checks(tmp_path):
    users, owner, store, chk, mail, clock = setup(tmp_path, ["blocked", 200])
    w = store.add(owner.id, params(), 250, now=clock.t)
    chk.tick()
    assert chk.paused_until > clock.t and "refusing" in store.get(w.id).last_error
    clock.t += 60
    chk.tick()
    assert chk.runner.provider.calls == 1   # still paused
    clock.t += 31 * 60
    chk.tick()
    assert store.get(w.id).last_price == 200 and len(mail.sent) == 1


def test_undelivered_alert_is_retried(tmp_path):
    users, owner, store, chk, mail, clock = setup(tmp_path, [100, 100], email="")
    w = store.add(owner.id, params(), 250, now=clock.t)
    chk.tick()
    assert mail.sent == [] and "no email on your account" in store.get(w.id).last_error
    users.set_email(owner.id, "me@example.com")   # fixed: the next check sends it
    clock.t += 15 * 60 + 1
    chk.tick()
    assert len(mail.sent) == 1


def test_failed_send_is_retried(tmp_path):
    users, owner, store, chk, mail, clock = setup(tmp_path, [100, 100])
    w = store.add(owner.id, params(), 250, now=clock.t)

    def broken(*a, **k):
        raise RuntimeError("Gmail said no")
    real, mail.send = mail.send, broken
    chk.tick()
    assert "Gmail said no" in store.get(w.id).last_error and not store.get(w.id).below
    mail.send = real
    clock.t += 15 * 60 + 1
    chk.tick()
    assert len(mail.sent) == 1


def test_past_dates_end_the_watch(tmp_path):
    users, owner, store, chk, mail, clock = setup(tmp_path, [])
    w = store.add(owner.id, params(depart_from="2020-01-01", depart_to="2020-01-01"), 250, now=clock.t)
    chk.tick()
    assert store.get(w.id).ended


def test_flexible_watch_scans_the_window(tmp_path):
    users = UserStore(tmp_path)
    users.ensure_owner("admin", "ownerpw")
    owner = users.by_name("admin")
    users.set_email(owner.id, "me@example.com")
    store = WatchStore(tmp_path)
    provider = FakeProvider()
    chk = Checker(store, users, SearchRunner(provider, NO_WAIT), FakeMailer(), clock=Clock(1e9))
    p = params(destination="DEL", depart_to=(TODAY + timedelta(days=35)).isoformat(), min_days=20, max_days=22)
    w = store.add(owner.id, p, 5000, now=1e9)
    chk.tick()
    w = store.get(w.id)
    assert provider.calls == 6 * 3          # every pair, no return-detail lookups
    assert w.last_price == min(t["price"] for t in w.last_result["trips"])
    assert w.next_check == pytest.approx(1e9 + 6 * 3600, abs=5)
    assert len(chk.mailer.sent) == 1


def test_daily_summary_once_a_day_after_8(tmp_path):
    users, owner, store, chk, mail, clock = setup(tmp_path, [300] * 5)
    store.add(owner.id, params(), 250, now=clock.t)
    users.set_daily_summary(owner.id, True)
    chk.tick()                                   # 6 AM: price checked, no summary yet
    assert mail.sent == []
    clock.t += 2 * 3600 + 60                     # 8:01 AM
    chk.tick()
    assert len(mail.sent) == 1 and mail.sent[0]["subject"].startswith("Flight prices for")
    assert "FLL to MSY" in mail.sent[0]["text"] and "$300" in mail.sent[0]["text"]
    clock.t += 3600
    chk.tick()
    assert len(mail.sent) == 1                   # not again today


# ---- pages and API ------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path):
    return create_app(FakeProvider(), NO_WAIT, password="ownerpw", data_dir=tmp_path,
                      mailer=FakeMailer(), start_checker=False)


def signed_in(app, name="admin", pw="ownerpw"):
    c = TestClient(app)
    assert c.post("/login", data={"name": name, "password": pw}, follow_redirects=False).status_code == 303
    return c


def test_create_and_manage_watch(app):
    c = signed_in(app)
    r = c.post("/api/watches", json={"search": params(), "trigger": 250})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "fixed" and "every 15 minutes" in r.json()["message"]
    assert "Add your email" in r.json()["message"]
    assert c.post("/api/watches", json={"search": params(), "trigger": 250}).status_code == 409
    bad = c.post("/api/watches", json={"search": params(origin="X"), "trigger": 250})
    assert bad.status_code == 422

    page = c.get("/watches").text
    assert "FLL to MSY" in page and "Not checked yet" in page and "Add your email" in page
    wid = r.json()["id"]
    c.post(f"/watches/{wid}/trigger", data={"trigger": "199"})
    assert app.state.watches.get(wid).trigger == 199
    c.post(f"/watches/{wid}/trigger", data={"trigger": ""})
    assert app.state.watches.get(wid).trigger is None
    c.post(f"/watches/{wid}/pause")
    assert not app.state.watches.get(wid).active
    c.post(f"/watches/{wid}/resume")
    assert app.state.watches.get(wid).active
    c.post(f"/watches/{wid}/delete")
    assert app.state.watches.get(wid) is None


def test_watches_are_private(app):
    owner = signed_in(app)
    owner.post("/people", data={"name": "alex", "password": "friendpw"})
    friend = signed_in(app, "alex", "friendpw")
    wid = owner.post("/api/watches", json={"search": params(), "trigger": 250}).json()["id"]
    assert "FLL to MSY" not in friend.get("/watches").text
    friend.post(f"/watches/{wid}/delete")
    friend.post(f"/watches/{wid}/trigger", data={"trigger": "1"})
    assert app.state.watches.get(wid).trigger == 250
    # Removing a person removes their watches.
    fw = friend.post("/api/watches", json={"search": params(), "trigger": 250}).json()["id"]
    owner.post(f"/people/{app.state.users.by_name('alex').id}/remove")
    assert app.state.watches.get(fw) is None


def test_checked_watch_shows_price_and_chart(app):
    c = signed_in(app)
    wid = c.post("/api/watches", json={"search": params(), "trigger": 100000}).json()["id"]
    chk = app.state.checker
    for _ in range(3):
        app.state.watches.update(wid, next_check=0)
        chk.tick()
    page = c.get("/watches").text
    assert "Below alert" in page and "<svg" in page and "Open on Google Flights" in page


def test_owner_email_settings_and_test(app):
    c = signed_in(app)
    c.post("/account/email", data={"email": "owner@example.com"})
    page = c.get("/account").text
    assert "Sending emails (owner)" in page and "Not set up yet" in page
    r = c.post("/account/mail", data={"user": "sender@gmail.com", "password": "abcd efgh ijkl mnop", "action": "test"},
               follow_redirects=False)
    assert r.status_code == 303 and "mail-test" in r.headers["location"]
    assert app.state.mailer.sent[-1]["to"] == "owner@example.com"
    assert app.state.watches.get_setting("smtp_password") == "abcd efgh ijkl mnop"
    # Leaving the password empty keeps it.
    c.post("/account/mail", data={"user": "sender@gmail.com", "password": "", "action": "save"})
    assert app.state.watches.get_setting("smtp_password") == "abcd efgh ijkl mnop"
    assert "Sending from <strong>sender@gmail.com</strong>" in c.get("/account").text
    # Friends don't see or change it.
    c.post("/people", data={"name": "alex", "password": "friendpw"})
    f = signed_in(app, "alex", "friendpw")
    assert "Sending emails" not in f.get("/account").text
    f.post("/account/mail", data={"user": "evil@gmail.com", "password": "x", "action": "save"})
    assert app.state.watches.get_setting("smtp_user") == "sender@gmail.com"


def test_summary_toggle(app):
    c = signed_in(app)
    c.post("/account/summary", data={"on": "1"})
    assert app.state.users.by_name("admin").daily_summary
    c.post("/account/summary", data={})
    assert not app.state.users.by_name("admin").daily_summary


def test_public_url_is_learned_from_the_browser(app):
    c = signed_in(app)
    c.get("/api/me", headers={"referer": "https://cchigu-pc.tail1.ts.net/"})
    assert app.state.watches.get_setting("public_url") == "https://cchigu-pc.tail1.ts.net"
    c.get("/api/me", headers={"referer": "http://127.0.0.1:8000/"})
    assert app.state.watches.get_setting("public_url") == "https://cchigu-pc.tail1.ts.net"
