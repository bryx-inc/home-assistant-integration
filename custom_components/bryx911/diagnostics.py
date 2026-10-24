"""Diagnostics for the Bryx 911 integration."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_API_KEY,
    CONF_CLIENT_ID,
    CONF_DEVICE_ID,
    CONF_EXCLUDED_UNITS,
    CONF_HARDWARE_ID,
)
from .coordinator import BryxConfigEntry

TO_REDACT = {CONF_API_KEY, CONF_CLIENT_ID, CONF_DEVICE_ID, CONF_EMAIL, CONF_HARDWARE_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BryxConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Job payloads carry dispatch data about real incidents and real people, so
    only counts and non-identifying summaries are included.
    """
    coordinator = entry.runtime_data
    data = coordinator.data

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        # Unit ids are readable keys that name the unit and its agency.
        "options": async_redact_data(dict(entry.options), {CONF_EXCLUDED_UNITS}),
        "connected": data.connected,
        "initial_load_complete": data.initial_load_complete,
        "on_duty": data.on_duty,
        "tracked_job_count": len(data.jobs),
        "open_job_count": len(data.open_jobs),
        "agency_count": len(coordinator.agencies),
        "tracked_agency_count": len(coordinator.tracked_agencies),
        "unit_count": len(coordinator.tracked_units),
        "response_options": sorted(
            {
                text
                for agency in coordinator.tracked_agencies
                for text in agency.response_texts
            }
        ),
        "dispositions": sorted({job.disposition for job in data.jobs.values()}),
    }
