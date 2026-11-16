"""
auth.py
-------
Teacher authentication for SmartQuiz.

Password hashing uses only the Python standard library (hashlib +
hmac + secrets) — PBKDF2-HMAC-SHA256 with a per-password random salt,
which is a legitimate, NIST/OWASP-recognized password hashing scheme.
This deliberately avoids adding passlib/bcrypt/argon2 as a new
dependency ("do not introduce unnecessary dependencies") when the
stdlib already provides everything needed for this.

Session identity (i.e. "is this browser currently logged in") is
handled by Starlette's SessionMiddleware, which signs a cookie using
itsdangerous — itsdangerous is already an existing transitive
dependency of FastAPI/Starlette, so wiring this up added no new
third-party package either. See app.py for the middleware setup; this
module only concerns itself with verifying credentials and gating
access, not how the session cookie itself is signed/stored.

The password hash lives in the database (AppSetting table, see
models.py) rather than an environment variable, specifically so it can
be CHANGED at runtime from the Settings page without editing .env or
restarting the server. TEACHER_INITIAL_PASSWORD (an env var) is only
ever read ONCE, to bootstrap the very first password if none exists yet
— after that first run it's irrelevant, since the real credential lives
in the DB from then on.
"""

import hashlib
import hmac
import os
import secrets

from fastapi import Request
from sqlalchemy.orm import Session

from models import AppSetting

# OWASP's current minimum recommendation for PBKDF2-HMAC-SHA256.
PBKDF2_ITERATIONS = 260_000
PASSWORD_HASH_KEY = "teacher_password_hash"
SESSION_KEY = "teacher_authenticated"


def hash_password(password: str) -> str:
    """Self-contained string: pbkdf2_sha256$iterations$salt_hex$hash_hex.
    Storing the salt and iteration count alongside the hash (rather than
    in separate config) is what lets verify_password work without extra
    state, and lets the iteration count be raised later without
    invalidating passwords hashed under an older count."""
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time comparison (hmac.compare_digest) so response timing
    can't leak how much of the hash matched. Never raises on a malformed
    stored value — just fails closed (returns False)."""
    try:
        algo, iterations_str, salt, hash_hex = stored_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations_str)
        )
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def get_password_hash(db: Session) -> str | None:
    row = db.query(AppSetting).filter(AppSetting.key == PASSWORD_HASH_KEY).first()
    return row.value if row else None


def set_password_hash(db: Session, password_hash: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == PASSWORD_HASH_KEY).first()
    if row:
        row.value = password_hash
    else:
        db.add(AppSetting(key=PASSWORD_HASH_KEY, value=password_hash))
    db.commit()


def ensure_teacher_password_bootstrapped(db: Session) -> None:
    """Called once at startup (see app.py's lifespan). If a password
    hash already exists, this is a no-op — the DB is always the source
    of truth once it has one. Otherwise: use TEACHER_INITIAL_PASSWORD
    from .env if set, hash and store it; if that's ALSO not set,
    generate a random password, store its hash, and print the plaintext
    to the server console exactly once (never over HTTP, never in a
    template, never logged anywhere else) so the teacher running the
    server can retrieve it and log in. This guarantees the app never
    ships with a guessable or hardcoded default credential, and never
    silently locks the teacher out either."""
    if get_password_hash(db):
        return

    initial = os.environ.get("TEACHER_INITIAL_PASSWORD")
    if initial:
        set_password_hash(db, hash_password(initial))
        print("[SmartQuiz] Teacher password bootstrapped from TEACHER_INITIAL_PASSWORD (.env).")
        return

    generated = secrets.token_urlsafe(9)  # ~12 URL-safe characters
    set_password_hash(db, hash_password(generated))
    print("=" * 64)
    print("[SmartQuiz] No teacher password was configured.")
    print(f"[SmartQuiz] Temporary password: {generated}")
    print("[SmartQuiz] Log in with this password, then change it under Settings.")
    print("=" * 64)


def is_teacher_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def log_in_teacher(request: Request) -> None:
    request.session[SESSION_KEY] = True


def log_out_teacher(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


class TeacherAuthRequired(Exception):
    """Raised by the /admin auth middleware when a request to a
    protected route arrives without a valid teacher session. Handled by
    an exception handler registered in app.py that converts this into a
    303 redirect to /login — using an exception + handler here (rather
    than returning a response directly from middleware in every case)
    keeps the "not authenticated" signal explicit and centrally handled
    in one place."""
