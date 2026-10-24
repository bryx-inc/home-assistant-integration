"""Sensor platform for the Bryx 911 integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MAX_STATE_LENGTH
from .coordinator import BryxConfigEntry, BryxCoordinator
from .entity import BryxAgencyEntity, BryxUnitEntity
from .models import BryxAgency, BryxJob, BryxJobs, BryxUnit, slugify_response

StateValue = str | int | datetime | None


def _truncate(value: str | None) -> str | None:
    """Fit a value into Home Assistant's 255 character state limit.

    Real synopsis values reach 248 characters and job type descriptions reach
    210, so this is close enough to matter. The untruncated text stays available
    as an attribute.
    """
    if value is None:
        return None
    if len(value) <= MAX_STATE_LENGTH:
        return value
    return f"{value[: MAX_STATE_LENGTH - 1]}…"


def _job_attributes(job: BryxJob | None) -> dict[str, Any]:
    """Return the full detail for a job."""
    if job is None:
        return {}

    attributes: dict[str, Any] = {
        "job_id": job.id,
        "disposition": job.disposition,
        "agency": job.agency_name,
        "agency_id": job.agency_id,
        "synopsis": job.synopsis,
        "location": job.location,
        "job_type": job.job_type,
        "category": job.category,
        "section": job.section,
        "dispatched_at": job.ts.isoformat(),
        "units": job.units,
        "assignment": job.assignment,
        "responder_count": len(job.responders),
        "supplemental_count": len(job.supplementals),
    }

    optional = {
        "street": job.street,
        "city": job.city,
        "state": job.state,
        "postal_code": job.postal_code,
        "cross_streets": job.cross_streets,
        "municipality": job.municipality,
        "location_info": job.location_info,
        "box": job.box,
        "priority": job.priority,
        "incident_id": job.incident_id,
        "cad_id": job.cad_id,
        "delay_in_seconds": job.delay_in_seconds,
        "my_response": job.own_response,
    }
    attributes.update({key: value for key, value in optional.items() if value})

    if job.radio_channels:
        attributes["radio_channels"] = job.radio_channels
    if job.latitude is not None and job.longitude is not None:
        attributes["latitude"] = job.latitude
        attributes["longitude"] = job.longitude

    return attributes


@dataclass(frozen=True, kw_only=True)
class BryxSensorDescription(SensorEntityDescription):
    """Describes a sensor that reads one unit's jobs."""

    value_fn: Callable[[BryxJobs], StateValue]
    attributes_fn: Callable[[BryxJobs], dict[str, Any]] | None = None


SENSORS: tuple[BryxSensorDescription, ...] = (
    BryxSensorDescription(
        key="latest_job",
        translation_key="latest_job",
        value_fn=lambda data: _truncate(data.latest.synopsis) if data.latest else None,
        attributes_fn=lambda data: _job_attributes(data.latest),
    ),
    BryxSensorDescription(
        key="latest_job_type",
        translation_key="latest_job_type",
        value_fn=lambda data: _truncate(data.latest.job_type) if data.latest else None,
        attributes_fn=lambda data: (
            {
                "category": data.latest.category,
                "section": data.latest.section,
                "job_type": data.latest.job_type,
            }
            if data.latest
            else {}
        ),
    ),
    BryxSensorDescription(
        key="latest_job_location",
        translation_key="latest_job_location",
        value_fn=lambda data: _truncate(data.latest.location) if data.latest else None,
        attributes_fn=lambda data: _job_attributes(data.latest),
    ),
    BryxSensorDescription(
        key="latest_job_time",
        translation_key="latest_job_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.latest.ts if data.latest else None,
    ),
    BryxSensorDescription(
        key="open_jobs",
        translation_key="open_jobs",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.open_jobs),
        attributes_fn=lambda data: {
            "job_ids": [job.id for job in data.open_jobs],
            "jobs": [
                {
                    "id": job.id,
                    "synopsis": job.synopsis,
                    "location": job.location,
                    "job_type": job.job_type,
                    "agency": job.agency_name,
                    "dispatched_at": job.ts.isoformat(),
                }
                for job in data.open_jobs
            ],
        },
    ),
    BryxSensorDescription(
        key="supplementals",
        translation_key="supplementals",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.latest.supplementals) if data.latest else None,
        attributes_fn=lambda data: (
            {
                "job_id": data.latest.id,
                "supplementals": [
                    {
                        "text": supplemental.text,
                        "timestamp": (
                            supplemental.ts.isoformat() if supplemental.ts else None
                        ),
                    }
                    for supplemental in data.latest.supplementals
                ],
            }
            if data.latest
            else {}
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BryxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Bryx sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for agency in coordinator.tracked_agencies:
        # One counter per response option the agency offers, in its own wording,
        # rather than a hardcoded list.
        entities.extend(
            BryxResponderSensor(coordinator, agency, response)
            for response in agency.response_texts
        )
        entities.extend(
            BryxUnitSensor(coordinator, unit, description)
            for unit in agency.units
            for description in SENSORS
        )
    async_add_entities(entities)


class BryxUnitSensor(BryxUnitEntity, SensorEntity):
    """A sensor derived from one unit's jobs."""

    entity_description: BryxSensorDescription

    def __init__(
        self,
        coordinator: BryxCoordinator,
        unit: BryxUnit,
        description: BryxSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, unit, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateValue:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.jobs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the detail behind the state."""
        if self.entity_description.attributes_fn is None:
            return {}
        return self.entity_description.attributes_fn(self.jobs)


class BryxResponderSensor(BryxAgencyEntity, SensorEntity):
    """Counts members who gave a particular response across an agency's open jobs."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "responders"

    def __init__(
        self, coordinator: BryxCoordinator, agency: BryxAgency, response: str
    ) -> None:
        """Initialize the sensor."""
        self._response_key = slugify_response(response)
        super().__init__(coordinator, agency, f"responders_{self._response_key}")
        self._response = response
        self._attr_translation_placeholders = {"response": response}

    def _matching(self) -> list[tuple[BryxJob, Any]]:
        """Return the responders matching this response, with their job."""
        return [
            (job, responder)
            for job in self.jobs.open_jobs
            for responder in job.responders
            if responder.key == self._response_key
        ]

    @property
    def native_value(self) -> int:
        """Return how many members gave this response."""
        return len(self._matching())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who responded and to which job."""
        return {
            "response": self._response,
            "responders": [
                {
                    "name": responder.name,
                    "initials": responder.initials,
                    "job_id": job.id,
                    "responded_at": (
                        responder.response_time.isoformat()
                        if responder.response_time
                        else None
                    ),
                }
                for job, responder in self._matching()
            ],
        }
