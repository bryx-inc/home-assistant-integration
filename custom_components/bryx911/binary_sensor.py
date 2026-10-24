"""Binary sensor platform for the Bryx 911 integration."""

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BryxConfigEntry, BryxCoordinator
from .entity import BryxEntity, BryxUnitEntity
from .models import BryxUnit


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BryxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Bryx binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [BryxConnection(coordinator)]
    entities.extend(
        BryxActiveJob(coordinator, unit) for unit in coordinator.tracked_units
    )
    async_add_entities(entities)


class BryxActiveJob(BryxUnitEntity, BinarySensorEntity):
    """On while at least one of the unit's jobs is open."""

    _attr_translation_key = "active_job"

    def __init__(self, coordinator: BryxCoordinator, unit: BryxUnit) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, unit, "active_job")

    @property
    def is_on(self) -> bool:
        """Return whether the unit has a job open."""
        return bool(self.jobs.open_jobs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the open jobs behind the state."""
        jobs = self.jobs
        latest = jobs.latest_open
        return {
            "open_job_count": len(jobs.open_jobs),
            "job_id": latest.id if latest else None,
            "synopsis": latest.synopsis if latest else None,
            "location": latest.location if latest else None,
            "job_type": latest.job_type if latest else None,
            "agency": latest.agency_name if latest else None,
        }


class BryxConnection(BryxEntity, BinarySensorEntity):
    """Reports whether the job subscription is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connection"

    def __init__(self, coordinator: BryxCoordinator) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        """Report the connection state even while the API is unreachable."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the subscription is connected."""
        return self.bryx.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detail about the subscription."""
        return {
            "initial_load_complete": self.bryx.initial_load_complete,
            "tracked_jobs": len(self.bryx.jobs),
        }
