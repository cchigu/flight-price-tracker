"""People who can sign in, stored in a small SQLite file.

The owner account ("admin" unless FT_OWNER says otherwise) is created the first time the app starts,
using FT_PASSWORD. After that the owner adds, resets and removes everyone else from the People page.
Passwords are stored only as salted scrypt hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 6


@dataclass(frozen=True)
class User:
    id: int
    name: str
    is_owner: bool
    email: str
    session_version: int
    created: float
    daily_summary: bool = False
    last_summary: str = ""


def _hash(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    good = _hash(password, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(good, digest_hex)


def clean_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("Names are 2–32 characters: letters, numbers, dots, dashes or underscores.")
    return name


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Passwords need at least {MIN_PASSWORD} characters.")


class UserStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "tracker.db"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_owner INTEGER NOT NULL DEFAULT 0,
            email TEXT NOT NULL DEFAULT '',
            session_version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL)""")
        # Columns added after the first release.
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(users)")}
        if "daily_summary" not in have:
            self._db.execute("ALTER TABLE users ADD COLUMN daily_summary INTEGER NOT NULL DEFAULT 0")
        if "last_summary" not in have:
            self._db.execute("ALTER TABLE users ADD COLUMN last_summary TEXT NOT NULL DEFAULT ''")
        self._db.commit()
        self.secret = self._load_secret(data_dir / "secret.key")

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        if path.exists():
            return bytes.fromhex(path.read_text().strip())
        key = secrets.token_bytes(32)
        path.write_text(key.hex())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key

    # ---- reading ----------------------------------------------------------------------
    @staticmethod
    def _user(row) -> Optional[User]:
        if row is None:
            return None
        return User(row["id"], row["name"], bool(row["is_owner"]), row["email"], row["session_version"],
                    row["created"], bool(row["daily_summary"]), row["last_summary"])

    def get(self, user_id: int) -> Optional[User]:
        with self._lock:
            return self._user(self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def by_name(self, name: str) -> Optional[User]:
        with self._lock:
            return self._user(self._db.execute("SELECT * FROM users WHERE name=?", ((name or "").strip().lower(),)).fetchone())

    def all(self) -> list[User]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM users ORDER BY is_owner DESC, name").fetchall()
        return [self._user(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # ---- signing in ---------------------------------------------------------------------
    def authenticate(self, name: str, password: str) -> Optional[User]:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE name=?", ((name or "").strip().lower(),)).fetchone()
        if row is None:
            _verify(password or "", _hash("timing-equalizer"))  # same work whether or not the name exists
            return None
        return self._user(row) if _verify(password or "", row["password_hash"]) else None

    # ---- changes ------------------------------------------------------------------------
    def ensure_owner(self, name: str, password: str) -> None:
        """Create the owner on first start. Does nothing once anyone exists."""
        if self.count() == 0 and password:
            self.add(name, password, is_owner=True)

    def add(self, name: str, password: str, is_owner: bool = False, email: str = "") -> User:
        name = clean_name(name)
        check_password(password)
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO users (name, password_hash, is_owner, email, created) VALUES (?,?,?,?,?)",
                    (name, _hash(password), int(is_owner), email.strip(), time.time()))
                self._db.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"There's already a login called {name}.")
        return self.get(cur.lastrowid)

    def set_password(self, user_id: int, password: str) -> None:
        """New password; signs that person out everywhere."""
        check_password(password)
        with self._lock:
            self._db.execute("UPDATE users SET password_hash=?, session_version=session_version+1 WHERE id=?",
                             (_hash(password), user_id))
            self._db.commit()

    def set_email(self, user_id: int, email: str) -> None:
        email = (email or "").strip()
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise ValueError("That doesn't look like an email address.")
        with self._lock:
            self._db.execute("UPDATE users SET email=? WHERE id=?", (email, user_id))
            self._db.commit()

    def set_daily_summary(self, user_id: int, on: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE users SET daily_summary=? WHERE id=?", (int(on), user_id))
            self._db.commit()

    def mark_summary_sent(self, user_id: int, day: str) -> None:
        with self._lock:
            self._db.execute("UPDATE users SET last_summary=? WHERE id=?", (day, user_id))
            self._db.commit()

    def remove(self, user_id: int) -> None:
        u = self.get(user_id)
        if u is None:
            return
        if u.is_owner:
            raise ValueError("The owner login can't be removed.")
        with self._lock:
            self._db.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._db.commit()
