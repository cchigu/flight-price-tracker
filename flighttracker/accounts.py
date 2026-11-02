"""Pages for signing in and out, your own account, and (for the owner) managing logins."""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from .auth import Gate, esc, login_page, page

NOTICES = {
    "password": "Password changed.",
    "email": "Email saved.",
    "added": "Login added. Send them the link, their name and the password below.",
    "reset": "Password reset. Send them the new one below.",
    "removed": "Login removed. They've been signed out.",
}


async def _form(request: Request) -> dict[str, str]:
    data = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in data.items()}


def _client(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _notice(key: str) -> str:
    text = NOTICES.get(key, "")
    return f'<p class="notice" role="status">{esc(text)}</p>' if text else ""


def _error(text: str) -> str:
    return f'<p class="form-error" role="alert">{esc(text)}</p>' if text else ""


def public_url(request: Request) -> str:
    """The address people outside use (the Tailscale/HTTPS name, not the internal 127.0.0.1)."""
    configured = os.environ.get("FT_PUBLIC_URL", "").rstrip("/")
    if configured:
        return configured
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if host and not host.startswith(("127.", "localhost", "[::1]")):
        proto = request.headers.get("x-forwarded-proto", "https")
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")


def suggest_password() -> str:
    """Easy to read out or type on a phone: three short words' worth of letters and digits."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def register(app: FastAPI, gate: Gate) -> None:
    users = gate.users

    # ---- sign in / out --------------------------------------------------------------------
    @app.get("/login")
    def login_form(request: Request):
        if gate.open or gate.current(request):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post("/login")
    async def login(request: Request):
        f = await _form(request)
        user, error = gate.try_login(_client(request), f.get("name", ""), f.get("password", ""))
        if user is None:
            return login_page(error, f.get("name", ""))
        resp = RedirectResponse("/", status_code=303)
        gate.sign_in(resp, user)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        gate.sign_out(resp)
        return resp

    @app.get("/api/me")
    def me(request: Request):
        u = request.state.user
        if u is None:
            return {"signed_in": False, "open": gate.open}
        return {"signed_in": True, "name": u.name, "is_owner": u.is_owner, "email": u.email}

    # ---- your account ---------------------------------------------------------------------
    def account_page(request: Request, error: str = "", done: str = ""):
        u = request.state.user
        body = f"""<h1>Your account</h1>
<p class="lede">Signed in as <strong>{esc(u.name)}</strong>{' (owner)' if u.is_owner else ''}.</p>
{_notice(done)}{_error(error)}
<section class="card">
  <h2>Change password</h2>
  <form method="post" action="/account/password" class="stack">
    <label class="field"><span>Current password</span><input type="password" name="current" autocomplete="current-password" required></label>
    <label class="field"><span>New password</span><input type="password" name="new" autocomplete="new-password" minlength="6" required></label>
    <button class="primary" type="submit">Change password</button>
  </form>
</section>
<section class="card">
  <h2>Email for price alerts</h2>
  <p class="hint">Price-drop alerts will go here once alerts are switched on.</p>
  <form method="post" action="/account/email" class="stack">
    <label class="field"><span>Email</span><input type="email" name="email" value="{esc(u.email)}" autocomplete="email"></label>
    <button class="primary" type="submit">Save email</button>
  </form>
</section>"""
        return page("Your account", body, 400 if error else 200, u)

    @app.get("/account")
    def account(request: Request):
        if request.state.user is None:
            return RedirectResponse("/", status_code=303)
        return account_page(request, done=request.query_params.get("done", ""))

    @app.post("/account/password")
    async def change_password(request: Request):
        u = request.state.user
        if u is None:
            return RedirectResponse("/", status_code=303)
        f = await _form(request)
        if users.authenticate(u.name, f.get("current", "")) is None:
            return account_page(request, "Your current password isn't right.")
        try:
            users.set_password(u.id, f.get("new", ""))
        except ValueError as e:
            return account_page(request, str(e))
        resp = RedirectResponse("/account?done=password", status_code=303)
        gate.sign_in(resp, users.get(u.id))  # stay signed in here; other devices are signed out
        return resp

    @app.post("/account/email")
    async def change_email(request: Request):
        u = request.state.user
        if u is None:
            return RedirectResponse("/", status_code=303)
        try:
            users.set_email(u.id, (await _form(request)).get("email", ""))
        except ValueError as e:
            return account_page(request, str(e))
        return RedirectResponse("/account?done=email", status_code=303)

    # ---- people (owner only) ---------------------------------------------------------------
    def people_page(request: Request, error: str = "", done: str = "", shown: tuple[str, str] | None = None,
                    form_name: str = ""):
        me = request.state.user
        base = public_url(request)
        share = ""
        if shown:
            name, pw = shown
            share = f"""<div class="share" aria-live="polite">
  <p>Send this to <strong>{esc(name)}</strong>:</p>
  <pre class="share-text"><span data-origin>{esc(base)}</span>\nName: {esc(name)}\nPassword: {esc(pw)}</pre>
  <p class="hint">They can change the password under Your account after signing in. This is the only time it's shown.</p>
</div>
<script>document.querySelectorAll("[data-origin]").forEach(function (el) {{ el.textContent = location.origin; }});</script>"""
        rows = []
        for u in users.all():
            d = datetime.fromtimestamp(u.created)
            since = f"{d:%b} {d.day}, {d.year}"  # %-d isn't supported on Windows
            if u.is_owner:
                actions = '<span class="hint">You</span>' if u.id == me.id else ""
            else:
                actions = f"""<form method="post" action="/people/{u.id}/reset"><button class="link" type="submit">Reset password</button></form>
<form method="post" action="/people/{u.id}/remove" onsubmit="return confirm('Remove {esc(u.name)}? They will be signed out and lose their saved searches.')"><button class="link danger" type="submit">Remove</button></form>"""
            rows.append(f"""<li class="person"><div><span><strong>{esc(u.name)}</strong>{' <span class="tag">Owner</span>' if u.is_owner else ''}</span>
<span class="hint">{esc(u.email) or 'No email yet'}, added {esc(since)}</span></div><div class="person-actions">{actions}</div></li>""")
        body = f"""<h1>People</h1>
<p class="lede">Everyone here can sign in and run their own searches. Searches and alerts are private to each person.</p>
{_notice(done)}{_error(error)}{share}
<section class="card"><h2>Logins</h2><ul class="people">{''.join(rows)}</ul></section>
<section class="card">
  <h2>Add a login</h2>
  <form method="post" action="/people" class="stack">
    <label class="field"><span>Name they'll sign in with</span><input name="name" value="{esc(form_name)}" autocapitalize="none" spellcheck="false" placeholder="e.g. alex" required></label>
    <label class="field"><span>Starting password</span><input name="password" value="{esc(suggest_password())}" minlength="6" required></label>
    <button class="primary" type="submit">Add login</button>
  </form>
</section>"""
        return page("People", body, 400 if error else 200, me)

    def owner_only(request: Request):
        u = request.state.user
        return u is not None and u.is_owner

    @app.get("/people")
    def people(request: Request):
        if not owner_only(request):
            return RedirectResponse("/", status_code=303)
        return people_page(request, done=request.query_params.get("done", ""))

    @app.post("/people")
    async def add_person(request: Request):
        if not owner_only(request):
            return RedirectResponse("/", status_code=303)
        f = await _form(request)
        try:
            u = users.add(f.get("name", ""), f.get("password", ""))
        except ValueError as e:
            return people_page(request, str(e), form_name=f.get("name", ""))
        return people_page(request, done="added", shown=(u.name, f.get("password", "")))

    @app.post("/people/{user_id}/reset")
    def reset_person(request: Request, user_id: int):
        if not owner_only(request):
            return RedirectResponse("/", status_code=303)
        u = users.get(user_id)
        if u is None or u.is_owner:
            return RedirectResponse("/people", status_code=303)
        pw = suggest_password()
        users.set_password(u.id, pw)
        return people_page(request, done="reset", shown=(u.name, pw))

    @app.post("/people/{user_id}/remove")
    def remove_person(request: Request, user_id: int):
        if not owner_only(request):
            return RedirectResponse("/", status_code=303)
        try:
            users.remove(user_id)
        except ValueError as e:
            return people_page(request, str(e))
        app.state.runner.forget_user(user_id)
        return RedirectResponse("/people?done=removed", status_code=303)
