"""Statistics import tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.bright_energy.const import CONF_VIRTUAL_ENTITY_ID, DOMAIN
from custom_components.bright_energy.importer import (
    cumulative,
    merge_hours,
    statistic_id,
    to_hourly,
)

from .conftest import VE_ID

NOW = datetime(2026, 11, 12, 14, 10, tzinfo=UTC)
ELEC = statistic_id(VE_ID, "electricity", "consumption")
ELEC_COST = statistic_id(VE_ID, "electricity", "cost")
GAS = statistic_id(VE_ID, "gas", "consumption")


def test_helpers() -> None:
    h = datetime(2026, 1, 1, tzinfo=UTC)
    hourly = to_hourly(
        [(h, 1.0), (h + timedelta(minutes=30), 2.0), (h + timedelta(hours=1), None)], 0.5
    )
    assert hourly == {h: 1.5}
    merged = merge_hours(
        {h: 0.0, h + timedelta(hours=2): 3.0}, {h: 1.5, h + timedelta(hours=1): 2.0}
    )
    assert merged == {h: 1.5, h + timedelta(hours=1): 2.0, h + timedelta(hours=2): 3.0}
    stats = cumulative(10, merged)
    assert [s["sum"] for s in stats] == [11.5, 13.5, 16.5]


async def _stats(hass: HomeAssistant, stat: str) -> list[dict]:
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        NOW - timedelta(days=400),
        None,
        {stat},
        "hour",
        None,
        {"sum", "state"},
    )
    return rows.get(stat, [])


async def _run_until_idle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, max_rounds=20):
    for _ in range(max_rounds):
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
        if coordinator.data is not None and not coordinator.data.backlog:
            return coordinator
        freezer.tick(timedelta(minutes=1, seconds=5))
        async_fire_time_changed(hass)
    raise AssertionError("backlog never cleared")


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VE_ID,
        data={
            CONF_USERNAME: "me@example.com",
            CONF_PASSWORD: "secret",
            CONF_VIRTUAL_ENTITY_ID: VE_ID,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_backfill_and_late_data(
    hass: HomeAssistant, glow, entry, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(NOW)
    start = datetime(2026, 11, 1, tzinfo=UTC)
    glow.fill("res-elec", start, datetime(2026, 11, 12, 10, tzinfo=UTC), 0.5)
    glow.fill("res-elec-cost", start, datetime(2026, 11, 12, 10, tzinfo=UTC), 12.0)
    glow.fill("res-gas", start, datetime(2026, 11, 12, 8, tzinfo=UTC), 1.0)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    coordinator = await _run_until_idle(hass, freezer)

    rows = await _stats(hass, ELEC)
    hours = (datetime(2026, 11, 12, 10, tzinfo=UTC) - start) // timedelta(hours=1)
    assert len(rows) == hours
    assert rows[-1]["sum"] == pytest.approx(hours * 1.0)
    assert rows[0]["start"] == start.timestamp()

    cost = await _stats(hass, ELEC_COST)
    assert cost[-1]["sum"] == pytest.approx(hours * 0.24)  # pence → pounds

    elec = coordinator.data.fuels["electricity"]
    assert elec.used_today == pytest.approx(10.0)  # 00:00–10:00 UTC (= local in Nov)
    assert elec.used_yesterday == pytest.approx(24.0)
    assert elec.latest_reading == datetime(2026, 11, 12, 10, tzinfo=UTC)
    assert elec.tariff.rate_pence == 24.5

    state = hass.states.get("sensor.bright_electricity_used_today")
    assert state is not None and float(state.state) == pytest.approx(10.0)
    assert hass.states.get("sensor.bright_electricity_unit_rate").state == "0.245"

    # Late data arrives, one past hour is corrected, and one past hour comes
    # back empty (an API hiccup) — it must keep its stored value.
    glow.fill(
        "res-elec",
        datetime(2026, 11, 12, 10, tzinfo=UTC),
        datetime(2026, 11, 12, 13, tzinfo=UTC),
        0.5,
    )
    glow.data["res-elec"][datetime(2026, 11, 10, 3, tzinfo=UTC)] = 2.0
    glow.data["res-elec"][datetime(2026, 11, 11, 5, tzinfo=UTC)] = None
    glow.data["res-elec"][datetime(2026, 11, 11, 5, 30, tzinfo=UTC)] = None

    # A routine update re-reads only the last two days: new data and the
    # empty hour are handled, the older correction waits for the full re-check.
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    rows = await _stats(hass, ELEC)
    assert rows[-1]["sum"] == pytest.approx(hours + 3)

    for _ in range(12):  # six hours of 30-minute updates
        freezer.tick(timedelta(minutes=31))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    rows = await _stats(hass, ELEC)
    by_start = {datetime.fromtimestamp(r["start"], tz=UTC): r for r in rows}
    assert len(rows) == hours + 3
    # +3 hours of new data, +1.5 kWh from the correction, nothing lost.
    assert rows[-1]["sum"] == pytest.approx(hours + 3 + 1.5)
    corrected = by_start[datetime(2026, 11, 10, 3, tzinfo=UTC)]
    assert corrected["state"] == pytest.approx(2.5)
    kept = by_start[datetime(2026, 11, 11, 5, tzinfo=UTC)]
    assert kept["state"] == pytest.approx(1.0)
    # Sums never go backwards.
    sums = [r["sum"] for r in rows]
    assert sums == sorted(sums)

    gas = await _stats(hass, GAS)
    assert gas[-1]["sum"] == pytest.approx(
        ((datetime(2026, 11, 12, 8, tzinfo=UTC) - start) // timedelta(hours=1)) * 2.0
    )


async def test_api_outage_keeps_history(
    hass: HomeAssistant, glow, entry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    start = datetime(2026, 11, 10, tzinfo=UTC)
    glow.fill("res-elec", start, datetime(2026, 11, 12, tzinfo=UTC), 0.5)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = await _run_until_idle(hass, freezer)
    before = await _stats(hass, ELEC)

    glow.fail_readings = True
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator.last_update_success
    assert await _stats(hass, ELEC) == before

    # Data all null (e.g. Bright reset) must not erase anything either.
    glow.fail_readings = False
    for key in glow.data["res-elec"]:
        glow.data["res-elec"][key] = None
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.last_update_success
    after = await _stats(hass, ELEC)
    assert [r["sum"] for r in after] == [r["sum"] for r in before]


async def test_bad_password_starts_reauth(hass: HomeAssistant, glow, entry) -> None:
    glow.password = "changed"
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)
