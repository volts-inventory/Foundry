#!/usr/bin/env python3
"""Single-account session login for the Workshop.

The Workshop drives real model spend and executes the code those models write,
and it shipped with no login: FOUNDRY_WORKSHOP_TOKEN gated the state-changing
routes, but only if you set it, and the UI only sends the matching header when
the token is in the page URL — so using it meant entering at ``/?token=…`` and
every plain visit 403ing on its first action.

This is the ordinary alternative: an account, a sign-in page, a signed session
cookie. Seeded as ``admin`` / ``changeme`` with a forced change at first
sign-in, the same shape most self-hosted apps use.

ON by default, unlike a token you have to remember to set. What is behind the
door is a build button that spends money and runs generated code, so an
unauthenticated default was the wrong way round. FOUNDRY_AUTH=off restores the
old behaviour for a localhost-only run where the login is just friction.

FOUNDRY_WORKSHOP_TOKEN still works and is unchanged — it is the header a script
or a curl can use, and it now satisfies the session check too, so automation
does not need a cookie jar.

Built for what it guards: PBKDF2 rather than anything reversible, per-client
rate limiting on failures, constant-time comparison, and a password change that
invalidates every existing session.

stdlib only — hashlib, hmac and secrets cover all of it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

# Beside the Workshop's other persisted state, so it survives a restart and
# follows the deployment if that directory is relocated or mounted.
STATE_DIR = Path(
    os.environ.get("FOUNDRY_AUTH_DIR")
    or os.environ.get("WORKSHOP_STATE_DIR")
    or os.environ.get("FOUNDRY_STATE_DIR")
    or ".run/web/state"
)
AUTH_FILE = STATE_DIR / "auth.json"

# PBKDF2-SHA256. The cost is paid once per sign-in, where a third of a second is
# unnoticeable, and it is the whole defence if this file ever leaks.
ITERATIONS = 600_000
SALT_BYTES = 16

SESSION_MAX_AGE_S = int(os.environ.get("FOUNDRY_SESSION_MAX_AGE_S") or 7 * 24 * 3600)
COOKIE_NAME = "foundry_session"

# The password a fresh install starts with when LAUNCHER_PASSWORD isn't set.
# Signing in with it lands you on a forced change, exactly like Budget.
SEED_PASSWORD = "changeme"

# Failed sign-ins allowed per client address inside the window before it's told
# to wait. Small because there is one account and one person using it.
MAX_FAILURES = 8
FAILURE_WINDOW_S = 300

_lock = threading.RLock()
_failures: dict[str, deque[float]] = {}


def enabled() -> bool:
    """Whether the Workshop asks for a sign-in.

    ON unless explicitly turned off. Behind this door is a button that spends
    real money and executes generated code, so defaulting to open would be the
    wrong way round — the opposite of the launcher's default, where the worst a
    stranger can do is toggle a container.
    """
    flag = (os.environ.get("FOUNDRY_AUTH") or "").strip().lower()
    return flag not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------
# stored state
# --------------------------------------------------------------------------

def _read() -> dict[str, Any]:
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _write(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def ensure_seeded() -> None:
    """Create the account on first boot.

    LAUNCHER_PASSWORD seeds a real password; without it the account starts on
    `changeme` and is flagged so the first sign-in has to replace it. The
    session-signing secret is generated here and persisted, so restarts don't
    log you out — it lives in the state volume beside the hash.
    """
    with _lock:
        data = _read()
        if data.get("hash"):
            return
        seed = (os.environ.get("FOUNDRY_AUTH_PASSWORD") or "").strip()
        salt = secrets.token_bytes(SALT_BYTES)
        _write(
            {
                "username": (os.environ.get("FOUNDRY_ADMIN_USERNAME") or "admin").strip() or "admin",
                "salt": salt.hex(),
                "iterations": ITERATIONS,
                "hash": _hash(seed or SEED_PASSWORD, salt),
                # Only a seeded default has to be changed; an explicitly
                # configured password is the operator's choice already.
                "must_change": not seed,
                "session_secret": secrets.token_hex(32),
                "token_version": 1,
                "created": int(time.time()),
            }
        )


def username() -> str:
    return str(_read().get("username") or "admin")


def must_change() -> bool:
    return bool(_read().get("must_change"))


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def check_password(candidate: str) -> bool:
    data = _read()
    salt = bytes.fromhex(str(data.get("salt") or ""))
    stored = str(data.get("hash") or "")
    if not salt or not stored:
        return False
    computed = _hash(candidate, salt, int(data.get("iterations") or ITERATIONS))
    # Constant time: a length-or-content-dependent comparison here leaks the
    # hash a byte at a time to anyone who can measure the response.
    return hmac.compare_digest(computed, stored)


def set_password(new_password: str) -> tuple[bool, str]:
    # Order matters: the seeded default is shorter than the minimum, so checking
    # length first would answer "too short" to someone retyping `changeme` and
    # leave this branch unreachable.
    if new_password == SEED_PASSWORD:
        return False, "That's the default password — pick a different one."
    if len(new_password) < 10:
        return False, "Use at least 10 characters."
    with _lock:
        data = _read()
        salt = secrets.token_bytes(SALT_BYTES)
        data.update(
            {
                "salt": salt.hex(),
                "iterations": ITERATIONS,
                "hash": _hash(new_password, salt),
                "must_change": False,
                # Every existing session dies with the old password, which is
                # the point of changing it after someone may have seen it.
                "token_version": int(data.get("token_version") or 1) + 1,
                "changed": int(time.time()),
            }
        )
        _write(data)
    return True, ""


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def _secret() -> bytes:
    return str(_read().get("session_secret") or "").encode("utf-8")


def issue_session() -> str:
    data = _read()
    payload = f"{int(time.time())}.{int(data.get('token_version') or 1)}"
    signature = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_session(cookie_value: str) -> bool:
    if not cookie_value:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return False
    issued_raw, version_raw, signature = parts
    payload = f"{issued_raw}.{version_raw}"
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    try:
        issued, version = int(issued_raw), int(version_raw)
    except ValueError:
        return False
    if version != int(_read().get("token_version") or 1):
        return False  # password changed since this was issued
    return 0 <= (time.time() - issued) <= SESSION_MAX_AGE_S


def cookie_header(value: str, max_age: int | None = None) -> str:
    """Set-Cookie for a session.

    HttpOnly so a script can't read it, SameSite=Strict so a browser won't send
    it on a request another site initiated — which, with the Origin check the
    Workshop already performs on mutations, is most of the CSRF story. Secure
    only when told: on plain HTTP a Secure cookie would never come back at all,
    so setting it without TLS in front locks you out.
    """
    secure = (os.environ.get("FOUNDRY_COOKIE_SECURE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    age = SESSION_MAX_AGE_S if max_age is None else max_age
    bits = [
        f"{COOKIE_NAME}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={age}",
    ]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)


def session_from_cookies(header: str | None) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return ""


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------

def note_failure(client: str) -> None:
    with _lock:
        bucket = _failures.setdefault(client, deque())
        bucket.append(time.time())


def throttled(client: str) -> int:
    """Seconds the caller should wait, or 0 if it may try now."""
    cutoff = time.time() - FAILURE_WINDOW_S
    with _lock:
        bucket = _failures.setdefault(client, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) < MAX_FAILURES:
            return 0
        return max(1, int(bucket[0] + FAILURE_WINDOW_S - time.time()))


def clear_failures(client: str) -> None:
    with _lock:
        _failures.pop(client, None)
