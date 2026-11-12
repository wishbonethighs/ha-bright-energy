"""Import Bright half-hourly readings into Home Assistant long-term statistics.

Bright data arrives late (30 minutes to a day or more). Rather than exposing a
"live" sensor, each reading is written as an hourly external statistic at the
hour it was actually used. Every update re-reads a recent window (default seven
days), so readings that arrive late, or are corrected, land in the right hour
and all later running totals are recalculated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from .api import BrightClient, Resource
from .const import (
    DOMAIN,
    KIND_COST,
    MAX_SPAN_PER_UPDATE,
    READINGS_CHUNK,
    REQUEST_PAUSE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def floor_hour(when: datetime) -> datetime:
    """Round down to the start of the UTC hour."""
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def statistic_id(ve_id: str, fuel: str, kind: str) -> str:
    """Return the external statistic ID for a stream."""
    site = "".join(ch for ch in ve_id.lower() if ch.isalnum())[:8]
    return f"{DOMAIN}:{site}_{fuel}_{kind}"


@dataclass(slots=True)
class ImportResult:
    """Outcome of importing one stream."""

    fetched_until: datetime
    backlog: bool
    hourly: dict[datetime, float]  # every hour value in the window, after merging
    latest_data: datetime | None  # start of the newest hour with data


def build_metadata(stat_id: str, name: str, kind: str) -> StatisticMetaData:
    """Build statistic metadata."""
    if kind == KIND_COST:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=None,
            unit_of_measurement=None,  # costs use the instance currency
        )
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=stat_id,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def unit_factor(kind: str, units: str | None) -> float | None:
    """Return the multiplier converting API units into kWh or pounds."""
    unit = (units or "").strip().lower()
    if kind == KIND_COST:
        if unit in ("pence", "p", "gbx"):
            return 0.01
        if unit in ("gbp", "pounds", "£"):
            return 1.0
        return None
    if unit in ("kwh", ""):
        return 1.0
    if unit == "wh":
        return 0.001
    return None  # e.g. m3 — not supported without a calorific value


def to_hourly(
    values: Iterable[tuple[datetime, float | None]], factor: float
) -> dict[datetime, float]:
    """Sum half-hourly values into UTC hours, skipping missing data."""
    hourly: dict[datetime, float] = {}
    for when, value in values:
        if value is None:
            continue
        hour = floor_hour(when)
        hourly[hour] = hourly.get(hour, 0.0) + value * factor
    return hourly


def merge_hours(
    fresh: dict[datetime, float], existing: dict[datetime, float]
) -> dict[datetime, float]:
    """Merge fresh API data over what is already stored.

    An hour missing from the fresh data keeps its stored value, and a fresh zero
    never overwrites a stored non-zero value. This stops a temporary short or
    empty API response from rewriting history downwards.
    """
    merged = dict(existing)
    for hour, value in fresh.items():
        old = existing.get(hour)
        if old is not None and old > 0 and value == 0:
            continue
        merged[hour] = value
    return merged


def cumulative(base: float, hourly: dict[datetime, float]) -> list[StatisticData]:
    """Build hourly statistics with a running sum starting at ``base``."""
    stats: list[StatisticData] = []
    total = base
    for hour in sorted(hourly):
        value = hourly[hour]
        total += value
        stats.append(StatisticData(start=hour, state=round(value, 6), sum=round(total, 6)))
    return stats


async def _stored_rows(
    hass: HomeAssistant, stat_id: str, start: datetime, end: datetime | None
) -> list[dict]:
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {stat_id},
        "hour",
        None,
        {"sum", "state"},
    )
    return list(rows.get(stat_id, []))


async def stored_state(
    hass: HomeAssistant, stat_id: str, window_start: datetime
) -> tuple[float, dict[datetime, float]]:
    """Return (sum just before window_start, stored hourly values from window_start)."""
    cutoff = window_start.timestamp()
    rows = await _stored_rows(hass, stat_id, window_start - timedelta(days=1), None)
    before = [r for r in rows if r["start"] < cutoff]
    # A gap of more than a day before the window (rare): look further back.
    for lookback_start in (window_start - timedelta(days=35), EPOCH):
        if before:
            break
        before = await _stored_rows(hass, stat_id, lookback_start, window_start)
    base = float(before[-1].get("sum") or 0.0) if before else 0.0

    existing: dict[datetime, float] = {}
    previous = base
    for row in rows:
        if row["start"] < cutoff:
            continue
        total = float(row.get("sum") or 0.0)
        existing[datetime.fromtimestamp(row["start"], tz=UTC)] = total - previous
        previous = total
    return base, existing


async def import_stream(
    hass: HomeAssistant,
    client: BrightClient,
    resource: Resource,
    metadata: StatisticMetaData,
    kind: str,
    fetch_start: datetime,
    now: datetime,
) -> ImportResult:
    """Fetch readings from ``fetch_start`` and write corrected hourly statistics."""
    window_start = floor_hour(fetch_start)
    fetch_end = min(window_start + MAX_SPAN_PER_UPDATE, now)
    backlog = fetch_end < now

    fresh: dict[datetime, float] = {}
    units_seen: str | None = None
    chunk_start = window_start
    while chunk_start < fetch_end:
        chunk_end = min(chunk_start + READINGS_CHUNK, fetch_end)
        readings = await client.get_readings(resource.resource_id, chunk_start, chunk_end)
        units_seen = readings.units or units_seen
        factor = unit_factor(kind, readings.units or resource.base_unit)
        if factor is None:
            _LOGGER.warning("Skipping %s: unsupported unit %r", resource.classifier, readings.units)
            return ImportResult(fetch_end, backlog, {}, None)
        for hour, value in to_hourly(readings.values, factor).items():
            fresh[hour] = fresh.get(hour, 0.0) + value
        chunk_start = chunk_end
        await asyncio.sleep(REQUEST_PAUSE_SECONDS)

    base, existing = await stored_state(hass, metadata["statistic_id"], window_start)
    merged = merge_hours(fresh, existing)
    if merged:
        async_add_external_statistics(hass, metadata, cumulative(base, merged))

    latest = max(fresh, default=None)
    _LOGGER.debug(
        "%s: fetched %s→%s (%s), %d fresh hours, %d stored, backlog=%s",
        metadata["statistic_id"],
        window_start,
        fetch_end,
        units_seen,
        len(fresh),
        len(existing),
        backlog,
    )
    return ImportResult(fetch_end, backlog, merged, latest)
