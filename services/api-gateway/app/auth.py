"""Per-user credentials for the gateway: bcrypt hashing + HS256 JWTs.

This is a PARALLEL credential to the X-API-Key shared secret (see main.py):
a protected route accepts either one. What this is NOT (Phase 1 limits,
stated plainly): no refresh tokens, no roles/permissions, no password
reset, no email verification, no token revocation -- a token is good for
its full 24h TTL no matter what happens to the account afterwards.

FAIL CLOSED, same as API_GATEWAY_SHARED_SECRET: JWT_SECRET_KEY is
mandatory. The module raises at import time when it is missing or blank,
so uvicorn exits and the container never starts -- there is deliberately
no configuration in which signing/verification silently disables itself.
Passwords and hashes are never logged or echoed anywhere in this module
or its callers.
"""

import os
import re
import time
from collections import deque
from dataclasses import dataclass

import bcrypt
import jwt


def _load_jwt_secret() -> str:
    """Return the configured JWT signing key, or refuse to start.

    Called at import time on purpose: raising here is what makes the
    gateway fail closed, because uvicorn (and therefore the container)
    never gets the chance to serve a single request with token
    verification disabled.
    """
    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError(
            "JWT_SECRET_KEY is missing or empty -- refusing to start. The "
            "api-gateway fails closed by design: starting without a signing "
            "key would silently issue and accept unverifiable tokens. Set "
            "it to a long random value (e.g. `openssl rand -hex 32`) in any "
            "real deployment, or to the non-secret local-dev placeholder "
            "from docker-compose.yml for local development."
        )
    return secret


# Read once, at import, for the same reason main.py caches the shared
# secret: a process's environment cannot change after startup, and one
# value is validated at boot and used for the process's whole life.
JWT_SECRET_KEY = _load_jwt_secret()
JWT_ALGORITHM = "HS256"  # pinned; decode below never accepts anything else
ACCESS_TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24h -- no refresh tokens (Phase 1)

# A real bcrypt hash of a throwaway string, generated at import so that a
# login for an unknown email still performs one full bcrypt verification
# (see main.py login) and response timing cannot reveal whether an account
# exists. Not a credential; nothing can log in with it.
DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"timing-equaliser-not-a-real-password", bcrypt.gensalt()
).decode("utf-8")


def hash_password(password: str) -> str:
    """bcrypt hash with a fresh random salt. Never log the result or the input."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-outcome verification; returns False instead of raising."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash, or a >72-byte input on bcrypt versions that
        # reject it instead of truncating: treat as a mismatch.
        return False


def create_token(user_id: str, email: str) -> str:
    """HS256 token: sub (user id), email, iat, exp (24h)."""
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "email": email, "iat": now, "exp": now + ACCESS_TOKEN_TTL_SECONDS},
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )


def decode_token(token: str) -> dict:
    """Strictly decode a token, or raise jwt.InvalidTokenError.

    The algorithm is pinned via the explicit `algorithms` list (an `alg: none`
    or RS256 token is rejected before any signature check), `exp` is required
    (an absent exp is rejected, an elapsed one raises ExpiredSignatureError),
    and the signature must match this deployment's JWT_SECRET_KEY.
    """
    return jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp"]},
    )


# ---------------------------------------------------------------------------
# Phase 2: principals + route allowlist (pure authorize function)
#
# Two kinds of caller exist:
# - "service" principal: presented the X-API-Key. A trusted internal
#   caller (CD smoke test, scripts). Full access.
# - "jwt" principal: a human customer holding a per-user bearer token.
#   May call ONLY the allowlisted customer routes; everything else is
#   403 {"detail":"forbidden"}. Ownership (is this MY order?) is checked
#   separately in main.py, which returns 404 with the same body as a
#   real not-found so order existence is not leaked.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    kind: str  # "jwt" or "service"
    user_id: str | None = None  # set for JWT principals (the token's sub)


_JWT_ALLOWLIST: tuple = (
    ("GET", re.compile(r"^/products$")),
    ("GET", re.compile(r"^/products/[^/]+$")),
    ("POST", re.compile(r"^/orders$")),
    ("GET", re.compile(r"^/orders/[^/]+$")),
    ("GET", re.compile(r"^/orders/[^/]+/events$")),
    ("POST", re.compile(r"^/orders/[^/]+/cancel$")),
    ("GET", re.compile(r"^/notifications/[^/]+$")),
    ("GET", re.compile(r"^/my/orders$")),
)


def authorize(principal: Principal, method: str, path: str) -> bool:
    """Pure authorization check: unit-testable without HTTP."""
    if principal.kind == "service":
        return True
    if principal.kind != "jwt":
        return False
    return any(
        method == allowed_method and allowed_path.match(path) is not None
        for allowed_method, allowed_path in _JWT_ALLOWLIST
    )


_ORDER_ID_RE = re.compile(r"^/orders/(?P<order_id>[^/]+)(?:/events|/cancel)?$")
_NOTIFICATION_ID_RE = re.compile(r"^/notifications/(?P<order_id>[^/]+)$")


def extract_order_id(path: str) -> str | None:
    """Return the order id in an ownership-scoped path, else None."""
    match = _ORDER_ID_RE.match(path) or _NOTIFICATION_ID_RE.match(path)
    return match.group("order_id") if match else None


# ---------------------------------------------------------------------------
# Phase 2: login/register throttling (in-memory sliding window)
#
# Limits are per gateway INSTANCE and held in process memory: fine for a
# demo, NOT for multi-replica production (each replica counts
# separately). Documented in CLAUDE.md / README.
# ---------------------------------------------------------------------------
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
REGISTER_MAX_ATTEMPTS_PER_IP = 20

_login_failures_by_ip: dict = {}
_login_failures_by_email: dict = {}
_register_attempts_by_ip: dict = {}


def _prune(bucket: deque, now: float, window: float) -> None:
    while bucket and bucket[0] <= now - window:
        bucket.popleft()


def _hit(bucket_map: dict, key: str, now: float, window: float) -> int:
    bucket = bucket_map.setdefault(key, deque())
    _prune(bucket, now, window)
    bucket.append(now)
    return len(bucket)


def record_login_failure(client_ip: str, email: str) -> None:
    now = time.monotonic()
    _hit(_login_failures_by_ip, client_ip, now, LOGIN_WINDOW_SECONDS)
    _hit(_login_failures_by_email, email, now, LOGIN_WINDOW_SECONDS)


def login_throttled(client_ip: str, email: str) -> bool:
    now = time.monotonic()
    for bucket_map, key in ((_login_failures_by_ip, client_ip), (_login_failures_by_email, email)):
        bucket = bucket_map.get(key)
        if bucket is None:
            continue
        _prune(bucket, now, LOGIN_WINDOW_SECONDS)
        if len(bucket) >= LOGIN_MAX_FAILURES:
            return True
    return False


def clear_login_failures(client_ip: str, email: str) -> None:
    _login_failures_by_ip.pop(client_ip, None)
    _login_failures_by_email.pop(email, None)


def register_throttled(client_ip: str) -> bool:
    count = _hit(_register_attempts_by_ip, client_ip, time.monotonic(), LOGIN_WINDOW_SECONDS)
    return count >= REGISTER_MAX_ATTEMPTS_PER_IP


def reset_throttle_state() -> None:
    """Test-only hook: empty every throttle bucket."""
    _login_failures_by_ip.clear()
    _login_failures_by_email.clear()
    _register_attempts_by_ip.clear()


# ---------------------------------------------------------------------------
# Phase 2: Idempotency-Key namespacing for JWT principals
#
# order-service keys idempotency globally: two users sending the same key
# would be handed the SAME order. Namespace JWT keys as
# "<user_id>:<client key>" so keys never collide across users. Client
# keys over 128 chars are rejected with 422 upstream of here.
# ---------------------------------------------------------------------------
MAX_IDEMPOTENCY_KEY_CHARS = 128


def namespace_idempotency_key(user_id: str, client_key: str) -> str:
    return f"{user_id}:{client_key}"
