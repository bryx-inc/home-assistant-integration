"""Calendar platform for the Bryx 911 integration."""

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_EVENT_DURATION_MINUTES
from .coordinator import BryxConfigEntry, BryxCoordinator
from .entity import BryxAgencyEntity
from .models import BryxAgency, BryxJob

_DEFAULT_DURATION = timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BryxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one calendar per tracked agency."""
    coordinator = entry.runtime_data
    async_add_entities(
        BryxJobCalendar(coordinator, agency) for agency in coordinator.tracked_agencies
    )


class BryxJobCalendar(BryxAgencyEntity, CalendarEntity):
    """Presents an agency's tracked jobs as calendar events."""

    _attr_translation_key = "jobs"

    def __init__(self, coordinator: BryxCoordinator, agency: BryxAgency) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator, agency, "calendar")

    @staticmethod
    def _to_event(job: BryxJob) -> CalendarEvent:
        """Convert a job into a calendar event.

        A closed job runs until it was last modified; an open one is shown with a
        nominal duration because its end is not yet known.
        """
        start = job.ts
        if not job.is_open and job.last_modified and job.last_modified > start:
            end = job.last_modified
        else:
            end = start + _DEFAULT_DURATION

        summary = " - ".join(part for part in (job.job_type, job.synopsis) if part)
        return CalendarEvent(
            start=start,
            end=end,
            summary=summary or job.synopsis or "Bryx job",
            description=job.synopsis or None,
            location=job.location or None,
            uid=job.id,
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the job to show as the current event."""
        jobs = self.jobs
        job = jobs.latest_open or jobs.latest
        return self._to_event(job) if job else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the agency's tracked jobs that overlap the requested range."""
        events = [self._to_event(job) for job in self.jobs.sorted_jobs]
        return [
            event
            for event in events
            if event.start < end_date and event.end > start_date
        ]
