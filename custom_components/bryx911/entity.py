"""Base entities for the Bryx 911 integration."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BryxCoordinator
from .models import BryxAgency, BryxData, BryxJobs, BryxUnit


def account_identifier(entry_id: str) -> tuple[str, str]:
    """Return the device identifier for the signed-in member's account."""
    return (DOMAIN, entry_id)


def agency_identifier(entry_id: str, agency_id: str) -> tuple[str, str]:
    """Return the device identifier for one agency."""
    return (DOMAIN, f"{entry_id}_agency_{agency_id}")


def unit_identifier(entry_id: str, unit_id: str) -> tuple[str, str]:
    """Return the device identifier for one unit."""
    return (DOMAIN, f"{entry_id}_unit_{unit_id}")


class BryxEntity(CoordinatorEntity[BryxCoordinator]):
    """An entity on the account device.

    Setup registers every device before the platforms load, parents first, so
    an entity only has to identify the device it belongs to.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: BryxCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{self._entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={account_identifier(self._entry_id)}
        )

    @property
    def available(self) -> bool:
        """Return whether the entity has usable data.

        Job data arrives over the websocket, not the polling interval, so a
        failed duty-status poll must not black out live job state. The entity is
        available while either source is healthy.
        """
        return self.coordinator.last_update_success or self.coordinator.data.connected

    @property
    def bryx(self) -> BryxData:
        """Return the current Bryx state."""
        return self.coordinator.data


class BryxAgencyEntity(BryxEntity):
    """An entity on one agency's device, covering the jobs it dispatched."""

    def __init__(
        self, coordinator: BryxCoordinator, agency: BryxAgency, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, f"agency_{agency.id}_{key}")
        self.agency = agency
        self._attr_device_info = DeviceInfo(
            identifiers={agency_identifier(self._entry_id, agency.id)}
        )

    @property
    def jobs(self) -> BryxJobs:
        """Return the agency's jobs."""
        return self.bryx.for_agency(self.agency.id)


class BryxUnitEntity(BryxEntity):
    """An entity on one unit's device, covering the jobs that list the unit."""

    def __init__(self, coordinator: BryxCoordinator, unit: BryxUnit, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, f"unit_{unit.id}_{key}")
        self.unit = unit
        self._attr_device_info = DeviceInfo(
            identifiers={unit_identifier(self._entry_id, unit.id)}
        )

    @property
    def jobs(self) -> BryxJobs:
        """Return the unit's jobs."""
        return self.bryx.for_unit(self.unit.id)
