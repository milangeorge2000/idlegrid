"""Accounts, sessions, API keys, node join tokens and node secrets."""

import hashlib
import hmac
import secrets

from .db import DB, new_id, now

STARTING_CREDITS = 5.00  # USD of free credits for new accounts


def hash_secret(secret: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"


def check_secret(secret: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_secret(secret, salt), stored)


def fast_hash(token: str) -> str:
    """For high-entropy random tokens a plain SHA-256 is enough (no brute force risk)."""
    return hashlib.sha256(token.encode()).hexdigest()


def register_user(db: DB, email: str, password: str) -> dict:
    email = email.strip().lower()
    if "@" not in email or len(password) < 8:
        raise ValueError("Enter a valid email and a password of at least 8 characters.")
    if db.one("SELECT id FROM users WHERE email = ?", email):
        raise ValueError("An account with this email already exists.")
    uid = new_id("u")
    db.insert("users", id=uid, email=email, pw_hash=hash_secret(password),
              credits=STARTING_CREDITS, created_at=now())
    db.insert("ledger", ts=now(), user_id=uid, amount=STARTING_CREDITS, kind="signup_credit", ref=None)
    return db.one("SELECT * FROM users WHERE id = ?", uid)


def login(db: DB, email: str, password: str) -> str:
    user = db.one("SELECT * FROM users WHERE email = ?", email.strip().lower())
    if not user or not check_secret(password, user["pw_hash"]):
        raise ValueError("Wrong email or password.")
    token = "s_" + secrets.token_urlsafe(32)
    db.insert("sessions", token=fast_hash(token), user_id=user["id"], created_at=now())
    return token


def create_api_key(db: DB, user_id: str, label: str = "default") -> str:
    key = "ig_" + secrets.token_urlsafe(32)
    db.insert("api_keys", key=fast_hash(key), user_id=user_id, label=label, created_at=now())
    return key


def user_from_bearer(db: DB, token: str | None) -> dict | None:
    """Accepts a dashboard session token or an API key."""
    if not token:
        return None
    h = fast_hash(token)
    row = db.one("SELECT user_id FROM sessions WHERE token = ?", h) or \
        db.one("SELECT user_id FROM api_keys WHERE key = ?", h)
    return db.one("SELECT * FROM users WHERE id = ?", row["user_id"]) if row else None


def create_join_token(db: DB, user_id: str) -> str:
    token = "join_" + secrets.token_urlsafe(18)
    db.insert("join_tokens", token=fast_hash(token), user_id=user_id, created_at=now())
    return token


def consume_join_token(db: DB, token: str) -> str | None:
    """Returns the owner user_id; join tokens are single-use and expire after 24h."""
    with db.tx():
        row = db.one("SELECT * FROM join_tokens WHERE token = ?", fast_hash(token))
        if not row or row["used"] or now() - row["created_at"] > 86400:
            return None
        db.run("UPDATE join_tokens SET used = 1 WHERE token = ?", fast_hash(token))
        return row["user_id"]
