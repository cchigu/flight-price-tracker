from fastapi.testclient import TestClient

from flighttracker.auth import PasswordGate
from flighttracker.engine import Throttle
from flighttracker.providers.fake import FakeProvider
from flighttracker.web import create_app


def app(password="open sesame"):
    return TestClient(create_app(FakeProvider(), Throttle(0, 0, sleep=lambda s: None), password=password))


def test_no_password_means_open():
    c = app(password="")
    assert c.get("/").status_code == 200
    assert c.get("/api/config").status_code == 200
    assert c.get("/login").status_code == 404


def test_locked_until_signed_in():
    c = app()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/config").status_code == 401
    assert c.get("/static/app.js", follow_redirects=False).status_code == 303
    # The sign-in page and its styles still load.
    assert c.get("/login").status_code == 200
    assert c.get("/static/styles.css").status_code == 200
    assert c.get("/healthz").status_code == 200


def test_wrong_then_right_password():
    c = app()
    r = c.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401 and "isn't right" in r.text
    r = c.post("/login", data={"password": "open sesame"}, follow_redirects=False)
    assert r.status_code == 303 and "ft_session" in r.cookies
    assert c.get("/").status_code == 200
    assert c.get("/api/config").json()["provider"] == "fake"
    # Already signed in: the login page sends you on.
    assert c.get("/login", follow_redirects=False).status_code == 303


def test_lockout_after_many_wrong_tries():
    c = app()
    for _ in range(10):
        c.post("/login", data={"password": "x"})
    r = c.post("/login", data={"password": "open sesame"}, follow_redirects=False)
    assert r.status_code == 401 and "Too many" in r.text


def test_tokens_expire_and_change_with_password():
    g = PasswordGate("a")
    t = g.make_token(now=1000)
    assert g.valid(t, now=2000)
    assert not g.valid(t, now=1000 + 31 * 24 * 3600)
    assert not PasswordGate("b").valid(t, now=2000)
    assert not g.valid("123.forged", now=0)
    assert not g.valid(None)
