"""Authentication for the Pulse API.

Two kinds of caller, two verification paths.

Clinicians arrive with an access token issued by the hospital's SMART on FHIR
authorization server. Those tokens are verified against the issuer's published
JWKS, with the signing keys discovered rather than configured, because a
hospital rotating its keys should not require a Traycer deployment.

Internal services arrive with a platform signed token. Those use the shared
signing key and a service audience.

Both paths fail closed. A missing key, an unreachable JWKS endpoint or an
unknown issuer produces a 401, never a pass through.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("traycer.pulse.security")

FHIR_ISSUER = os.getenv("FHIR_ISSUER", "").rstrip("/")
FHIR_AUDIENCE = os.getenv("FHIR_CLIENT_ID", "traycer-pulse")
JWT_SIGNING_KEY = os.getenv("JWT_SIGNING_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
SERVICE_AUDIENCE = os.getenv("JWT_AUDIENCE", "traycer-pulse-api")
SERVICE_ISSUER = os.getenv("JWT_ISSUER", "traycer-platform")

# Discovery documents change rarely. An hour keeps key rotation responsive
# without hitting the EHR on every request.
DISCOVERY_TTL_SECONDS = 3600

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    subject: str
    kind: str  # "clinician" or "service"
    scopes: frozenset[str]
    fhir_user: str | None = None

    def require_scope(self, *accepted: str) -> None:
        if not self.scopes.intersection(accepted):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"token needs one of: {', '.join(sorted(accepted))}",
            )


class _DiscoveryCache:
    """Caches the SMART and OpenID discovery documents for the issuer."""

    def __init__(self) -> None:
        self._document: dict[str, Any] | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> dict[str, Any]:
        if not FHIR_ISSUER:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="FHIR_ISSUER is not configured",
            )

        now = time.time()
        if self._document and now - self._fetched_at < DISCOVERY_TTL_SECONDS:
            return self._document

        async with self._lock:
            # Another request may have refreshed while this one waited.
            if self._document and time.time() - self._fetched_at < DISCOVERY_TTL_SECONDS:
                return self._document

            document = await _fetch_discovery(FHIR_ISSUER)
            self._document = document
            self._fetched_at = time.time()
            return document


_discovery = _DiscoveryCache()
_jwk_clients: dict[str, jwt.PyJWKClient] = {}


async def _fetch_discovery(issuer: str) -> dict[str, Any]:
    """Read the SMART configuration, falling back to OpenID discovery.

    SMART on FHIR servers publish `.well-known/smart-configuration`. Some
    older installations only publish the OpenID document, and both carry the
    jwks_uri this code needs, so trying the second is worth the extra call
    once an hour.
    """
    candidates = (
        f"{issuer}/.well-known/smart-configuration",
        f"{issuer}/.well-known/openid-configuration",
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in candidates:
            try:
                response = await client.get(url, headers={"accept": "application/json"})
            except httpx.HTTPError as exc:
                logger.warning("discovery request to %s failed: %s", url, type(exc).__name__)
                continue

            if response.status_code == 200:
                return response.json()

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="could not read the SMART configuration from the FHIR issuer",
    )


async def _verify_clinician_token(token: str) -> Principal:
    document = await _discovery.get()
    jwks_uri = document.get("jwks_uri")
    if not jwks_uri:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the FHIR issuer publishes no jwks_uri",
        )

    client = _jwk_clients.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri, cache_keys=True)
        _jwk_clients[jwks_uri] = client

    try:
        # PyJWKClient performs blocking network calls, so it runs off the
        # event loop rather than stalling every other request behind it.
        signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=FHIR_AUDIENCE,
            issuer=document.get("issuer", FHIR_ISSUER),
            options={"require": ["exp", "iat", "aud", "iss"]},
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

    return Principal(
        subject=str(claims.get("sub", "")),
        kind="clinician",
        scopes=scopes,
        fhir_user=claims.get("fhirUser"),
    )


def _verify_service_token(token: str) -> Principal:
    if not JWT_SIGNING_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="service is not configured with a signing key",
        )

    claims = jwt.decode(
        token,
        JWT_SIGNING_KEY,
        algorithms=[JWT_ALGORITHM],
        audience=SERVICE_AUDIENCE,
        issuer=SERVICE_ISSUER,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )

    raw_scopes = claims.get("scope", "")
    scopes = frozenset(raw_scopes.split()) if isinstance(raw_scopes, str) else frozenset()

    return Principal(subject=str(claims["sub"]), kind="service", scopes=scopes)


async def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # The header tells the two paths apart without a trial decode. HS256 is
    # only ever used by internal services; the EHR signs asymmetrically.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed token"
        ) from exc

    algorithm = header.get("alg", "")

    if algorithm.startswith("HS"):
        try:
            return _verify_service_token(token)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="token rejected"
            ) from exc

    if algorithm in {"RS256", "ES256"}:
        return await _verify_clinician_token(token)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"unsupported token algorithm {algorithm or 'none'}",
    )
