"""Fixtures: a fake Glowmarkt API."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.bright_energy.const import API_BASE

VE_ID = "ABCDEF12-0000-0000-0000-000000000000"
RESOURCES = {
    "res-elec": ("electricity.consumption", "kWh"),
    "res-elec-cost": ("electricity.consumption.cost", "pence"),
    "res-gas": ("gas.consumption", "kWh"),
    "res-gas-cost": ("gas.consumption.cost", "pence"),
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Start the recorder (before hass) and enable custom integrations."""
    return


class FakeGlow:
    """In-memory Glowmarkt."""

    def __init__(self) -> None:
        self.password = "secret"
        # resource id -> {utc half-hour start: value or None}
        self.data: dict[str, dict[datetime, float | None]] = {r: {} for r in RESOURCES}
        self.requests: list[str] = []
        self.fail_readings = False

    def fill(self, resource: str, start: datetime, end: datetime, value: float) -> None:
        t = start
        while t < end:
            self.data[resource][t] = value
            t += timedelta(minutes=30)

    async def handle(self, method, url: URL, data):
        path = url.path.split("/api/v0-1/")[-1]
        self.requests.append(f"{method.upper()} {path}")
        if path == "auth":
            if data and data.get("password") == self.password:
                return AiohttpClientMockResponse(
                    method, url, json={"valid": True, "token": "tok", "exp": 4102444800}
                )
            return AiohttpClientMockResponse(method, url, status=401)
        if path == "virtualentity":
            return AiohttpClientMockResponse(
                method, url, json=[{"veId": VE_ID, "name": "Home", "postalCode": "IG10"}]
            )
        if path == f"virtualentity/{VE_ID}/resources":
            return AiohttpClientMockResponse(
                method,
                url,
                json={
                    "resources": [
                        {"resourceId": rid, "classifier": c, "name": c, "baseUnit": u}
                        for rid, (c, u) in RESOURCES.items()
                    ]
                },
            )
        if m := re.fullmatch(r"resource/([^/]+)/(readings|tariff|catchup)", path):
            rid, action = m.groups()
            if action == "catchup":
                return AiohttpClientMockResponse(method, url, json={"data": []})
            if action == "tariff":
                return AiohttpClientMockResponse(
                    method,
                    url,
                    json={
                        "data": [
                            {
                                "name": "Standard",
                                "currentRates": {"rate": 24.5, "standingCharge": 53.35},
                            }
                        ]
                    },
                )
            if self.fail_readings:
                return AiohttpClientMockResponse(method, url, status=500)
            q = url.query
            start = datetime.fromisoformat(q["from"]).replace(tzinfo=UTC)
            end = datetime.fromisoformat(q["to"]).replace(tzinfo=UTC)
            assert end - start <= timedelta(days=10), "range too long for PT30M"
            points = []
            t = start
            while t <= end:  # Glowmarkt's "to" is inclusive
                points.append([int(t.timestamp()), self.data[rid].get(t)])
                t += timedelta(minutes=30)
            return AiohttpClientMockResponse(
                method, url, json={"units": RESOURCES[rid][1], "data": points}
            )
        return AiohttpClientMockResponse(method, url, status=404)


@pytest.fixture
def glow(aioclient_mock: AiohttpClientMocker) -> FakeGlow:
    """Register the fake API."""
    fake = FakeGlow()
    pattern = re.compile("^" + re.escape(API_BASE))
    aioclient_mock.get(pattern, side_effect=fake.handle)
    aioclient_mock.post(pattern, side_effect=fake.handle)
    return fake


@pytest.fixture(autouse=True)
def no_request_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the politeness pause (it would hang under a frozen clock)."""
    monkeypatch.setattr("custom_components.bright_energy.importer.REQUEST_PAUSE_SECONDS", 0)
