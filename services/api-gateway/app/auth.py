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
import time

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
