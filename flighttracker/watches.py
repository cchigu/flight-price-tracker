"""Saved price watches, their price history, and the rule for when to send an alert.

Two kinds of watch:
  fixed     one departure date (and one return date): a single price check, every 15 minutes
  flexible  a window of departure days and trip lengths: a full scan, every 6 hours

When to email (decide_alert):
  - the first time the price is at or below the trigger
  - again only if it then falls a further REALERT_DROP or more below the last emailed price
  - if the price goes back above the trigger, the next dip below it counts as "first" again
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .engine import FlexibleSearch
from .models import Passengers, SearchOptions

FIXED_EVERY_S = 15 * 60
FLEX_EVERY_S = 6 * 3600
REALERT_DROP = 25
MAX_FIXED = 10      # per person
MAX_FLEXIBLE = 3    # per person; each one is ~100-800 price checks


# ---- the alert rule ---------------------------------------------------------------------------
def decide_alert(price: Optional[int], trigger: Optional[int], below: bool,
                 last_alert_price: Optional[int], step: int = REALERT_DROP) -> tuple[bool, bool, Optional[int]]:
    """Returns (send_email, below_now, last_alert_price_now)."""
    if price is None or trigger is None:
        return False, below, last_alert_price
    if price > trigger:
        return False, False, None           # back above: re-arm
    if not below or last_alert_price is None or price <= last_alert_price - step:
        return True, True, price
    return False, True, last_alert_price


# ---- turning saved settings into a search -----------------------------------------------------
def kind_of(params: dict) -> str:
    one_way = params.get("trip_type") == "one-way"
    same_day = params["depart_from"] == params["depart_to"]
    same_len = one_way or int(params.get("min_days", 0)) == int(params.get("max_days", 0))
    return "fixed" if same_day and same_len else "flexible"


def request_from_params(params: dict, today: Optional[date] = None) -> FlexibleSearch:
    """Build the search, moving a window that has started into the future (it can't search the past)."""
    today = today or date.today()
    p = params.get("passengers") or {}
    opts = SearchOptions(
        trip_type=params.get("trip_type", "round-trip"),
        cabin=params.get("cabin", "economy"),
        passengers=Passengers(int(p.get("adults", 1)), int(p.get("children", 0)),
                              int(p.get("infants_in_seat", 0)), int(p.get("infants_on_lap", 0))),
        currency=params.get("currency", "USD").upper(),
        stops=params.get("stops", "any"),
        max_stops=None if params.get("stops") == "direct" else params.get("max_stops"),
    )
    start = max(date.fromisoformat(params["depart_from"]), today)
    end = date.fromisoformat(params["depart_to"])
    return FlexibleSearch(params["origin"], params["destination"], start, end,
                          int(params.get("min_days", 7)), int(params.get("max_days", 7)),
                          opts, int(params.get("top_n", 10)), params.get("rank_by", "price"))


def ended(params: dict, today: Optional[date] = None) -> bool:
    return date.fromisoformat(params["depart_to"]) < (today or date.today())


def describe(params: dict) -> str:
    """Short human label, e.g. 'FLL to DEL, leaving Jan 5 to Feb 15, 20-25 days'."""
    def d(s):
        x = date.fromisoformat(s)
        return f"{x:%b} {x.day}"
    route = f"{params['origin']} to {params['destination']}"
    one_way = params.get("trip_type") == "one-way"
    if kind_of(params) == "fixed":
        if one_way:
            return f"{route}, one way on {d(params['depart_from'])}"
        ret = date.fromisoformat(params["depart_from"]) + timedelta(days=int(params["min_days"]))
        return f"{route}, {d(params['depart_from'])} to {d(ret.isoformat())}"
    when = f"leaving {d(params['depart_from'])} to {d(params['depart_to'])}"
    if one_way:
        return f"{route}, one way, {when}"
    lo, hi = int(params["min_days"]), int(params["max_days"])
    return f"{route}, {when}, {lo}–{hi} days" if lo != hi else f"{route}, {when}, {lo} days"


# ---- storage ------------------------------------------------------------------------------------
@dataclass
class Watch:
    id: int
    user_id: int
    kind: str
    params: dict
    trigger: Optional[int]
    active: bool
    created: float
    next_check: float
    last_checked: Optional[float]
    last_price: Optional[int]
    last_result: Optional[dict]
    below: bool
    last_alert_price: Optional[int]
    last_error: str
    ended: bool

    @property
    def label(self) -> str:
        return describe(self.params)

    @property
    def currency(self) -> str:
        return self.params.get("currency", "USD")


class WatchStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(data_dir / "tracker.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                params TEXT NOT NULL,
                trigger_price INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                ended INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                next_check REAL NOT NULL,
                last_checked REAL,
                last_price INTEGER,
                last_result TEXT,
                below INTEGER NOT NULL DEFAULT 0,
                last_alert_price INTEGER,
                last_error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS price_history (
                watch_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                price INTEGER);
            CREATE INDEX IF NOT EXISTS price_history_watch ON price_history (watch_id, ts);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self._db.commit()

    # ---- settings (email account, public address) --------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._db.commit()

    # ---- watches -------------------------------------------------------------------------------
    @staticmethod
    def _watch(r) -> Watch:
        return Watch(r["id"], r["user_id"], r["kind"], json.loads(r["params"]), r["trigger_price"],
                     bool(r["active"]), r["created"], r["next_check"], r["last_checked"], r["last_price"],
                     json.loads(r["last_result"]) if r["last_result"] else None, bool(r["below"]),
                     r["last_alert_price"], r["last_error"], bool(r["ended"]))

    def add(self, user_id: int, params: dict, trigger: Optional[int], now: Optional[float] = None) -> Watch:
        kind = kind_of(params)
        mine = [w for w in self.for_user(user_id) if not w.ended]
        limit = MAX_FIXED if kind == "fixed" else MAX_FLEXIBLE
        if sum(1 for w in mine if w.kind == kind) >= limit:
            what = "fixed-date" if kind == "fixed" else "flexible-date"
            raise ValueError(f"You can have up to {limit} {what} watches. Delete one to add another.")
        same = next((w for w in mine if w.params == params), None)
        if same is not None:
            raise ValueError("You're already watching this search. Change its alert price on the Watches page.")
        now = now or time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO watches (user_id, kind, params, trigger_price, created, next_check) VALUES (?,?,?,?,?,?)",
                (user_id, kind, json.dumps(params, sort_keys=True), trigger, now, now))
            self._db.commit()
        return self.get(cur.lastrowid)

    def get(self, watch_id: int) -> Optional[Watch]:
        with self._lock:
            r = self._db.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        return self._watch(r) if r else None

    def for_user(self, user_id: int) -> list[Watch]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM watches WHERE user_id=? ORDER BY ended, created DESC",
                                    (user_id,)).fetchall()
        return [self._watch(r) for r in rows]

    def due(self, now: float) -> list[Watch]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM watches WHERE active=1 AND ended=0 AND next_check<=? "
                                    "ORDER BY kind='flexible', next_check", (now,)).fetchall()
        return [self._watch(r) for r in rows]

    def update(self, watch_id: int, **fields) -> None:
        cols = {"trigger": "trigger_price"}
        conv = {"last_result": lambda v: json.dumps(v) if v is not None else None,
                "params": lambda v: json.dumps(v, sort_keys=True)}
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{cols.get(k, k)}=?")
            vals.append(conv[k](v) if k in conv else v)
        with self._lock:
            self._db.execute(f"UPDATE watches SET {', '.join(sets)} WHERE id=?", (*vals, watch_id))
            self._db.commit()

    def delete(self, watch_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            self._db.execute("DELETE FROM price_history WHERE watch_id=?", (watch_id,))
            self._db.commit()

    def delete_for_user(self, user_id: int) -> None:
        for w in self.for_user(user_id):
            self.delete(w.id)

    # ---- history -------------------------------------------------------------------------------
    def add_history(self, watch_id: int, price: Optional[int], ts: Optional[float] = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO price_history (watch_id, ts, price) VALUES (?,?,?)",
                             (watch_id, ts or time.time(), price))
            self._db.commit()

    def history(self, watch_id: int, since: Optional[float] = None) -> list[tuple[float, Optional[int]]]:
        since = since if since is not None else time.time() - 30 * 86400
        with self._lock:
            rows = self._db.execute("SELECT ts, price FROM price_history WHERE watch_id=? AND ts>=? ORDER BY ts",
                                    (watch_id, since)).fetchall()
        return [(r["ts"], r["price"]) for r in rows]
