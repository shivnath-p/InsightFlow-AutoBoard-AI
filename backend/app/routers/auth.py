import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import config
from ..audit import log_event
from ..database import get_db
from ..models import User
from ..schemas import LoginIn, RegisterIn
from ..security import (account_limiter, client_key, create_token, get_current_user,
                        hash_password, login_limiter, verify_password)

router = APIRouter(prefix="/auth", tags=["Auth"])
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _email_fingerprint(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


@router.post("/register", status_code=201)
def register(p: RegisterIn, request: Request, db: Session = Depends(get_db)):
    if not config.ALLOW_REGISTRATION:
        raise HTTPException(403, "Registration is disabled")
    login_limiter.check("reg:" + client_key(request))
    if db.query(User).filter(User.email == p.email).first():
        raise HTTPException(409, "Email already registered")
    user = User(id=uuid.uuid4().hex, email=p.email, password_hash=hash_password(p.password),
                org_id=uuid.uuid4().hex, role="analyst")
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # two parallel sign-ups with the same email: unique index decides
        db.rollback()
        raise HTTPException(409, "Email already registered")
    log_event(db, user, "user.register", "user", user.id)
    return {"access_token": create_token(user), "token_type": "bearer"}


@router.post("/login")
def login(p: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = p.email.strip().lower()
    ip = client_key(request)
    login_limiter.check("login:" + ip)                          # one IP guessing many accounts
    account_limiter.check("acct:" + _email_fingerprint(email))  # many IPs guessing one account
    user = db.query(User).filter(User.email == email).first()
    ok = verify_password(p.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        log_event(db, None, "user.login_failed",
                  detail={"email_sha": _email_fingerprint(email), "ip": ip})
        raise HTTPException(401, "Invalid email or password")
    log_event(db, user, "user.login", "user", user.id)
    return {"access_token": create_token(user), "token_type": "bearer"}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "org_id": user.org_id, "role": user.role}
