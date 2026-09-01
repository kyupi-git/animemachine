"""Small two-role authentication store for private AnimeMachine deployments."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any


SESSION_SECONDS = 12 * 60 * 60
SESSION_RENEWAL_SECONDS = 15 * 60


@dataclass(frozen=True)
class Session:
    token: str
    csrf: str
    user_id: int
    username: str
    role: str


def _password_hash(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    salt = salt or os.urandom(16)
    value = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$32768$8$1$" + base64.urlsafe_b64encode(salt).decode().rstrip("=") + "$" + base64.urlsafe_b64encode(value).decode().rstrip("=")


def _verify(password: str, encoded: str) -> bool:
    try:
        kind, n, r, p, salt_text, expected_text = encoded.split("$")
        if kind != "scrypt":
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        actual = hashlib.scrypt(password.encode("utf-8"), salt=decode(salt_text), n=int(n), r=int(r), p=int(p), dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, decode(expected_text))
    except (ValueError, TypeError):
        return False


_DUMMY_PASSWORD_HASH = _password_hash("invalid-password-placeholder")


class Store:
    def __init__(self, path: Path, *, enabled: bool, bootstrap_username: str = "", bootstrap_password: str = "") -> None:
        self.path = path
        self.enabled = enabled
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS auth_user(
                id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','user')),
                enabled INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL
              );
              CREATE TABLE IF NOT EXISTS auth_session(
                token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES auth_user(id) ON DELETE CASCADE
              );
            """)
            count = int(db.execute("SELECT COUNT(*) FROM auth_user").fetchone()[0])
        if enabled and count == 0:
            if not bootstrap_username.strip() or not bootstrap_password:
                raise ValueError("authentication is enabled but bootstrap administrator credentials are missing")
            self.create_user(bootstrap_username, bootstrap_password, "admin")

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def create_user(self, username: str, password: str, role: str = "user") -> dict[str, Any]:
        username = username.strip()
        if not (3 <= len(username) <= 64) or not all(ch.isalnum() or ch in "._-" for ch in username):
            raise ValueError("username must be 3-64 letters, digits, dots, underscores or hyphens")
        if role not in {"admin", "user"}:
            raise ValueError("role must be admin or user")
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            cursor = db.execute("INSERT INTO auth_user(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                                (username, _password_hash(password), role, int(time.time())))
        return {"id": int(cursor.lastrowid), "username": username, "role": role, "enabled": True}

    def users(self) -> list[dict[str, Any]]:
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT MIN(id) FROM auth_user WHERE role='admin'").fetchone()
            initial_admin_id = int(row[0]) if row and row[0] is not None else None
            return [{"id": int(row[0]), "username": str(row[1]), "role": str(row[2]), "enabled": bool(row[3]),
                     "initialAdmin": initial_admin_id is not None and int(row[0]) == initial_admin_id}
                    for row in db.execute("SELECT id,username,role,enabled FROM auth_user ORDER BY role,username")]

    def set_enabled(self, user_id: int, enabled: bool, *, actor_id: int) -> bool:
        if user_id == actor_id and not enabled:
            raise ValueError("cannot disable the current account")
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute("UPDATE auth_user SET enabled=? WHERE id=?", (int(enabled), user_id)).rowcount
            if not enabled:
                db.execute("DELETE FROM auth_session WHERE user_id=?", (user_id,))
        return bool(changed)

    def login(self, username: str, password: str) -> Session | None:
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT id,username,password_hash,role,enabled FROM auth_user WHERE username=?", (username.strip(),)).fetchone()
            valid = bool(row and row[4] and _verify(password, str(row[2])))
            if not valid:
                # Equalize the expensive path when the account is absent.
                if not row:
                    _verify(password, _DUMMY_PASSWORD_HASH)
                return None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            now = int(time.time())
            db.execute("DELETE FROM auth_session WHERE expires_at<?", (now,))
            db.execute("INSERT INTO auth_session VALUES(?,?,?,?,?)",
                       (self._digest(token), int(row[0]), csrf, now + SESSION_SECONDS, now))
        return Session(token, csrf, int(row[0]), str(row[1]), str(row[3]))

    def from_cookie(self, header: str) -> Session | None:
        if not self.enabled:
            return Session("", "", 0, "local", "admin")
        cookie = SimpleCookie(); cookie.load(header or "")
        token = cookie.get("anm_session").value if cookie.get("anm_session") else ""
        if not token:
            return None
        now = int(time.time())
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("""SELECT s.csrf_token,s.expires_at,u.id,u.username,u.role,u.enabled
                FROM auth_session s JOIN auth_user u ON u.id=s.user_id WHERE s.token_hash=?""",
                (self._digest(token),)).fetchone()
            if not row or not row[5] or int(row[1]) < now:
                db.execute("DELETE FROM auth_session WHERE token_hash=?", (self._digest(token),))
                return None
            if int(row[1]) < now + SESSION_SECONDS - SESSION_RENEWAL_SECONDS:
                db.execute("UPDATE auth_session SET expires_at=? WHERE token_hash=?", (now + SESSION_SECONDS, self._digest(token)))
        return Session(token, str(row[0]), int(row[2]), str(row[3]), str(row[4]))

    def csrf_valid(self, session: Session, supplied: str) -> bool:
        if not self.enabled:
            return True
        return bool(supplied) and hmac.compare_digest(session.csrf, supplied)

    def logout(self, session: Session) -> None:
        if session.token:
            with contextlib.closing(sqlite3.connect(self.path)) as db, db:
                db.execute("DELETE FROM auth_session WHERE token_hash=?", (self._digest(session.token),))
