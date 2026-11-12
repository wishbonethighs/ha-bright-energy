"""Constants for the Bright Energy integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "bright_energy"

API_BASE: Final = "https://api.glowmarkt.com/api/v0-1/"
# Public application ID used by Hildebrand's own Bright app.
APPLICATION_ID: Final = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"

CONF_VIRTUAL_ENTITY_ID: Final = "virtual_entity_id"
CONF_RECHECK_DAYS: Final = "recheck_days"

DEFAULT_RECHECK_DAYS: Final = 7
MIN_RECHECK_DAYS: Final = 2
MAX_RECHECK_DAYS: Final = 31

# How far back to import on first set-up.
INITIAL_BACKFILL_DAYS: Final = 395

UPDATE_INTERVAL: Final = timedelta(minutes=30)
# Interval used while working through a history backlog.
BACKLOG_INTERVAL: Final = timedelta(minutes=1)

# Glowmarkt limits the range of a single PT30M readings request.
READINGS_CHUNK: Final = timedelta(days=7)
# Most history fetched per resource in one update (keeps each update short).
MAX_SPAN_PER_UPDATE: Final = timedelta(days=63)
# Pause between API calls so we are gentle on Hildebrand's servers.
REQUEST_PAUSE_SECONDS: Final = 0.3
# Re-read the whole recheck window this often; other updates re-read only
# QUICK_RECHECK_DAYS, which keeps routine updates to a couple of small requests.
FULL_RECHECK_INTERVAL: Final = timedelta(hours=6)
QUICK_RECHECK_DAYS: Final = 2
TARIFF_INTERVAL: Final = timedelta(hours=6)
# Ask the DCC for fresh data at most this often per meter.
CATCHUP_INTERVAL: Final = timedelta(minutes=55)

FUEL_ELECTRICITY: Final = "electricity"
FUEL_GAS: Final = "gas"
FUELS: Final = (FUEL_ELECTRICITY, FUEL_GAS)

KIND_CONSUMPTION: Final = "consumption"
KIND_COST: Final = "cost"

# Glowmarkt resource classifiers.
CLASSIFIERS: Final = {
    (FUEL_ELECTRICITY, KIND_CONSUMPTION): "electricity.consumption",
    (FUEL_ELECTRICITY, KIND_COST): "electricity.consumption.cost",
    (FUEL_GAS, KIND_CONSUMPTION): "gas.consumption",
    (FUEL_GAS, KIND_COST): "gas.consumption.cost",
}

SERVICE_REIMPORT: Final = "reimport_history"
ATTR_DAYS: Final = "days"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

STORAGE_VERSION: Final = 1
