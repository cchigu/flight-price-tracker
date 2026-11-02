"""Single-password login for when the page is on the internet.

Turned on by setting FT_PASSWORD. After signing in, the browser keeps a signed cookie for
30 days. Changing the password signs every browser out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE = "ft_session"
MAX_AGE = 30 * 24 * 3600
# Paths that work without signing in (the login page needs its own styles and fonts).
OPEN_PATHS = ("/login", "/static/fonts/", "/static/styles.css", "/healthz")


class PasswordGate:
    def __init__(self, password: str, secure_cookie: bool = False):
        self.password = password
        # Key derived from the password, so a new password invalidates old cookies.
        self.key = hashlib.sha256(b"flight-tracker-session|" + password.encode()).digest()
        self.secure_cookie = secure_cookie
        self._fails: dict[str, list[float]] = {}

    # ---- cookies ----------------------------------------------------------------------
    def make_token(self, now: Optional[float] = None) -> str:
        expires = int((now or time.time()) + MAX_AGE)
        sig = hmac.new(self.key, str(expires).encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{sig}"

    def valid(self, token: Optional[str], now: Optional[float] = None) -> bool:
        if not token or "." not in token:
            return False
        expires, sig = token.split(".", 1)
        if not expires.isdigit() or int(expires) < (now or time.time()):
            return False
        good = hmac.new(self.key, expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good)

    # ---- brute-force brake: 10 wrong tries in 15 minutes locks that address out --------
    def _locked(self, who: str) -> bool:
        cutoff = time.time() - 900
        tries = [t for t in self._fails.get(who, []) if t > cutoff]
        self._fails[who] = tries
        return len(tries) >= 10

    def check_password(self, who: str, attempt: str) -> Optional[str]:
        """None when correct, otherwise the message to show."""
        if self._locked(who):
            return "Too many wrong tries. Wait 15 minutes and try again."
        if secrets.compare_digest(attempt.encode(), self.password.encode()):
            self._fails.pop(who, None)
            return None
        self._fails.setdefault(who, []).append(time.time())
        return "That password isn't right."

    # ---- middleware -------------------------------------------------------------------
    async def __call__(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith(OPEN_PATHS) or self.valid(request.cookies.get(COOKIE)):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Sign in again to keep using the tracker."}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    def set_cookie(self, resp: Response) -> None:
        resp.set_cookie(COOKIE, self.make_token(), max_age=MAX_AGE, httponly=True,
                        samesite="lax", secure=self.secure_cookie)


def login_page(error: str = "") -> HTMLResponse:
    msg = f'<p class="login-error" role="alert">{error}</p>' if error else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Flight Price Tracker</title><link rel="stylesheet" href="/static/styles.css"></head>
<body class="login-body"><form class="panel login" method="post" action="/login">
<header class="brand"><h1>Flight Price Tracker</h1><p>Enter your password to continue.</p></header>
<label class="field"><span>Password</span><input type="password" name="password" autocomplete="current-password" autofocus required></label>
{msg}<button class="primary" type="submit">Sign in</button></form></body></html>""",
                        status_code=401 if error else 200)
