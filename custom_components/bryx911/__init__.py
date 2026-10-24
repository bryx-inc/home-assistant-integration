"""The Bryx 911 integration."""

from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DEFAULT_URL
from .coordinator import BryxConfigEntry, BryxCoordinator
from .entity import account_identifier, agency_identifier, unit_identifier

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Version 1 kept every job entity on the account device, under these keys and
# one "responders_<response>" key per response option.
_V1_JOB_KEYS = frozenset(
    {
        "active_job",
        "calendar",
        "latest_job",
        "latest_job_location",
        "latest_job_time",
        "latest_job_type",
        "open_jobs",
        "supplementals",
    }
)

# Version 2.1 stored the server as a named environment under this key. Version
# 2.2 stores its base URL instead.
_V2_1_ENVIRONMENT = "environment"
_V2_1_URLS = {
    "production": DEFAULT_URL,
    "development": "https://graph.dev.bryx911.com",
}


async def async_setup_entry(hass: HomeAssistant, entry: BryxConfigEntry) -> bool:
    """Set up Bryx 911 from a config entry."""
    coordinator = BryxCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    _async_sync_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BryxConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: BryxConfigEntry) -> bool:
    """Upgrade an entry created by an older version.

    Version 1 put every job entity on the account device. Version 2 moves them
    onto agency and unit devices, so the old ones are removed. The duty status
    switch and the connection sensor keep their entities.

    Version 2.2 replaces the named environment with the server's base URL.
    """
    if entry.version == 1:
        registry = er.async_get(hass)
        prefix = f"{entry.entry_id}_"
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            key = entity.unique_id.removeprefix(prefix)
            if key in _V1_JOB_KEYS or key.startswith("responders_"):
                registry.async_remove(entity.entity_id)
        hass.config_entries.async_update_entry(entry, version=2)
    if entry.version == 2 and entry.minor_version < 2:
        data = dict(entry.data)
        environment = data.pop(_V2_1_ENVIRONMENT, "production")
        data[CONF_URL] = _V2_1_URLS.get(environment, DEFAULT_URL)
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True


@callback
def _async_sync_devices(hass: HomeAssistant, entry: BryxConfigEntry) -> None:
    """Register the account, agency and unit devices, and remove stale ones.

    Parents are registered first because a child names its parent by device
    registry id. A device for an agency or unit that is no longer tracked, after
    an options change or a change to the member's units, is removed along with
    its entities.
    """
    registry = dr.async_get(hass)
    entry_id = entry.entry_id
    account = registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={account_identifier(entry_id)},
        entry_type=dr.DeviceEntryType.SERVICE,
        manufacturer="Bryx",
        model="Bryx 911",
        name=entry.title,
        configuration_url="https://bryx911.com",
    )
    current = {account.id}

    for agency in entry.runtime_data.tracked_agencies:
        agency_device = registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={agency_identifier(entry_id, agency.id)},
            entry_type=dr.DeviceEntryType.SERVICE,
            manufacturer="Bryx",
            model="Agency",
            name=agency.name,
            via_device_id=account.id,
        )
        current.add(agency_device.id)
        for unit in agency.units:
            unit_device = registry.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={unit_identifier(entry_id, unit.id)},
                entry_type=dr.DeviceEntryType.SERVICE,
                manufacturer="Bryx",
                model="Unit",
                name=unit.name,
                via_device_id=agency_device.id,
            )
            current.add(unit_device.id)

    for device in dr.async_entries_for_config_entry(registry, entry_id):
        if device.id not in current:
            registry.async_remove_device(device.id)
