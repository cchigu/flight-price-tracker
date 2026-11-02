import re
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from flighttracker.engine import Throttle
from flighttracker.providers.fake import FakeProvider
from flighttracker.users import UserStore
from flighttracker.web import create_app

OWNER_PW = "open sesame"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("FT_OWNER", raising=False)
    return create_app(FakeProvider(), Throttle(0.01, 0.01, sleep=lambda s: None), password=OWNER_PW, data_dir=tmp_path)


def client(app):
    return TestClient(app)


def sign_in(c, name, pw):
    return c.post("/login", data={"name": name, "password": pw}, follow_redirects=False)


def add_friend(owner, name="alex", pw="friend-pass"):
    r = owner.post("/people", data={"name": name, "password": pw})
    assert r.status_code == 200, r.text
    return r


def search_body(**kw):
    t = date.today()
    b = {"origin": "FLL", "destination": "MSY", "depart_from": (t + timedelta(days=30)).isoformat(),
         "depart_to": (t + timedelta(days=33)).isoformat(), "min_days": 3, "max_days": 4}
    b.update(kw)
    return b


# ---- store ---------------------------------------------------------------------------------
def test_store_basics(tmp_path):
    s = UserStore(tmp_path)
    s.ensure_owner("admin", "ownerpw")
    s.ensure_owner("someone", "else!!")  # only on first start
    assert [u.name for u in s.all()] == ["admin"]
    assert s.authenticate("ADMIN ", "ownerpw").is_owner
    assert s.authenticate("admin", "wrong") is None
    assert s.authenticate("ghost", "ownerpw") is None
    u = s.add("Alex", "secret1")
    assert u.name == "alex" and not u.is_owner
    with pytest.raises(ValueError):
        s.add("alex", "secret2")          # duplicate
    with pytest.raises(ValueError):
        s.add("a b", "secret2")           # bad name
    with pytest.raises(ValueError):
        s.add("bo", "123")                # short password
    with pytest.raises(ValueError):
        s.remove(s.by_name("admin").id)   # owner stays
    # The hash isn't the password, and data survives a restart.
    raw = s._db.execute("SELECT password_hash FROM users WHERE name='alex'").fetchone()[0]
    assert "secret1" not in raw and raw.startswith("scrypt$")
    assert UserStore(tmp_path).authenticate("alex", "secret1") is not None


def test_no_password_means_open(tmp_path):
    c = TestClient(create_app(FakeProvider(), Throttle(0, 0, sleep=lambda s: None), password="", data_dir=tmp_path))
    assert c.get("/").status_code == 200
    assert c.get("/api/config").status_code == 200
    assert c.get("/api/me").json() == {"signed_in": False, "open": True}


# ---- signing in ---------------------------------------------------------------------------
def test_locked_until_signed_in(app):
    c = client(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/config").status_code == 401
    assert c.get("/static/app.js", follow_redirects=False).status_code == 303
    assert c.get("/people", follow_redirects=False).status_code == 303
    assert c.get("/login").status_code == 200
    assert c.get("/static/styles.css").status_code == 200
    assert c.get("/healthz").status_code == 200


def test_owner_signs_in_as_admin(app):
    c = client(app)
    r = sign_in(c, "admin", "nope")
    assert r.status_code == 401 and "don&#x27;t match" in r.text and 'value="admin"' in r.text
    assert sign_in(c, "admin", OWNER_PW).status_code == 303
    me = c.get("/api/me").json()
    assert me == {"signed_in": True, "name": "admin", "is_owner": True, "email": ""}
    assert c.get("/login", follow_redirects=False).status_code == 303
    c.post("/logout")
    assert c.get("/api/me").status_code == 401


def test_lockout_after_many_wrong_tries(app):
    c = client(app)
    for _ in range(10):
        sign_in(c, "admin", "x")
    r = sign_in(c, "admin", OWNER_PW)
    assert r.status_code == 401 and "Too many" in r.text


# ---- people page ---------------------------------------------------------------------------
def test_owner_adds_friend_who_can_sign_in(app):
    owner = client(app)
    sign_in(owner, "admin", OWNER_PW)
    r = add_friend(owner)
    assert "Name: alex" in r.text and "Password: friend-pass" in r.text
    assert "https://testserver" in r.text
    friend = client(app)
    assert sign_in(friend, "alex", "friend-pass").status_code == 303
    assert friend.get("/api/me").json()["is_owner"] is False
    # Friends can't manage people.
    assert friend.get("/people", follow_redirects=False).status_code == 303
    assert friend.post("/people", data={"name": "eve", "password": "123456"}, follow_redirects=False).status_code == 303
    assert app.state.users.by_name("eve") is None


def test_share_text_uses_public_address(app):
    owner = client(app)
    sign_in(owner, "admin", OWNER_PW)
    r = owner.post("/people", data={"name": "sam", "password": "abcdef"},
                   headers={"host": "cchigu-pc.tail1.ts.net", "x-forwarded-proto": "https"})
    assert "https://cchigu-pc.tail1.ts.net" in r.text


def test_add_errors_are_explained(app):
    owner = client(app)
    sign_in(owner, "admin", OWNER_PW)
    add_friend(owner)
    r = owner.post("/people", data={"name": "alex", "password": "another1"})
    assert r.status_code == 400 and "already a login called alex" in r.text
    r = owner.post("/people", data={"name": "x", "password": "another1"})
    assert r.status_code == 400 and "2–32 characters" in r.text


def test_reset_and_remove_sign_the_friend_out(app):
    owner = client(app)
    sign_in(owner, "admin", OWNER_PW)
    add_friend(owner)
    friend = client(app)
    sign_in(friend, "alex", "friend-pass")
    uid = app.state.users.by_name("alex").id

    r = owner.post(f"/people/{uid}/reset")
    new_pw = re.search(r"Password: ([a-z0-9-]+)", r.text).group(1)
    assert friend.get("/api/me").status_code == 401          # old session ended
    assert sign_in(friend, "alex", "friend-pass").status_code == 401
    assert sign_in(friend, "alex", new_pw).status_code == 303

    owner.post(f"/people/{uid}/remove")
    assert friend.get("/api/me").status_code == 401
    assert app.state.users.by_name("alex") is None


# ---- your account --------------------------------------------------------------------------
def test_change_own_password_and_email(app):
    owner = client(app)
    sign_in(owner, "admin", OWNER_PW)
    add_friend(owner)
    friend, laptop = client(app), client(app)
    sign_in(friend, "alex", "friend-pass")
    sign_in(laptop, "alex", "friend-pass")

    r = friend.post("/account/password", data={"current": "wrong", "new": "brand-new"})
    assert r.status_code == 400 and "current password" in r.text
    r = friend.post("/account/password", data={"current": "friend-pass", "new": "brand-new"}, follow_redirects=False)
    assert r.status_code == 303
    assert friend.get("/api/me").status_code == 200        # stays signed in here
    assert laptop.get("/api/me").status_code == 401        # other devices signed out

    assert friend.post("/account/email", data={"email": "nope"}).status_code == 400
    friend.post("/account/email", data={"email": "alex@example.com"})
    assert friend.get("/api/me").json()["email"] == "alex@example.com"
    assert "alex@example.com" in owner.get("/people").text


# ---- searches are private ------------------------------------------------------------------
def test_searches_are_private_and_one_at_a_time(app):
    owner = client(app)
    sign_in(owner, "admin", OWNER_PW)
    add_friend(owner)
    friend = client(app)
    sign_in(friend, "alex", "friend-pass")

    job = friend.post("/api/searches", json=search_body()).json()
    assert friend.get(f"/api/searches/{job['id']}").status_code == 200
    assert owner.get(f"/api/searches/{job['id']}").status_code == 404
    assert owner.post(f"/api/searches/{job['id']}/cancel").status_code == 404
    assert friend.get("/api/searches/active").json()["id"] in (job["id"], None)
    assert owner.get("/api/searches/active").json()["id"] is None

    # Only one search each at a time.
    r = friend.post("/api/searches", json=search_body())
    if friend.get(f"/api/searches/{job['id']}").json()["status"] in ("queued", "running"):
        assert r.status_code == 409 and "already have a search" in r.json()["detail"]
    # The owner can still start their own.
    assert owner.post("/api/searches", json=search_body()).status_code == 201


def test_removing_friend_stops_their_search(app):
    owner = client(app)
    sign_in(owner, "admin", OWNER_PW)
    add_friend(owner)
    friend = client(app)
    sign_in(friend, "alex", "friend-pass")
    job = friend.post("/api/searches", json=search_body(depart_to=(date.today() + timedelta(days=60)).isoformat())).json()
    uid = app.state.users.by_name("alex").id
    owner.post(f"/people/{uid}/remove")
    assert job["id"] not in app.state.runner.jobs


def test_tokens_expire(app):
    gate_users = app.state.users
    from flighttracker.auth import Gate
    g = Gate(gate_users)
    owner = gate_users.by_name("admin")
    t = g.make_token(owner, now=1000)
    assert g.user_from_token(t, now=2000).name == "admin"
    assert g.user_from_token(t, now=1000 + 31 * 24 * 3600) is None
    assert g.user_from_token(t.replace(".", "x", 1), now=2000) is None
    assert g.user_from_token("1.1.99999999999.forged") is None
    assert g.user_from_token(None) is None
