"""Sign-in for several people.

Each person signs in with a name and password and gets a signed cookie for 30 days. A cookie
carries the person's id and their session version; changing a password or removing a login
bumps/deletes that, which signs them out everywhere.

When there are no logins at all (no FT_PASSWORD set on first start) the page is open, which is
handy for trying it locally.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .users import User, UserStore

COOKIE = "ft_session"
MAX_AGE = 30 * 24 * 3600
# Paths that work without signing in (the sign-in page needs its styles and fonts).
OPEN_PATHS = ("/login", "/static/fonts/", "/static/styles.css", "/healthz")


class Gate:
    def __init__(self, users: UserStore, secure_cookie: bool = False):
        self.users = users
        self.secure_cookie = secure_cookie
        self._fails: dict[str, list[float]] = {}

    @property
    def open(self) -> bool:
        return self.users.count() == 0

    # ---- cookies ----------------------------------------------------------------------
    def _sig(self, payload: str) -> str:
        return hmac.new(self.users.secret, payload.encode(), hashlib.sha256).hexdigest()

    def make_token(self, user: User, now: Optional[float] = None) -> str:
        payload = f"{user.id}.{user.session_version}.{int((now or time.time()) + MAX_AGE)}"
        return f"{payload}.{self._sig(payload)}"

    def user_from_token(self, token: Optional[str], now: Optional[float] = None) -> Optional[User]:
        try:
            uid, ver, expires, sig = (token or "").split(".")
            if not hmac.compare_digest(sig, self._sig(f"{uid}.{ver}.{expires}")):
                return None
            if int(expires) < (now or time.time()):
                return None
            user = self.users.get(int(uid))
        except (ValueError, TypeError):
            return None
        if user is None or user.session_version != int(ver):
            return None
        return user

    def current(self, request: Request) -> Optional[User]:
        return self.user_from_token(request.cookies.get(COOKIE))

    def sign_in(self, resp: Response, user: User) -> None:
        resp.set_cookie(COOKIE, self.make_token(user), max_age=MAX_AGE, httponly=True,
                        samesite="lax", secure=self.secure_cookie)

    @staticmethod
    def sign_out(resp: Response) -> None:
        resp.delete_cookie(COOKIE)

    # ---- brute-force brake: 10 wrong tries in 15 minutes locks that address out --------
    def try_login(self, who: str, name: str, password: str) -> tuple[Optional[User], str]:
        cutoff = time.time() - 900
        tries = [t for t in self._fails.get(who, []) if t > cutoff]
        self._fails[who] = tries
        if len(tries) >= 10:
            return None, "Too many wrong tries. Wait 15 minutes and try again."
        user = self.users.authenticate(name, password)
        if user is None:
            tries.append(time.time())
            return None, "That name and password don't match."
        self._fails.pop(who, None)
        return user, ""

    # ---- middleware -------------------------------------------------------------------
    async def __call__(self, request: Request, call_next) -> Response:
        request.state.user = None
        if self.open:
            return await call_next(request)
        path = request.url.path
        user = self.current(request)
        request.state.user = user
        if user is not None or path.startswith(OPEN_PATHS):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Sign in again to keep using the tracker."}, status_code=401)
        return RedirectResponse("/login", status_code=303)


# ---- small server-rendered pages --------------------------------------------------------
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def page(title: str, body: str, status: int = 200, user: Optional[User] = None) -> HTMLResponse:
    nav = ""
    if user is not None:
        people = '<a href="/people">People</a>' if user.is_owner else ""
        nav = (f'<nav class="page-nav"><a href="/">Search</a><a href="/watches">Watches</a>{people}<a href="/account">Your account</a>'
               f'<form method="post" action="/logout"><button class="link" type="submit">Sign out</button></form></nav>')
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Flight Price Tracker</title><link rel="stylesheet" href="/static/styles.css"></head>
<body class="sheet-body"><main class="sheet">{nav}{body}</main></body></html>""", status_code=status)


def login_page(error: str = "", name: str = "") -> HTMLResponse:
    msg = f'<p class="form-error" role="alert">{esc(error)}</p>' if error else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Flight Price Tracker</title><link rel="stylesheet" href="/static/styles.css"></head>
<body class="login-body"><form class="panel login" method="post" action="/login">
<header class="brand"><h1>Flight Price Tracker</h1><p>Sign in to continue.</p></header>
<label class="field"><span>Name</span><input name="name" value="{esc(name)}" autocomplete="username" autocapitalize="none" spellcheck="false" required {'' if name else 'autofocus'}></label>
<label class="field"><span>Password</span><input type="password" name="password" autocomplete="current-password" required {'autofocus' if name else ''}></label>
{msg}<button class="primary" type="submit">Sign in</button></form></body></html>""",
                        status_code=401 if error else 200)
