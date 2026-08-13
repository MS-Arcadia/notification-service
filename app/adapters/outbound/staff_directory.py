"""Who counts as staff, according to Auth.

A few events are addressed to whoever can act on them rather than to the person who acted — a
game waiting for review, a role somebody asked for. This service owns "who gets told", so it
asks Auth for the SUPPORT and ADMIN ids rather than having Catalog carry a notion of staffing.

The same route and the same self-signed SERVICE token festival-service uses for the platform-wide
festival audience; see `festival-service/app/adapters/outbound/auth_profile.py`.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt

logger = logging.getLogger(__name__)

# A user has exactly one role, so these are two disjoint queries rather than a filter.
_STAFF_ROLES = ("SUPPORT", "ADMIN")


class HttpStaffDirectory:
    """Reads auth-profile-service's internal `/v1/admin/users/ids` route."""

    def __init__(
        self,
        *,
        base_url: str,
        jwt_secret: str,
        jwt_algorithm: str = "HS256",
        jwt_issuer: str = "",
        jwt_audience: str = "",
        service_name: str = "notification-service",
        ttl_seconds: float = 60.0,
        timeout: float = 3.0,
    ) -> None:
        self._jwt_secret = jwt_secret
        self._jwt_algorithm = jwt_algorithm
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience
        self._service_name = service_name
        self._ttl = ttl_seconds
        self._cached: list[str] = []
        self._fetched_at = 0.0
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _service_token(self) -> str:
        """Mint a short-lived token for this service.

        `role: SUPPORT` is the narrowest role the target route accepts. Symmetric signing is
        only safe because the platform's JWT secret is shared; any service that can verify a
        token can also mint one.
        """
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "sub": self._service_name,
            "role": "SUPPORT",
            "typ": "access",
            "scopes": ["users:read"],
            "iat": now,
            "exp": now + timedelta(minutes=2),
        }
        if self._jwt_issuer:
            claims["iss"] = self._jwt_issuer
        if self._jwt_audience:
            claims["aud"] = self._jwt_audience
        return jwt.encode(claims, self._jwt_secret, algorithm=self._jwt_algorithm)

    async def staff_ids(self) -> list[str]:
        """Every active SUPPORT and ADMIN id.

        Cached for a minute: the set changes when somebody is promoted, and without it every
        submitted game would cost two round trips.

        An unreachable directory returns whatever was cached — an empty list on a cold start —
        rather than raising. The alternative is a consumer that stalls on an event nobody is
        waiting for, which costs more than a missed notification.
        """
        now = time.monotonic()
        if self._cached and now - self._fetched_at < self._ttl:
            return self._cached

        found: list[str] = []
        token = self._service_token()
        for role in _STAFF_ROLES:
            try:
                response = await self._client.get(
                    "/v1/admin/users/ids",
                    params={"status": "ACTIVE", "role": role},
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                found.extend(str(user_id) for user_id in response.json())
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "could not read the %s directory; notifying %d cached recipient(s)",
                    role,
                    len(self._cached),
                    extra={"error": str(exc)},
                )
                return self._cached

        self._cached = sorted(set(found))
        self._fetched_at = now
        return self._cached
