"""Update coordinator for Bright Energy."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import BrightAuthError, BrightClient, BrightConnectionError, Resource, Tariff
from .const import (
    BACKLOG_INTERVAL,
    CATCHUP_INTERVAL,
    CLASSIFIERS,
    CONF_RECHECK_DAYS,
    CONF_VIRTUAL_ENTITY_ID,
    DEFAULT_RECHECK_DAYS,
    DOMAIN,
    FUELS,
    FULL_RECHECK_INTERVAL,
    INITIAL_BACKFILL_DAYS,
    KIND_CONSUMPTION,
    KIND_COST,
    QUICK_RECHECK_DAYS,
    STORAGE_VERSION,
    TARIFF_INTERVAL,
    UPDATE_INTERVAL,
)
from .importer import build_metadata, floor_hour, import_stream, statistic_id

_LOGGER = logging.getLogger(__name__)

type BrightConfigEntry = ConfigEntry[BrightCoordinator]


@dataclass(slots=True)
class FuelData:
    """Latest known figures for one fuel."""

    used_today: float | None = None
    used_yesterday: float | None = None
    cost_today: float | None = None
    cost_yesterday: float | None = None
    latest_reading: datetime | None = None
    tariff: Tariff | None = None


@dataclass(slots=True)
class BrightData:
    """Data shared with entities."""

    fuels: dict[str, FuelData] = field(default_factory=dict)
    backlog: bool = False


def _day_total(hourly: dict[datetime, float], day: date) -> float | None:
    values = [v for h, v in hourly.items() if dt_util.as_local(h).date() == day]
    return round(sum(values), 4) if values else None


class BrightCoordinator(DataUpdateCoordinator[BrightData]):
    """Fetch Bright data and keep long-term statistics up to date."""

    config_entry: BrightConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BrightConfigEntry, client: BrightClient) -> None:
        """Initialise."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.ve_id: str = entry.data[CONF_VIRTUAL_ENTITY_ID]
        self.resources: dict[tuple[str, str], Resource] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._cursors: dict[str, str] = {}
        self._latest: dict[str, str] = {}
        self._last_catchup: dict[str, datetime] = {}
        self._last_full_recheck: datetime | None = None
        self._last_tariff: datetime | None = None
        self._fuels: dict[str, FuelData] = {}

    @property
    def recheck_days(self) -> int:
        """Days of recent history re-read on every update."""
        return int(self.config_entry.options.get(CONF_RECHECK_DAYS, DEFAULT_RECHECK_DAYS))

    async def async_discover(self) -> None:
        """Load saved progress and find this site's meters."""
        stored = await self._store.async_load() or {}
        self._cursors = dict(stored.get("cursors", {}))
        self._latest = dict(stored.get("latest", {}))

        found = await self.client.get_resources(self.ve_id)
        by_classifier = {r.classifier: r for r in found}
        self.resources = {
            key: by_classifier[classifier]
            for key, classifier in CLASSIFIERS.items()
            if classifier in by_classifier
        }
        if not any(k == KIND_CONSUMPTION for _, k in self.resources):
            raise BrightConnectionError(
                "No electricity or gas consumption found for this Bright site"
            )
        for fuel in self.fuels:
            fuel_data = self._fuels.setdefault(fuel, FuelData())
            if fuel in self._latest:
                fuel_data.latest_reading = dt_util.parse_datetime(self._latest[fuel])

    @property
    def fuels(self) -> list[str]:
        """Fuels this site has consumption data for."""
        return [f for f in FUELS if (f, KIND_CONSUMPTION) in self.resources]

    def stat_id(self, fuel: str, kind: str) -> str:
        """Statistic ID for a stream."""
        return statistic_id(self.ve_id, fuel, kind)

    def stat_name(self, fuel: str, kind: str) -> str:
        """Statistic display name for a stream."""
        label = "consumption" if kind == KIND_CONSUMPTION else "cost"
        return f"Bright {fuel} {label}"

    async def async_reset_history(self, days: int) -> None:
        """Re-read the last ``days`` days on the next update."""
        start = (dt_util.utcnow() - timedelta(days=days)).isoformat()
        for fuel, kind in self.resources:
            self._cursors[self.stat_id(fuel, kind)] = start
        await self._save()
        await self.async_request_refresh()

    async def _save(self) -> None:
        await self._store.async_save({"cursors": self._cursors, "latest": self._latest})

    def _fetch_start(self, stat: str, now: datetime, days: int) -> datetime:
        recheck_from = floor_hour(now - timedelta(days=days))
        cursor = dt_util.parse_datetime(self._cursors[stat]) if stat in self._cursors else None
        if cursor is None:
            return floor_hour(now - timedelta(days=INITIAL_BACKFILL_DAYS))
        return min(recheck_from, floor_hour(cursor))

    async def _maybe_catchup(self, resource: Resource, now: datetime) -> None:
        last = self._last_catchup.get(resource.resource_id)
        if last and now - last < CATCHUP_INTERVAL:
            return
        self._last_catchup[resource.resource_id] = now
        try:
            await self.client.catchup(resource.resource_id)
        except BrightConnectionError as err:
            # Catch-up is best effort; readings may still be available.
            _LOGGER.debug("Catch-up request failed for %s: %s", resource.classifier, err)

    async def _async_update_data(self) -> BrightData:
        try:
            return await self._update()
        except BrightAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BrightConnectionError as err:
            raise UpdateFailed(str(err)) from err

    async def _update(self) -> BrightData:
        now = dt_util.utcnow()
        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)
        backlog = False
        # Re-read the full window a few times a day; otherwise just recent days.
        full = (
            self._last_full_recheck is None
            or now - self._last_full_recheck >= FULL_RECHECK_INTERVAL
        )
        days = self.recheck_days if full else min(QUICK_RECHECK_DAYS, self.recheck_days)
        tariff_due = self._last_tariff is None or now - self._last_tariff >= TARIFF_INTERVAL

        for fuel in self.fuels:
            fuel_data = self._fuels.setdefault(fuel, FuelData())
            await self._maybe_catchup(self.resources[(fuel, KIND_CONSUMPTION)], now)

            for kind in (KIND_CONSUMPTION, KIND_COST):
                resource = self.resources.get((fuel, kind))
                if resource is None:
                    continue
                stat = self.stat_id(fuel, kind)
                result = await import_stream(
                    self.hass,
                    self.client,
                    resource,
                    build_metadata(stat, self.stat_name(fuel, kind), kind),
                    kind,
                    self._fetch_start(stat, now, days),
                    now,
                )
                self._cursors[stat] = result.fetched_until.isoformat()
                backlog |= result.backlog
                if result.backlog:
                    continue  # still catching up on old history
                day_today = _day_total(result.hourly, today)
                day_before = _day_total(result.hourly, yesterday)
                if kind == KIND_CONSUMPTION:
                    fuel_data.used_today, fuel_data.used_yesterday = day_today, day_before
                    if result.latest_data:
                        # The end of the newest hour that has data.
                        data_end = result.latest_data + timedelta(hours=1)
                        if fuel_data.latest_reading is None or data_end > fuel_data.latest_reading:
                            fuel_data.latest_reading = data_end
                            self._latest[fuel] = data_end.isoformat()
                else:
                    fuel_data.cost_today, fuel_data.cost_yesterday = day_today, day_before

            if tariff_due:
                try:
                    fuel_data.tariff = await self.client.get_tariff(
                        self.resources[(fuel, KIND_CONSUMPTION)].resource_id
                    )
                except BrightConnectionError as err:
                    _LOGGER.debug("Could not read %s tariff: %s", fuel, err)

        if tariff_due:
            self._last_tariff = now
        if full and not backlog:
            self._last_full_recheck = now
        await self._save()
        self.update_interval = BACKLOG_INTERVAL if backlog else UPDATE_INTERVAL
        if backlog:
            _LOGGER.debug("Importing history; continuing in %s", BACKLOG_INTERVAL)
        return BrightData(fuels=dict(self._fuels), backlog=backlog)
