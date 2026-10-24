"""Switch platform for the Bryx 911 integration."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import BryxError
from .const import DOMAIN
from .coordinator import BryxConfigEntry, BryxCoordinator
from .entity import BryxEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BryxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Bryx switches."""
    async_add_entities([BryxDutyStatusSwitch(entry.runtime_data)])


class BryxDutyStatusSwitch(BryxEntity, SwitchEntity):
    """Puts the signed-in member on or off duty."""

    _attr_translation_key = "on_duty"

    def __init__(self, coordinator: BryxCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, "on_duty")

    @property
    def is_on(self) -> bool:
        """Return whether the member is on duty."""
        return self.bryx.on_duty

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Go on duty."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Go off duty."""
        await self._async_set(False)

    async def _async_set(self, on_duty: bool) -> None:
        """Send the duty status to the API."""
        try:
            await self.coordinator.async_set_duty_status(on_duty)
        except BryxError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="duty_status_failed",
                translation_placeholders={"error": str(err)},
            ) from err
