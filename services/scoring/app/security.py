"""Caller authentication for the scoring service.

The scoring service is internal. Its callers are the ingest pipeline and the
pulse API, never a browser and never a phone. Tokens therefore carry a service
subject rather than a patient scope, and the service refuses to start scoring
without a signing key rather than falling open.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SIGNING_KEY = os.getenv("JWT_SIGNING_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "traycer-scoring")
JWT_ISSUER = os.getenv("JWT_ISSUER", "traycer-platform")

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class ServicePrincipal:
    service: str
    scopes: frozenset[str]

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"token is missing the {scope} scope",
            )


async def current_service(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> ServicePrincipal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not JWT_SIGNING_KEY:
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

    raw_scopes = claims.get("scope", "")
    scopes = frozenset(raw_scopes.split()) if isinstance(raw_scopes, str) else frozenset()

    return ServicePrincipal(service=str(claims.get("sub", "")), scopes=scopes)
