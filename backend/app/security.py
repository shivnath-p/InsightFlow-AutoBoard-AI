"""Authentication (JWT), password hashing (PBKDF2, stdlib) and a small rate limiter."""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from . import config
from .database import get_db
from .models import User

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, config.PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        config.PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_b64, hash_b64 = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64),
                                     int(iters))
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except Exception:
        return False


def create_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=config.ACCESS_TOKEN_MINUTES)
    return jwt.encode({"sub": user.id, "iat": now, "exp": exp}, config.SECRET_KEY,
                      algorithm="HS256")


def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                     db: Session = Depends(get_db)) -> User:
    """The org is always read from the DB row, never from anything the client can send."""
    unauthorized = HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if not creds:
        raise unauthorized
    try:
        # a token without exp/sub is rejected instead of living forever
        payload = jwt.decode(creds.credentials, config.SECRET_KEY, algorithms=["HS256"],
                             options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        raise unauthorized
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise unauthorized
    user = db.get(User, sub)
    if not user:
        raise unauthorized
    return user


class RateLimiter:
    """In-memory sliding window. Single process only: use Redis when you scale out."""

    _SWEEP_EVERY = 1024  # checks between sweeps of keys that went quiet

    def __init__(self, limit: int, window_seconds: int = 60):
        self.limit, self.window = limit, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()
        self._calls = 0

    def _sweep(self, now: float) -> None:
        """Drop idle keys, otherwise an attacker can grow the dict without bound."""
        for key in [k for k, q in self.hits.items() if not q or now - q[-1] > self.window]:
            del self.hits[key]

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self.lock:
            self._calls += 1
            if self._calls % self._SWEEP_EVERY == 0:
                self._sweep(now)
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise HTTPException(429, "Too many requests, slow down",
                                    headers={"Retry-After": str(self.window)})
            q.append(now)

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


ask_limiter = RateLimiter(config.ASK_RATE_LIMIT_PER_MIN)
login_limiter = RateLimiter(config.LOGIN_RATE_LIMIT_PER_MIN)      # per IP
account_limiter = RateLimiter(config.LOGIN_RATE_LIMIT_PER_MIN)    # per target account
heavy_limiter = RateLimiter(config.HEAVY_RATE_LIMIT_PER_MIN)      # uploads, auto dashboards, exports


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"
