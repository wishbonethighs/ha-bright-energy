"""Minimal async client for the Hildebrand Glowmarkt (Bright) API.

Only talks to api.glowmarkt.com. No third-party dependencies beyond aiohttp,
which Home Assistant already provides.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .const import API_BASE, APPLICATION_ID

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)
# Renew the token this many seconds before Glowmarkt says it expires.
TOKEN_RENEW_MARGIN = 3600


class BrightError(Exception):
    """Base error for the Bright client."""


class BrightAuthError(BrightError):
    """Credentials were rejected."""


class BrightConnectionError(BrightError):
    """Could not reach the API, or it returned an unexpected response."""


@dataclass(frozen=True, slots=True)
class Resource:
    """A Glowmarkt resource (one data stream from one meter)."""

    resource_id: str
    classifier: str
    name: str
    base_unit: str | None


@dataclass(frozen=True, slots=True)
class VirtualEntity:
    """A Glowmarkt virtual entity (one property / site)."""

    ve_id: str
    name: str | None
    postal_code: str | None


@dataclass(frozen=True, slots=True)
class Readings:
    """Readings for one resource: list of (UTC period start, value or None)."""

    units: str | None
    values: list[tuple[datetime, float | None]]


@dataclass(frozen=True, slots=True)
class Tariff:
    """Current tariff for one resource, in pence."""

    rate_pence: float | None
    standing_charge_pence: float | None
    name: str | None


def _iso_utc(when: datetime) -> str:
    """Format as the naive UTC ISO string Glowmarkt expects."""
    return when.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _to_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class BrightClient:
    """Async Glowmarkt API client."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        """Initialise the client."""
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_expiry: float = 0
        self._auth_lock = asyncio.Lock()

    async def authenticate(self) -> None:
        """Log in and store a token."""
        try:
            async with self._session.post(
                f"{API_BASE}auth",
                json={"username": self._username, "password": self._password},
                headers={
                    "applicationId": APPLICATION_ID,
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise BrightAuthError("Bright rejected the username or password")
                if resp.status != 200:
                    raise BrightConnectionError(f"Login failed: HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BrightConnectionError(f"Could not reach Bright: {err}") from err

        if not isinstance(body, dict) or not body.get("valid") or not body.get("token"):
            raise BrightAuthError("Bright did not issue a login token")
        self._token = body["token"]
        # "exp" is a Unix timestamp; fall back to 6 days if missing.
        self._token_expiry = _to_float(body.get("exp")) or time.time() + 6 * 86400
        _LOGGER.debug("Authenticated with Bright; token valid until %s", self._token_expiry)

    async def _ensure_token(self) -> None:
        async with self._auth_lock:
            if self._token is None or time.time() > self._token_expiry - TOKEN_RENEW_MARGIN:
                await self.authenticate()

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, _retry: bool = True
    ) -> Any:
        await self._ensure_token()
        assert self._token is not None
        try:
            async with self._session.get(
                f"{API_BASE}{path}",
                params=params,
                headers={
                    "applicationId": APPLICATION_ID,
                    "token": self._token,
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 401 and _retry:
                    # Token may have been revoked early; log in again once.
                    self._token = None
                    return await self._get(path, params, _retry=False)
                if resp.status in (401, 403):
                    raise BrightAuthError("Bright rejected the login token")
                if resp.status != 200:
                    text = (await resp.text())[:200]
                    raise BrightConnectionError(f"GET {path} failed: HTTP {resp.status} {text}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BrightConnectionError(f"GET {path} failed: {err}") from err

    async def get_virtual_entities(self) -> list[VirtualEntity]:
        """List the sites on the account."""
        body = await self._get("virtualentity")
        if not isinstance(body, list):
            raise BrightConnectionError("Unexpected virtualentity response")
        return [
            VirtualEntity(
                ve_id=str(item["veId"]),
                name=item.get("name"),
                postal_code=item.get("postalCode"),
            )
            for item in body
            if isinstance(item, dict) and item.get("veId")
        ]

    async def get_resources(self, ve_id: str) -> list[Resource]:
        """List the data streams for a site."""
        body = await self._get(f"virtualentity/{ve_id}/resources")
        items = body.get("resources") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise BrightConnectionError("Unexpected resources response")
        return [
            Resource(
                resource_id=str(item["resourceId"]),
                classifier=str(item.get("classifier", "")),
                name=str(item.get("name", "")),
                base_unit=item.get("baseUnit"),
            )
            for item in items
            if isinstance(item, dict) and item.get("resourceId")
        ]

    async def catchup(self, resource_id: str) -> None:
        """Ask Glowmarkt to pull the latest data from the DCC."""
        await self._get(f"resource/{resource_id}/catchup")

    async def get_readings(
        self, resource_id: str, start: datetime, end: datetime, period: str = "PT30M"
    ) -> Readings:
        """Fetch readings with period starts in [start, end)."""
        body = await self._get(
            f"resource/{resource_id}/readings",
            {
                "from": _iso_utc(start),
                "to": _iso_utc(end),
                "period": period,
                "offset": 0,
                "function": "sum",
                # Return null rather than 0 where data is missing.
                "nulls": 1,
            },
        )
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise BrightConnectionError("Unexpected readings response")
        values: list[tuple[datetime, float | None]] = []
        for point in data:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            ts = _to_float(point[0])
            if ts is None:
                continue
            when = datetime.fromtimestamp(ts, tz=UTC)
            if start <= when < end:
                values.append((when, _to_float(point[1])))
        return Readings(units=body.get("units"), values=values)

    async def get_tariff(self, resource_id: str) -> Tariff | None:
        """Fetch the current tariff for a resource, if Glowmarkt has one."""
        body = await self._get(f"resource/{resource_id}/tariff")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        first = data[0]
        rates = first.get("currentRates") or {}
        return Tariff(
            rate_pence=_to_float(rates.get("rate")),
            standing_charge_pence=_to_float(rates.get("standingCharge")),
            name=first.get("name"),
        )
