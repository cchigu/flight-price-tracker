import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from flighttracker.engine import Throttle
from flighttracker.providers.fake import FakeProvider
from flighttracker.web import create_app

TODAY = date.today()


@pytest.fixture
def client():
    app = create_app(FakeProvider(), Throttle(0, 0, sleep=lambda s: None))
    with TestClient(app) as c:
        yield c


def body(**kw):
    b = {"origin": "FLL", "destination": "DEL",
         "depart_from": (TODAY + timedelta(days=40)).isoformat(),
         "depart_to": (TODAY + timedelta(days=60)).isoformat(),
         "min_days": 20, "max_days": 25}
    b.update(kw)
    return b


def wait(client, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/searches/{job_id}").json()
        if j["final"] or j["status"] == "failed":
            return j
        time.sleep(0.05)
    raise AssertionError("search did not finish")


def test_page_and_config(client):
    r = client.get("/")
    assert r.status_code == 200 and "Flight Price Tracker" in r.text
    assert client.get("/static/app.js").status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["provider"] == "fake" and cfg["today"] == TODAY.isoformat()


def test_estimate(client):
    r = client.post("/api/estimate", json=body())
    assert r.status_code == 200
    assert r.json()["checks"] == 21 * 6


def test_validation_messages(client):
    r = client.post("/api/estimate", json=body(origin="XX"))
    assert r.status_code == 422 and "airport code" in r.json()["detail"]
    r = client.post("/api/searches", json=body(passengers={"adults": 1, "infants_on_lap": 2}))
    assert r.status_code == 422 and "lap infant" in r.json()["detail"]


def test_full_search_flow(client):
    r = client.post("/api/searches", json=body())
    assert r.status_code == 201
    job = wait(client, r.json()["id"])
    assert job["status"] == "done" and job["final"]
    assert len(job["results"]) == 10
    assert len({t["depart_date"] for t in job["results"]}) == 10
    assert len(job["days"]) == 21  # every departure day got a cheapest fare for the calendar
    assert all(t["inbound"] for t in job["results"])
    assert min(job["days"].values()) == job["results"][0]["price"]


def test_options_pass_through(client):
    r = client.post("/api/searches", json=body(destination="MSY", min_days=3, max_days=4, stops="direct",
                                               rank_by="duration", currency="eur", top_n=5))
    job = wait(client, r.json()["id"])
    assert len(job["results"]) == 5 and job["currency"] == "EUR"
    assert all(t["max_stops"] == 0 for t in job["results"])
    durs = [t["total_duration_min"] for t in job["results"]]
    assert durs == sorted(durs)


def test_cancel_and_missing(client):
    assert client.get("/api/searches/nope").status_code == 404
    slow = create_app(FakeProvider(), Throttle(0.02, 0.02))
    with TestClient(slow) as c:
        jid = c.post("/api/searches", json=body()).json()["id"]
        time.sleep(0.2)
        assert c.post(f"/api/searches/{jid}/cancel").status_code == 200
        job = wait(c, jid)
        assert job["status"] == "cancelled" and job["checks_done"] < job["checks_total"]
