"""Data models for the Bryx 911 integration."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
from typing import Any

from .const import OPEN_DISPOSITIONS

_LOGGER = logging.getLogger(__name__)


def parse_instant(value: Any) -> datetime | None:
    """Convert a GraphQL Instant into an aware datetime.

    The API serializes Instant as a string holding epoch milliseconds, for
    example "1790865000000".
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except TypeError, ValueError:
        _LOGGER.debug("Could not parse Instant %r", value)
        return None


def parse_geo_point(value: Any) -> tuple[float, float] | None:
    """Convert a GraphQL GeoPoint into a (latitude, longitude) pair.

    GeoPoint is a string containing GeoJSON, and GeoJSON orders coordinates
    longitude first.
    """
    if not value:
        return None
    try:
        geo = json.loads(value) if isinstance(value, str) else value
        longitude, latitude = geo["coordinates"][0], geo["coordinates"][1]
        return float(latitude), float(longitude)
    except TypeError, ValueError, KeyError, IndexError:
        _LOGGER.debug("Could not parse GeoPoint %r", value)
        return None


def slugify_response(text: str) -> str:
    """Build a stable key for a response option's text."""
    return "_".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


@dataclass(frozen=True, slots=True)
class BryxResponder:
    """A member's response to a job."""

    client_id: str
    name: str
    initials: str
    response: str
    is_positive: bool
    response_time: datetime | None

    @property
    def key(self) -> str:
        """Return the normalized response text."""
        return slugify_response(self.response)


@dataclass(frozen=True, slots=True)
class BryxSupplemental:
    """A supplemental note added to a job after dispatch."""

    id: str
    text: str
    ts: datetime | None


@dataclass(frozen=True, slots=True)
class BryxJob:
    """A dispatched job."""

    id: str
    ts: datetime
    created: datetime | None
    last_modified: datetime | None
    disposition: str
    synopsis: str
    location: str
    latitude: float | None
    longitude: float | None
    job_type: str
    category: str
    section: str
    agency_id: str
    agency_name: str
    units: list[str] = field(default_factory=list)
    unit_ids: list[str] = field(default_factory=list)
    assignment: list[str] = field(default_factory=list)
    radio_channels: list[str] = field(default_factory=list)
    responders: list[BryxResponder] = field(default_factory=list)
    supplementals: list[BryxSupplemental] = field(default_factory=list)
    own_response: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    street: str | None = None
    cross_streets: str | None = None
    box: str | None = None
    priority: str | None = None
    incident_id: str | None = None
    cad_id: str | None = None
    municipality: str | None = None
    location_info: str | None = None
    delay_in_seconds: int | None = None

    @property
    def is_open(self) -> bool:
        """Return whether the job is still an active call.

        OPEN and OLD are both open; OLD is an open job that has aged rather than
        one that has finished. CLOSED and TIMED_OUT are both closed.
        """
        return self.disposition in OPEN_DISPOSITIONS

    @classmethod
    def from_node(cls, node: dict[str, Any]) -> BryxJob:
        """Build a job from a GraphQL JobNode."""
        coordinates = parse_geo_point(node.get("centroid"))
        job_type = node.get("type") or {}
        agency = node.get("agency") or {}
        own_response = node.get("currentResponse") or {}

        responders = [
            BryxResponder(
                client_id=(responder.get("client") or {}).get("id", ""),
                name=(responder.get("client") or {}).get("fullName", "Unknown"),
                initials=(responder.get("client") or {}).get("initials", ""),
                response=responder.get("verboseResponse") or "",
                is_positive=bool(responder.get("isPositive")),
                response_time=parse_instant(responder.get("responseTime")),
            )
            for responder in node.get("responders") or []
        ]

        supplementals = [
            BryxSupplemental(
                id=supplemental.get("id", ""),
                text=supplemental.get("text") or "",
                ts=parse_instant(supplemental.get("ts")),
            )
            for supplemental in node.get("supplementals") or []
        ]
        supplementals.sort(key=lambda item: item.ts or datetime.min.replace(tzinfo=UTC))

        job_units = node.get("units") or []
        units = [
            (unit.get("unit") or {}).get("name") or unit.get("unitId") or ""
            for unit in job_units
        ]
        unit_ids = [
            unit.get("unitId") or (unit.get("unit") or {}).get("id") or ""
            for unit in job_units
        ]

        radio_channels = [
            channel.get("channel", "")
            for channel in node.get("radioChannels") or []
            if channel.get("channel")
        ]

        # ts is the only timestamp guaranteed to be present on every job.
        ts = parse_instant(node.get("ts")) or datetime.now(UTC)

        return cls(
            id=node["id"],
            ts=ts,
            created=parse_instant(node.get("creationTs")),
            last_modified=parse_instant(node.get("lastModified")),
            disposition=node.get("disposition") or "",
            synopsis=node.get("synopsis") or "",
            location=node.get("locationOfIncident") or "",
            latitude=coordinates[0] if coordinates else None,
            longitude=coordinates[1] if coordinates else None,
            job_type=job_type.get("description") or "",
            category=job_type.get("category") or "",
            section=job_type.get("section") or "",
            agency_id=agency.get("id") or "",
            agency_name=agency.get("name") or "",
            units=[unit for unit in units if unit],
            unit_ids=[unit_id for unit_id in unit_ids if unit_id],
            assignment=list(node.get("assignment") or []),
            radio_channels=radio_channels,
            responders=responders,
            supplementals=supplementals,
            own_response=own_response.get("verboseResponse"),
            city=node.get("city"),
            state=node.get("state"),
            postal_code=node.get("zip"),
            street=node.get("street"),
            cross_streets=node.get("crossStreets"),
            box=node.get("box"),
            priority=node.get("priority"),
            incident_id=node.get("incidentId"),
            cad_id=node.get("cadId"),
            municipality=node.get("municipality"),
            location_info=node.get("locationInfo"),
            delay_in_seconds=node.get("delayInSeconds"),
        )


@dataclass(frozen=True, slots=True)
class BryxResponseOption:
    """A response option offered by an agency."""

    id: str
    text: str
    is_positive: bool

    @property
    def key(self) -> str:
        """Return the normalized response text."""
        return slugify_response(self.text)


@dataclass(frozen=True, slots=True)
class BryxUnit:
    """A unit the signed-in member belongs to."""

    id: str
    name: str
    agency_id: str


@dataclass(frozen=True, slots=True)
class BryxAgency:
    """An agency the signed-in member belongs to."""

    id: str
    name: str
    response_options: list[BryxResponseOption] = field(default_factory=list)
    # Only filled in for tracked agencies.
    units: list[BryxUnit] = field(default_factory=list)

    @property
    def response_texts(self) -> list[str]:
        """Return the agency's distinct response option texts.

        Agencies sometimes list the same text more than once, so the texts are
        deduplicated on their normalized key.
        """
        seen: dict[str, str] = {}
        for option in self.response_options:
            seen.setdefault(option.key, option.text)
        return [seen[key] for key in sorted(seen)]


@dataclass(frozen=True, slots=True)
class BryxJobs:
    """A selection of jobs, newest first, with the views entities read."""

    sorted_jobs: list[BryxJob]

    @property
    def latest(self) -> BryxJob | None:
        """Return the most recent job."""
        return self.sorted_jobs[0] if self.sorted_jobs else None

    @property
    def open_jobs(self) -> list[BryxJob]:
        """Return the active jobs, newest first."""
        return [job for job in self.sorted_jobs if job.is_open]

    @property
    def latest_open(self) -> BryxJob | None:
        """Return the most recent active job."""
        jobs = self.open_jobs
        return jobs[0] if jobs else None


@dataclass
class BryxData:
    """State shared by every entity in a config entry."""

    jobs: dict[str, BryxJob] = field(default_factory=dict)
    connected: bool = False
    initial_load_complete: bool = False
    on_duty: bool = False
    client_name: str = ""

    @property
    def sorted_jobs(self) -> list[BryxJob]:
        """Return tracked jobs, newest first."""
        return sorted(self.jobs.values(), key=lambda job: job.ts, reverse=True)

    @property
    def open_jobs(self) -> list[BryxJob]:
        """Return the active jobs, newest first."""
        return [job for job in self.sorted_jobs if job.is_open]

    def for_agency(self, agency_id: str) -> BryxJobs:
        """Return the jobs one agency dispatched."""
        return BryxJobs([job for job in self.sorted_jobs if job.agency_id == agency_id])

    def for_unit(self, unit_id: str) -> BryxJobs:
        """Return the jobs that list one unit, whichever agency dispatched them."""
        return BryxJobs([job for job in self.sorted_jobs if unit_id in job.unit_ids])
