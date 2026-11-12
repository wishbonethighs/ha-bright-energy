"""Sensors for Bright Energy.

These are for dashboards and alerts. The Energy dashboard should use the
"Bright ... consumption/cost" statistics instead, which place late-arriving
readings at the right time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BrightConfigEntry, BrightCoordinator, FuelData

PARALLEL_UPDATES = 0
GBP = "GBP"


def _pounds(pence: float | None) -> float | None:
    return None if pence is None else round(pence / 100, 4)


@dataclass(frozen=True, kw_only=True)
class BrightSensorDescription(SensorEntityDescription):
    """Describes a Bright sensor."""

    value_fn: Callable[[FuelData], float | datetime | None]


DESCRIPTIONS: tuple[BrightSensorDescription, ...] = (
    BrightSensorDescription(
        key="used_today",
        translation_key="used_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda d: d.used_today,
    ),
    BrightSensorDescription(
        key="used_yesterday",
        translation_key="used_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda d: d.used_yesterday,
    ),
    BrightSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=GBP,
        suggested_display_precision=2,
        value_fn=lambda d: d.cost_today,
    ),
    BrightSensorDescription(
        key="cost_yesterday",
        translation_key="cost_yesterday",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=GBP,
        suggested_display_precision=2,
        value_fn=lambda d: d.cost_yesterday,
    ),
    BrightSensorDescription(
        key="unit_rate",
        translation_key="unit_rate",
        native_unit_of_measurement=f"{GBP}/{UnitOfEnergy.KILO_WATT_HOUR}",
        suggested_display_precision=4,
        value_fn=lambda d: _pounds(d.tariff.rate_pence if d.tariff else None),
    ),
    BrightSensorDescription(
        key="standing_charge",
        translation_key="standing_charge",
        native_unit_of_measurement=f"{GBP}/d",
        suggested_display_precision=4,
        value_fn=lambda d: _pounds(d.tariff.standing_charge_pence if d.tariff else None),
    ),
    BrightSensorDescription(
        key="latest_reading",
        translation_key="latest_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.latest_reading,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        BrightSensor(coordinator, fuel, description)
        for fuel in coordinator.fuels
        for description in DESCRIPTIONS
    )


class BrightSensor(CoordinatorEntity[BrightCoordinator], SensorEntity):
    """A Bright sensor for one fuel."""

    _attr_has_entity_name = True
    entity_description: BrightSensorDescription

    def __init__(
        self,
        coordinator: BrightCoordinator,
        fuel: str,
        description: BrightSensorDescription,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._fuel = fuel
        self._attr_unique_id = f"{coordinator.ve_id}_{fuel}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.ve_id}_{fuel}")},
            name=f"Bright {fuel}",
            manufacturer="Hildebrand",
            model="Smart meter via DCC",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Available once data has been fetched."""
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> float | datetime | None:
        """Return the value."""
        data = self.coordinator.data
        if data is None or (fuel_data := data.fuels.get(self._fuel)) is None:
            return None
        return self.entity_description.value_fn(fuel_data)
