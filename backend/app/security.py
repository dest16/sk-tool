import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from passlib.context import CryptContext

from .models import Session


pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def new_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(days: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(tzinfo=None)


def session_for(token: str, user_id: int, days: int) -> tuple[Session, str]:
    csrf = new_token(24)
    return (
        Session(
            token_hash=token_hash(token),
            csrf_token=csrf,
            user_id=user_id,
            expires_at=session_expiry(days),
        ),
        csrf,
    )


