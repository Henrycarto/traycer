"""Device JWT verification.

The voice sample is sent by the patient's phone, so the caller is a device
rather than a clinician. Device tokens are short lived, signed by the platform
enrollment service, and scoped to a single patient code. The scope check
matters: without it a valid token from one device could submit samples
attributed to another patient.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SIGNING_KEY = os.getenv("JWT_SIGNING_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "traycer-ingest")
JWT_ISSUER = os.getenv("JWT_ISSUER", "traycer-enrollment")

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class DevicePrincipal:
    patient_code: str
    device_id: str
    scopes: frozenset[str]

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"token is missing the {scope} scope",
            )


async def current_device(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> DevicePrincipal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not JWT_SIGNING_KEY:
        # Failing closed. A service that accepts unsigned tokens because the
        # key was not mounted is worse than a service that will not start.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="service is not configured with a signing key",
        )

    try:
        claims = jwt.decode(
            credentials.credentials,
            JWT_SIGNING_KEY,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token rejected"
        ) from exc

    patient_code = claims.get("patient_code")
    if not isinstance(patient_code, str) or not patient_code:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token carries no patient scope",
        )

    raw_scopes = claims.get("scope", "")
    scopes = frozenset(raw_scopes.split()) if isinstance(raw_scopes, str) else frozenset()

    return DevicePrincipal(
        patient_code=patient_code,
        device_id=str(claims.get("sub", "")),
        scopes=scopes,
    )
