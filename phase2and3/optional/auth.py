"""
Authentication
--------------
JWT-based session authentication for the Phase 3 API.
Tokens expire after SESSION_DURATION_HOURS (default 1 hour).

Flow:
  POST /auth/login  →  receive access_token
  All other endpoints require: Authorization: Bearer <token>
"""
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

from config.settings import JWT_SECRET_KEY, SESSION_DURATION_HOURS

security = HTTPBearer()


def create_token(username: str) -> str:
    """Issue a JWT token valid for SESSION_DURATION_HOURS."""
    expire = datetime.utcnow() + timedelta(hours=SESSION_DURATION_HOURS)
    return jwt.encode(
        {"sub": username, "exp": expire},
        JWT_SECRET_KEY,
        algorithm="HS256"
    )


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    Validate the Bearer token on every protected endpoint.
    Returns the username on success, raises HTTPException on failure.
    """
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET_KEY,
            algorithms=["HS256"]
        )
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token.")
