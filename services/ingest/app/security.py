"""Device authentication for the ingest endpoint.

The caller is a phone, not a clinician and not another service. Device tokens
are short lived, signed by the enrollment service, and scoped to exactly one
patient code. The scope check is the part that matters: without it a valid
token from one device could submit batches attributed to another patient, and
nothing later in the pipeline would notice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class JwtSettings:
    signing_key: str
    algorithm: str
    audience: str
    issuer: str


def jwt_settings() -> JwtSettings:
    """Read the token configuration per request rather than at import.

    Binding these to module constants at import time makes the module's
    behaviour depend on whether the environment happened to be populated
    before the first import, which is unpredictable under a test runner and
    under any loader that imports before configuration. Reading them here
    costs four dictionary lookups and removes that class of problem.
    """
    return JwtSettings(
        signing_key=os.getenv("JWT_SIGNING_KEY", ""),
        algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        audience=os.getenv("JWT_AUDIENCE", "traycer-ingest"),
        issuer=os.getenv("JWT_ISSUER", "traycer-enrollment"),
    )


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

    settings = jwt_settings()

    if not settings.signing_key:
        # Failing closed. A service that accepts unsigned tokens because a
        # secret was not mounted is worse than one that refuses to serve.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="service is not configured with a signing key",
        )

    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.signing_key,
            algorithms=[settings.algorithm],
            audience=settings.audience,
            issuer=settings.issuer,
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
