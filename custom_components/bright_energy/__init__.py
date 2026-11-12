"""Bright Energy: Hildebrand Glow / Bright smart meter data for Home Assistant."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import BrightAuthError, BrightClient, BrightConnectionError
from .const import ATTR_CONFIG_ENTRY_ID, ATTR_DAYS, DOMAIN, SERVICE_REIMPORT
from .coordinator import BrightConfigEntry, BrightCoordinator

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REIMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_DAYS): vol.All(vol.Coerce(int), vol.Range(min=1, max=400)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the re-import service."""

    async def reimport(call: ServiceCall) -> None:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entries = [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if entry_id in (None, e.entry_id)
        ]
        if not entries:
            raise ServiceValidationError("No loaded Bright Energy entry found")
        for entry in entries:
            await entry.runtime_data.async_reset_history(call.data[ATTR_DAYS])

    hass.services.async_register(DOMAIN, SERVICE_REIMPORT, reimport, schema=REIMPORT_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BrightConfigEntry) -> bool:
    """Set up Bright Energy from a config entry."""
    client = BrightClient(
        async_get_clientsession(hass), entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    coordinator = BrightCoordinator(hass, entry, client)
    try:
        await coordinator.async_discover()
    except BrightAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except BrightConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The first update may import months of history, so run it in the background
    # rather than holding up Home Assistant's start-up.
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN}_first_refresh")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BrightConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
