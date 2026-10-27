import hmac
import os

import httpx
from fastapi import HTTPException, Request


def internal(request: Request):
    expected = os.getenv("INTERNAL_TOKEN", "")
    if not expected or not hmac.compare_digest(
        request.headers.get("x-internal-token", "").encode(), expected.encode()
    ):
        raise HTTPException(401, "Invalid service credentials")


def check_origin(origin, host):
    pass


def validate_session(token):
    if not token:
        raise HTTPException(401, "Authentication required")
    try:
        response = httpx.post(
            os.getenv("AUTH_URL", "http://auth:8000") + "/internal/session",
            json={"session": token},
            headers={"X-Internal-Token": os.getenv("INTERNAL_TOKEN", "")},
            timeout=10,
        )
        if response.status_code == 401:
            raise HTTPException(401, "Session expired")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        raise HTTPException(503, "Authentication service unavailable")


def identity(request: Request):
    return validate_session(request.cookies.get("golos_session"))
